"""AI-offered "hear next" lines and their cache, gists made through the app, and the
conversation recommended after a session. The model is never called: `structured_json` is
replaced with a fake that returns hand-built output and records what it was sent."""
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import PrototypeSession
from app.llm import StructuredResult, _fallback_options
from app.main import app
from app.models_prototype import ConversationGist, ListeningInterest, ListeningSuggestionRun
from app.services import gists, listening_suggestions, next_steps

client = TestClient(app)
_user_ids = itertools.count(96000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


class FakeModel:
    def __init__(self, monkeypatch, module, data):
        self.data = data
        self.calls: list[dict] = []
        monkeypatch.setattr(module, "structured_json", self)

    def __call__(self, *, system, user, schema, name, effort=None, model=None):
        self.calls.append({"payload": json.loads(user), "model": model, "effort": effort})
        return StructuredResult(data=self.data, provider="anthropic", model=model or "default", reasoning_effort=effort or "low")


def _refresh():
    r = client.post("/listening_interests/suggestions/refresh")
    assert r.status_code == 200, r.text
    return r.json()


def test_suggestions_are_offered_once_cached_and_never_repeat_a_refusal(user_id, monkeypatch):
    cfg = get_config().listening_suggestions
    fake = FakeModel(monkeypatch, listening_suggestions, {"suggestions": [
        {"text": "Hear from renters about meeting times", "reason": "From your question about who shows up."},
        {"text": "hear from renters about meeting times.", "reason": "The same line again."},
        {"text": "Hear how newcomers find out about meetings", "reason": "From your highlight."},
        {"text": "Already on my list", "reason": "A line the user has."},
        {"text": "Hear from people who stopped coming", "reason": "A fourth."},
    ]})

    # Nothing to go on: no call.
    assert _refresh() == {"suggestions": [], "generated": False, "reason": "nothing_to_go_on"}
    assert fake.calls == []

    client.post("/listening_interests", json={"text": "Already on my list"})
    client.post("/research_questions", json={"name": "Who shows up, and who doesn't?"})
    snippet = client.get("/conversations/9").json()["snippets"][0]
    client.post("/conversations/9/highlights", json={"notes": "why this matters", "snippets": [
        {"snippet_id": snippet["snippet_id"], "start_index": 0, "end_index": 2}]})

    first = _refresh()
    assert first["generated"] is True
    assert [s["text"] for s in first["suggestions"]] == [
        "Hear from renters about meeting times", "Hear how newcomers find out about meetings",
        "Hear from people who stopped coming"][: cfg.max_suggestions]
    sent = fake.calls[0]
    assert sent["model"] == cfg.model and sent["model"].startswith("claude-haiku")
    assert sent["payload"]["questions"][0]["name"] == "Who shows up, and who doesn't?"
    assert sent["payload"]["recent_highlights"] == [
        {"text": " ".join(w["word"] for w in snippet["words"][:2]), "note": "why this matters"}]
    assert sent["payload"]["lines_already_on_the_list"] == ["Already on my list"]

    # Offers still waiting: listed without a call, by either route.
    assert _refresh()["reason"] == "undecided"
    assert len(client.get("/listening_interests/suggestions").json()) == len(first["suggestions"])
    assert len(fake.calls) == 1

    keep, *rest = first["suggestions"]
    kept = client.post(f"/listening_interests/suggestions/{keep['id']}/accept").json()
    assert kept["interest"]["text"] == keep["text"] and kept["interest"]["origin"] == "suggestion"
    assert client.post(f"/listening_interests/suggestions/{keep['id']}/accept").status_code == 409
    for s in rest:
        assert client.post(f"/listening_interests/suggestions/{s['id']}/dismiss").json()["status"] == "dismissed"
    assert keep["text"] in [i["text"] for i in client.get("/listening_interests").json()]

    # Everything decided and nothing changed: the same inputs are never sent twice.
    assert _refresh()["reason"] == "unchanged"
    # Changed, but the last run was a moment ago.
    client.post("/research_questions", json={"name": "What gets in the way?"})
    assert _refresh()["reason"] == "recent"
    assert len(fake.calls) == 1

    # Past the wait, the model is told what was turned down, and offering it again is dropped.
    monkeypatch.setattr(cfg, "min_minutes_between_runs", 0)
    again = _refresh()
    assert again == {"suggestions": [], "generated": True, "reason": None}
    assert fake.calls[1]["payload"]["lines_turned_down"] == [s["text"] for s in rest]
    assert _refresh()["reason"] == "unchanged"  # a run that offered nothing is still remembered
    with PrototypeSession() as db:
        assert db.query(ListeningSuggestionRun).filter(ListeningSuggestionRun.user_id == user_id).count() == 2
        assert db.query(ListeningInterest).filter(ListeningInterest.user_id == user_id).count() == 2

    kinds = [a["kind"] for a in client.get("/activity?limit=50").json()["items"]]
    assert {"listening_lines_suggested", "listening_line_accepted", "listening_line_dismissed"} <= set(kinds)


def test_another_users_suggestion_is_not_found(user_id, monkeypatch):
    FakeModel(monkeypatch, listening_suggestions, {"suggestions": [{"text": "Hear from elders", "reason": "r"}]})
    client.post("/research_questions", json={"name": "Whose voice is missing?"})
    sid = _refresh()["suggestions"][0]["id"]
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=next(_user_ids))
    assert client.post(f"/listening_interests/suggestions/{sid}/accept").status_code == 404
    assert client.post(f"/listening_interests/suggestions/{sid}/dismiss").status_code == 404
    assert client.get("/listening_interests/suggestions").json() == []


def test_haiku_is_not_asked_for_a_server_side_fallback():
    assert _fallback_options("claude-haiku-5-5") == {}
    assert _fallback_options("claude-opus-5-5")["fallbacks"] == "default"


@pytest.fixture
def no_gists():
    """These tests need to know exactly which conversations have a gist."""
    with PrototypeSession() as db:
        saved = [(g.conversation_id, g.project_id, g.forum_id, g.summary_text, g.keywords_json) for g in db.query(ConversationGist)]
        db.query(ConversationGist).delete()
        db.commit()
    yield
    with PrototypeSession() as db:
        db.query(ConversationGist).delete()
        for cid, pid, fid, text, kw in saved:
            db.add(ConversationGist(conversation_id=cid, project_id=pid, forum_id=fid, summary_text=text, keywords_json=kw))
        db.commit()


def test_gists_are_made_a_few_per_call_until_none_remain(user_id, no_gists, monkeypatch):
    monkeypatch.setattr(get_config().gists, "max_per_request", 2)
    fake = FakeModel(monkeypatch, gists, {"summary": "Neighbours talk about the bus route.", "keywords": ["buses", 7]})
    before = client.get("/gists/status").json()
    assert before["with_gist"] == 0 and before["remaining"] == before["total"] > 2

    first = client.post("/gists/generate").json()
    assert len(first["generated"]) + len(first["without_transcript"]) == 2 and first["failed"] == []
    assert first["remaining"] == before["total"] - 2
    assert all(c["model"] == get_config().gists.model for c in fake.calls)
    assert fake.calls[0]["payload"]["snippets"] and "title" in fake.calls[0]["payload"]
    with PrototypeSession() as db:
        g = db.get(ConversationGist, first["generated"][0])
        assert g.summary_text == "Neighbours talk about the bus route." and json.loads(g.keywords_json) == ["buses", "7"]

    second = client.post("/gists/generate").json()
    assert not set(second["generated"]) & set(first["generated"])
    assert second["remaining"] == before["total"] - 4


def test_a_model_failure_stops_the_batch_and_keeps_what_was_made(user_id, no_gists, monkeypatch):
    from app.llm import AIProviderError

    monkeypatch.setattr(get_config().gists, "max_per_request", 3)
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise AIProviderError("Claude is busy right now. Try again in a minute.", 503)
        return StructuredResult(data={"summary": "s", "keywords": []}, provider="anthropic", model="m", reasoning_effort="low")

    monkeypatch.setattr(gists, "structured_json", flaky)
    out = client.post("/gists/generate").json()
    assert len(out["generated"]) == 1 and len(calls) == 2
    assert out["failed"][0]["detail"].startswith("Claude is busy")
    assert out["with_gist"] == 1


def test_a_conversation_is_recommended_after_a_session_and_then_served_from_what_is_saved(user_id, no_gists, monkeypatch):
    convs = [c["id"] for c in client.get("/conversations").json()]
    target, other = convs[0], convs[1]
    fake = FakeModel(monkeypatch, next_steps, {"suggestions": [
        {"research_question_id": None, "suggested_direction": "Renters and meeting times",
         "recommended_conversation_id": target, "rationale": "Its summary mentions renters."},
        {"research_question_id": None, "suggested_direction": None, "recommended_conversation_id": 99999999, "rationale": "made up"},
    ]})

    # No summaries to choose from: the model is not called.
    assert client.post("/next_steps/recommend").json() == {"recommendation": None, "generated": False, "reason": "no_summaries"}
    assert fake.calls == []

    with PrototypeSession() as db:
        for cid in (target, other):
            db.add(ConversationGist(conversation_id=cid, project_id=1, summary_text="People talk about renters.", keywords_json="[]"))
        db.commit()

    first = client.post("/next_steps/recommend").json()
    assert first["generated"] is True and first["recommendation"]["recommended_conversation_id"] == target
    assert first["recommendation"]["recommended_conversation_title"]
    again = client.post("/next_steps/recommend").json()
    assert again["generated"] is False and again["recommendation"]["id"] == first["recommendation"]["id"]
    assert len(fake.calls) == 1

    # Once that conversation has been opened it is no longer the recommendation, and a second
    # call so soon is not made.
    client.put(f"/conversations/{target}/reading_position", json={})
    assert client.post("/next_steps/recommend").json() == {"recommendation": None, "generated": False, "reason": "recent"}
    assert len(fake.calls) == 1


def test_a_failed_call_is_remembered_so_the_page_does_not_pay_for_it_on_every_load(user_id, monkeypatch):
    from app.llm import AIProviderError

    calls = []

    def declines(**kwargs):
        calls.append(kwargs)
        raise AIProviderError("Claude declined this request.", 422)

    monkeypatch.setattr(listening_suggestions, "structured_json", declines)
    client.post("/research_questions", json={"name": "Who is missing?"})
    assert client.post("/listening_interests/suggestions/refresh").status_code == 422
    assert _refresh()["reason"] == "recent" and len(calls) == 1
    # After the wait it is tried again: a failure is not treated as an answer.
    monkeypatch.setattr(get_config().listening_suggestions, "min_minutes_between_runs", 0)
    assert client.post("/listening_interests/suggestions/refresh").status_code == 422
    assert len(calls) == 2


def test_a_declined_conversation_is_skipped_and_does_not_hold_up_the_rest(user_id, no_gists, monkeypatch):
    from app.llm import AIProviderError

    monkeypatch.setattr(get_config().gists, "max_per_request", 2)
    calls = []

    def declines_first(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise AIProviderError("Claude declined this request.", 422)
        return StructuredResult(data={"summary": "s", "keywords": []}, provider="anthropic", model="m", reasoning_effort="low")

    monkeypatch.setattr(gists, "structured_json", declines_first)
    first = client.post("/gists/generate").json()
    assert len(first["without_transcript"]) == 1 and len(first["generated"]) == 1 and first["failed"] == []
    second = client.post("/gists/generate").json()
    assert not set(second["generated"]) & (set(first["generated"]) | set(first["without_transcript"]))


def test_the_same_inputs_are_not_sent_for_a_recommendation_twice(user_id, no_gists, monkeypatch):
    target = client.get("/conversations").json()[0]["id"]
    fake = FakeModel(monkeypatch, next_steps, {"suggestions": [
        {"research_question_id": None, "suggested_direction": "Only a direction", "recommended_conversation_id": None, "rationale": "r"}]})
    with PrototypeSession() as db:
        db.add(ConversationGist(conversation_id=target, project_id=1, summary_text="summary", keywords_json="[]"))
        db.commit()
    assert client.post("/next_steps/recommend").json() == {"recommendation": None, "generated": True, "reason": None}
    assert client.post("/next_steps/recommend").json()["reason"] == "recent"
    monkeypatch.setattr(get_config().next_steps, "min_minutes_between_auto_runs", 0)
    assert client.post("/next_steps/recommend").json()["reason"] == "unchanged"
    assert len(fake.calls) == 1
