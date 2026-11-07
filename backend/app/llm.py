"""One structured-output call to the configured model provider.

Every AI feature asks for JSON that matches a schema. `structured_json` sends that request
to Claude (the default) or OpenAI, depending on `config.ai_review.provider`, and returns the
parsed JSON plus the model that actually answered, for `ai_metadata`. Provider failures
become `AIProviderError`, which `main.py` turns into a JSON error instead of a bare 500.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from .config import get_config

# Claude's server-side fallback: if the model declines on policy grounds, the API reruns
# the request on a server-chosen fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT_SECONDS = 280.0  # under Vercel's 300 s function limit


class AIProviderError(Exception):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class StructuredResult:
    data: dict
    provider: str
    model: str
    reasoning_effort: str
    input_tokens: int = 0
    output_tokens: int = 0


def structured_json(*, system: str, user: str, schema: dict, name: str, effort: str | None = None) -> StructuredResult:
    cfg = get_config().ai_review
    effort = effort or cfg.reasoning_effort
    if cfg.provider == "anthropic":
        return _anthropic(system=system, user=user, schema=schema, model=cfg.model, effort=effort,
                          max_tokens=cfg.max_output_tokens)
    if cfg.provider == "openai":
        return _openai(system=system, user=user, schema=schema, name=name, model=cfg.model, effort=effort)
    raise AIProviderError(f"Unknown AI provider {cfg.provider!r} in config.ai_review.provider.", 500)


# --- Claude -------------------------------------------------------------------------------


def anthropic_schema(schema: Any) -> Any:
    """Rewrites `{"type": [X, "null"]}` as `anyOf`, the form Claude's structured outputs
    documents for nullable fields. Everything else passes through unchanged."""
    if isinstance(schema, list):
        return [anthropic_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: anthropic_schema(v) for k, v in schema.items()}
    types = out.get("type")
    if isinstance(types, list):
        out.pop("type")
        out["anyOf"] = [{"type": t} for t in types]
    return out


def _anthropic_client():
    import anthropic

    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2)


def _anthropic(*, system: str, user: str, schema: dict, model: str, effort: str, max_tokens: int) -> StructuredResult:
    import anthropic

    try:
        client = _anthropic_client()
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": anthropic_schema(copy.deepcopy(schema))},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AIProviderError("Claude rejected the API key. Check ANTHROPIC_API_KEY.", 503) from exc
    except (anthropic.RateLimitError, anthropic.InternalServerError) as exc:
        raise AIProviderError("Claude is busy right now. Try again in a minute.", 503) from exc
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
        raise AIProviderError("Couldn't reach Claude. Try again in a minute.", 503) from exc
    except anthropic.APIStatusError as exc:
        raise AIProviderError(f"Claude rejected the request ({exc.status_code}): {exc.message}", 502) from exc
    except anthropic.AnthropicError as exc:
        # Includes a missing ANTHROPIC_API_KEY, which surfaces when the client resolves credentials.
        raise AIProviderError(f"Couldn't call Claude: {exc}", 503) from exc

    if message.stop_reason == "refusal":
        raise AIProviderError("Claude declined this request.", 422)
    if message.stop_reason == "max_tokens":
        raise AIProviderError("Claude's answer was cut off before it finished. Try again.", 502)
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise AIProviderError("Claude returned no answer.", 502)
    usage = message.usage
    return StructuredResult(
        data=_parse(text),
        provider="anthropic",
        model=message.model,  # the fallback model, when the fallback answered
        reasoning_effort=effort,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


# --- OpenAI (kept selectable for local runs against the old model) -----------------------


def _openai(*, system: str, user: str, schema: dict, name: str, model: str, effort: str) -> StructuredResult:
    import openai

    try:
        response = openai.OpenAI().responses.create(
            model=model,
            reasoning={"effort": effort},
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
        )
    except openai.OpenAIError as exc:
        raise AIProviderError(f"Couldn't call OpenAI: {exc}", 502) from exc
    usage = getattr(response, "usage", None)
    return StructuredResult(
        data=_parse(response.output_text),
        provider="openai",
        model=model,
        reasoning_effort=effort,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def _parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIProviderError("The model's answer wasn't valid JSON.", 502) from exc
    if not isinstance(data, dict):
        raise AIProviderError("The model's answer wasn't a JSON object.", 502)
    return data
