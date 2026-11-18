"""app/llm.py without a model: a fake Claude client stands in for the SDK."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import llm


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class _FakeClient:
    def __init__(self, message):
        self.calls = []
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return _FakeStream(message)

        self.beta = SimpleNamespace(messages=_Messages())


def _message(text: str | None, stop_reason: str = "end_turn", model: str = "claude-opus-5-5"):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


SCHEMA = {
    "type": "object",
    "properties": {"rq": {"type": ["integer", "null"]}, "items": {"type": "array", "items": {"type": "string"}}},
    "required": ["rq", "items"],
    "additionalProperties": False,
}


@pytest.fixture
def fake(monkeypatch):
    def install(message):
        client = _FakeClient(message)
        monkeypatch.setattr(llm, "_anthropic_client", lambda: client)
        return client

    return install


def test_nullable_types_become_any_of():
    out = llm.anthropic_schema(SCHEMA)
    assert out["properties"]["rq"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert out["properties"]["items"] == SCHEMA["properties"]["items"]
    assert SCHEMA["properties"]["rq"] == {"type": ["integer", "null"]}  # input untouched


def test_returns_parsed_json_and_served_model(fake):
    client = fake(_message('{"rq": 3, "items": ["a"]}', model="claude-fallback"))
    result = llm.structured_json(system="sys", user="{}", schema=SCHEMA, name="x")
    assert result.data == {"rq": 3, "items": ["a"]}
    assert result.model == "claude-fallback"
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    sent = client.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"] == "sys"
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"
    assert "thinking" not in sent


def test_effort_override(fake):
    client = fake(_message('{"rq": null, "items": []}'))
    llm.structured_json(system="s", user="u", schema=SCHEMA, name="x", effort="high")
    assert client.calls[0]["output_config"]["effort"] == "high"


@pytest.mark.parametrize(
    "message, status",
    [
        (_message(None, stop_reason="refusal"), 422),
        (_message('{"rq": 1', stop_reason="max_tokens"), 502),
        (_message(None), 502),
        (_message("not json"), 502),
    ],
)
def test_bad_answers_raise_provider_errors(fake, message, status):
    fake(message)
    with pytest.raises(llm.AIProviderError) as exc:
        llm.structured_json(system="s", user="u", schema=SCHEMA, name="x")
    assert exc.value.status_code == status



def test_provider_errors_answer_as_json():
    from fastapi.testclient import TestClient

    from app.main import app

    def boom():
        raise llm.AIProviderError("Claude is busy right now. Try again in a minute.", 503)

    app.add_api_route("/_test_ai_error", boom)
    try:
        r = TestClient(app).get("/_test_ai_error")
    finally:
        app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != "/_test_ai_error"]
    assert r.status_code == 503
    assert r.json() == {"detail": "Claude is busy right now. Try again in a minute.", "source": "ai"}
