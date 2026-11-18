"""Find more passages and What am I missing. The logic after each model call is tested with
hand-built model output (invalid ids and ranges included), as in test_code_families.py. The
routes run against a stand-in for `structured_json`; no test calls a model."""
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.cortico.source import SnapshotSource
from app.db import CorticoSession, PrototypeSession
from app.evidence_provenance import HighlightSpan
from app.llm import AIProviderError, StructuredResult
from app.main import app
from app.models_prototype import (
    ConversationGist,
    HighlightQuestion,
    NextStepSuggestion,
    PassageSuggestion,
    QuestionGap,
    RLHighlight,
)
from app.services import find_passages as passages_svc
from app.services import question_gaps as gaps_svc

client = TestClient(app)
_user_ids = itertools.count(97000)
_AI = {"model": "m", "reasoning_effort": "low", "prompt": "p"}


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _rq(name: str, description: str | None = None) -> int:
    return client.post("/research_questions", json={"name": name, "description": description}).json()["id"]


def _snippets(conversation_id: int) -> list[dict]:
    snippets = client.get(f"/conversations/{conversation_id}").json()["snippets"]
    return [s for s in snippets if not s["is_facilitator"] and len(s["words"]) >= 20]


def _fake_model(monkeypatch, module, data):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs["user"])
        return StructuredResult(data=data(payload) if callable(data) else data, provider="anthropic",
                                model="fake-model", reasoning_effort="low")

    monkeypatch.setattr(module, "structured_json", fake)
    return calls


def _passage(snippet_id, start, end, reason="speaks to the question"):
    return {"snippet_id": snippet_id, "start_index": start, "end_index": end, "reason": reason}


# --- _apply_passages: hand-built model output ----------------------------------------------


def test_apply_passages_checks_every_span_against_the_real_transcript(user_id):
    rq = _rq("apply passages")
    cortico = CorticoSession()
    try:
        by_id = {s.id: s for s in SnapshotSource(cortico).snippets(12)}
        long = [s for s in by_id.values() if len(s.words) >= 20]
        s1, s2, s3 = long[:3]
        from_another_conversation = SnapshotSource(cortico).snippets(13)[2]
    finally:
        cortico.close()
    n = len(s1.words)
    raw = [
        _passage(s1.id, 0, 5),  # kept
        _passage(999_999_999, 0, 3),  # no such snippet
        _passage(from_another_conversation.id, 0, 3),  # a real snippet, but not this conversation's
        _passage(s1.id, 5, 5),  # empty range
        _passage(s1.id, 9, 6),  # backwards
        _passage(s1.id, -1, 3),  # negative start
        _passage(s1.id, n - 2, n + 1),  # runs past the last word
        _passage(s1.id, n - 3, n),  # kept: end_index is exclusive, so the word count itself is allowed
        _passage(s1.id, 3, 8),  # overlaps the first passage of this same output
        _passage(s2.id, 4, 9),  # overlaps the user's highlight [6, 12)
        _passage(s2.id, 12, 15),  # kept: touches the highlight's end without sharing a word
        _passage(s3.id, 2, 6),  # overlaps an earlier suggestion for this question
        {"snippet_id": s3.id, "start_index": "10", "end_index": 14, "reason": "not integers"},
    ]
    with PrototypeSession() as db:
        created = passages_svc._apply_passages(
            db, user_id=user_id, research_question_id=rq, conversation_id=12, raw_passages=raw, snippets=by_id,
            user_highlight_spans=[HighlightSpan(1, False, s2.id, 6, 12)], known_spans=[(s3.id, 0, 4)],
            max_passages=10, ai_metadata=_AI,
        )
        assert [(p.snippet_id, p.start_index, p.end_index) for p in created] == [
            (s1.id, 0, 5), (s1.id, n - 3, n), (s2.id, 12, 15)]
        assert all(p.status == "suggested" and json.loads(p.ai_metadata) == _AI and p.highlight_id is None for p in created)
        assert all((p.user_id, p.research_question_id, p.conversation_id) == (user_id, rq, 12) for p in created)

        capped = passages_svc._apply_passages(
            db, user_id=user_id, research_question_id=rq, conversation_id=12,
            raw_passages=[_passage(s3.id, 10, 12), _passage(s3.id, 12, 14), _passage(s3.id, 14, 16)],
            snippets=by_id, user_highlight_spans=[], known_spans=[], max_passages=2, ai_metadata=_AI,
        )
        assert len(capped) == 2


# --- find_passages -------------------------------------------------------------------------


def test_find_passages_in_one_conversation_suggests_and_accept_makes_a_filed_highlight(user_id, monkeypatch):
    rq = _rq("Where do people feel known?", "places people are recognised")
    s1, s2 = _snippets(12)[:2]
    mine = client.post("/conversations/12/highlights",
                       json={"snippets": [{"snippet_id": s2["snippet_id"], "start_index": 0, "end_index": 6}]}).json()
    calls = _fake_model(monkeypatch, passages_svc, {"passages": [
        _passage(s1["snippet_id"], 2, 9, "names the library as the place"),
        _passage(s2["snippet_id"], 3, 8, "overlaps a highlight the user already has"),
        _passage(999_999_999, 0, 4, "an invented snippet"),
    ]})

    r = client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    (call,) = calls
    payload = json.loads(call["user"])
    assert call["system"] == passages_svc.FIND_PASSAGES_PROMPT and call["name"] == "find_passages"
    assert payload["research_question"] == {"name": "Where do people feel known?",
                                            "description": "places people are recognised", "insights": []}
    assert payload["conversation"]["id"] == 12 and payload["max_passages"] == get_config().find_passages.max_passages_per_conversation
    first = payload["transcript_snippets"][0]
    assert set(first) == {"snippet_id", "speaker", "is_facilitator", "text", "word_count"}
    assert payload["already_highlighted"] == [{"snippet_id": s2["snippet_id"], "start_index": 0, "end_index": 6}]

    assert (body["research_question_id"], body["searched_conversation_ids"], body["failed"]) == (rq, [12], [])
    (suggestion,) = body["suggestions"]
    assert (suggestion["snippet_id"], suggestion["start_index"], suggestion["end_index"]) == (s1["snippet_id"], 2, 9)
    assert suggestion["text"] == " ".join(w["word"] for w in s1["words"][2:9])
    assert (suggestion["conversation_id"], suggestion["speaker_name"]) == (12, s1["speaker_name"])
    assert suggestion["conversation_title"] and suggestion["time_seconds"] == s1["words"][2]["start"]
    assert suggestion["reason"] == "names the library as the place" and suggestion["status"] == "suggested"
    assert suggestion["ai_metadata"]["model"] == "fake-model" and suggestion["highlight_id"] is None

    listed = client.get(f"/research_questions/{rq}/passage_suggestions").json()
    assert listed == {"research_question_id": rq, "suggestions": [suggestion]}
    assert client.get(f"/research_questions/{rq}/passage_suggestions?conversation_id=9").json()["suggestions"] == []
    with PrototypeSession() as db:  # a suggestion is not a highlight until the user accepts it
        assert db.query(RLHighlight).filter_by(user_id=user_id).count() == 1

    accepted = client.post(f"/passage_suggestions/{suggestion['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    highlight = accepted.json()["highlight"]
    assert highlight["conversation_id"] == 12 and highlight["research_question_ids"] == [rq]
    assert highlight["snippets"] == [{"snippet_id": s1["snippet_id"], "start_index": 2, "end_index": 9}]
    assert highlight["cortico_sync"] is None and highlight["notes"] is None  # snapshot mode: no write-back
    assert accepted.json()["suggestion"]["status"] == "accepted"
    assert accepted.json()["suggestion"]["highlight_id"] == highlight["id"]
    with PrototypeSession() as db:
        row = db.get(RLHighlight, highlight["id"])
        assert (row.user_id, row.project_id) == (user_id, client.get("/conversations/12").json()["project_id"])
        assert db.query(HighlightQuestion).filter_by(highlight_id=row.id, research_question_id=rq).count() == 1
    assert {h["id"] for h in client.get("/conversations/12/highlights").json()} == {mine["id"], highlight["id"]}
    assert [h["highlight_id"] for h in client.get(f"/research_questions/{rq}/highlights").json()["highlights"]] == [
        highlight["id"]]

    assert client.post(f"/passage_suggestions/{suggestion['id']}/accept").status_code == 409  # never twice
    assert client.post(f"/passage_suggestions/{suggestion['id']}/dismiss").status_code == 409
    assert client.get(f"/research_questions/{rq}/passage_suggestions").json()["suggestions"] == []
    with PrototypeSession() as db:
        assert db.query(RLHighlight).filter_by(user_id=user_id).count() == 2

    kinds = [i["kind"] for i in client.get("/activity").json()["items"]]
    assert "passages_suggested" in kinds and "passage_accepted" in kinds

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.post(f"/passage_suggestions/{suggestion['id']}/accept").status_code == 404
    assert client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 12}).status_code == 404
    assert client.get(f"/research_questions/{rq}/passage_suggestions").status_code == 404


def test_find_passages_without_a_conversation_searches_the_most_recently_opened_up_to_the_cap(user_id, monkeypatch):
    rq = _rq("recently opened")
    cap = get_config().find_passages.max_conversations_per_run
    assert cap == 3
    for conversation_id in (12, 13, 160, 14, 15, 16):  # 160 has no transcript
        assert client.put(f"/conversations/{conversation_id}/reading_position").status_code == 200

    def answer(payload):
        snippet = next(s for s in payload["transcript_snippets"] if s["word_count"] >= 10)
        return {"passages": [_passage(snippet["snippet_id"], 0, 4)]}

    calls = _fake_model(monkeypatch, passages_svc, answer)
    body = client.post(f"/research_questions/{rq}/find_passages").json()
    assert body["searched_conversation_ids"] == [16, 15, 14]  # newest opened first, one call each
    assert [json.loads(c["user"])["conversation"]["id"] for c in calls] == [16, 15, 14]
    assert sorted(s["conversation_id"] for s in body["suggestions"]) == [14, 15, 16]

    # A dismissed passage is not suggested again, and a conversation with no transcript costs no call.
    dismissed = body["suggestions"][0]
    assert client.post(f"/passage_suggestions/{dismissed['id']}/dismiss").json() == {"id": dismissed["id"], "status": "dismissed"}
    assert client.post(f"/passage_suggestions/{dismissed['id']}/accept").status_code == 409
    client.put("/conversations/160/reading_position")
    again = client.post(f"/research_questions/{rq}/find_passages").json()
    assert again["searched_conversation_ids"] == [16, 15, 14] and again["suggestions"] == []
    assert client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 160}).json()[
        "searched_conversation_ids"] == []
    assert len(calls) == 6
    assert client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 999_999_999}).status_code == 404
    assert "passage_dismissed" in [i["kind"] for i in client.get("/activity?filter=set_aside").json()["items"]]


def test_find_passages_with_nothing_opened_makes_no_call_and_a_model_failure_is_a_clean_error(user_id, monkeypatch):
    rq = _rq("nothing opened")
    calls = _fake_model(monkeypatch, passages_svc, {"passages": []})
    assert client.post(f"/research_questions/{rq}/find_passages").json() == {
        "research_question_id": rq, "searched_conversation_ids": [], "failed": [], "suggestions": []}
    assert calls == []

    def refuse(**_):
        raise AIProviderError("The model declined this request.", 422)

    monkeypatch.setattr(passages_svc, "structured_json", refuse)
    r = client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 12})
    assert r.status_code == 422 and r.json() == {"detail": "The model declined this request.", "source": "ai"}

    client.patch(f"/research_questions/{rq}", json={"archived": True})
    assert client.post(f"/research_questions/{rq}/find_passages", json={"conversation_id": 12}).status_code == 409


def test_find_passages_keeps_earlier_results_when_a_later_call_fails(user_id, monkeypatch):
    rq = _rq("partial failure")
    for conversation_id in (12, 13):
        client.put(f"/conversations/{conversation_id}/reading_position")
    seen = []

    def flaky(**kwargs):
        payload = json.loads(kwargs["user"])
        seen.append(payload["conversation"]["id"])
        if len(seen) == 2:
            raise AIProviderError("The model is overloaded. Try again in a moment.", 503)
        snippet = next(s for s in payload["transcript_snippets"] if s["word_count"] >= 10)
        return StructuredResult(data={"passages": [_passage(snippet["snippet_id"], 0, 4)]}, provider="anthropic",
                                model="fake-model", reasoning_effort="low")

    monkeypatch.setattr(passages_svc, "structured_json", flaky)
    r = client.post(f"/research_questions/{rq}/find_passages")
    assert r.status_code == 200
    body = r.json()
    assert body["searched_conversation_ids"] == [13] and len(body["suggestions"]) == 1
    assert body["failed"] == [{"conversation_id": 12, "detail": "The model is overloaded. Try again in a moment."}]


# --- _apply_gaps: hand-built model output --------------------------------------------------


def test_apply_gaps_drops_unknown_conversations_and_empty_observations_and_caps(user_id):
    rq = _rq("apply gaps")
    raw = [
        {"kind": "perspective", "observation": " Nobody under 30 has spoken to this yet. ",
         "conversation_ids": [14, 999_999_999, 9, 14]},  # 9 was opened; 999999999 does not exist
        {"kind": "counter_view", "observation": "   ", "conversation_ids": [14]},
        {"kind": "something_else", "observation": "Only one conversation backs this.", "conversation_ids": []},
        {"kind": "thin_spot", "observation": "Past the cap.", "conversation_ids": [15]},
    ]
    with PrototypeSession() as db:
        created = gaps_svc._apply_gaps(
            db, user_id=user_id, research_question_id=rq, raw_observations=raw,
            valid_conversation_ids={14, 15}, max_observations=2, ai_metadata=_AI,
        )
        assert [(g.gap_kind, g.observation, json.loads(g.conversation_ids_json)) for g in created] == [
            ("perspective", "Nobody under 30 has spoken to this yet.", [14]),
            ("thin_spot", "Only one conversation backs this.", []),
        ]
        assert all(g.status == "suggested" and json.loads(g.ai_metadata) == _AI and g.user_id == user_id for g in created)


# --- whats_missing --------------------------------------------------------------------------


def test_whats_missing_shows_the_model_what_was_heard_and_what_was_not_and_works_without_gists(user_id, monkeypatch):
    rq = _rq("Trust in local institutions", "who people rely on")
    (s9, *_) = _snippets(9)
    vm = client.post("/conversations/9/reflections", json={"text": "a reflection"}).json()
    client.post(f"/voice_memos/{vm['id']}/file", json={"decisions": [
        {"connection_id": None, "action": "accept", "research_question_id": rq, "insight_text": "trust is lost quietly"}]})
    h = client.post("/conversations/12/highlights", json={
        "notes": "about the library",
        "snippets": [{"snippet_id": _snippets(12)[0]["snippet_id"], "start_index": 0, "end_index": 5}]}).json()
    client.put(f"/highlights/{h['id']}/questions", json={"research_question_ids": [rq]})
    with PrototypeSession() as db:  # one unopened conversation has a cached gist; the rest have none
        if db.get(ConversationGist, 20) is None:
            db.add(ConversationGist(conversation_id=20, project_id=1, summary_text="volunteer firefighters",
                                    keywords_json=json.dumps(["fire department"])))
            db.commit()

    calls = _fake_model(monkeypatch, gaps_svc, {"observations": [
        {"kind": "counter_view", "observation": "Every insight is about trust being lost. One unopened conversation describes it being earned.",
         "conversation_ids": [20, 9, 999_999_999]},  # 9 is already opened, the last is invented
        {"kind": "thin_spot", "observation": "Only one conversation backs this question so far.", "conversation_ids": []},
    ]})
    r = client.post(f"/research_questions/{rq}/whats_missing")
    assert r.status_code == 200, r.text
    (call,) = calls
    payload = json.loads(call["user"])
    assert call["system"] == gaps_svc.WHATS_MISSING_PROMPT and call["name"] == "question_gaps"
    assert payload["research_question"] == {"name": "Trust in local institutions", "description": "who people rely on"}
    assert payload["insights"] == ["trust is lost quietly"]
    (quote,) = payload["highlight_quotes"]
    assert quote["conversation_id"] == 12 and quote["note"] == "about the library" and len(quote["text"].split()) == 5
    assert sorted(c["conversation_id"] for c in payload["conversations_heard"]) == [9, 12]
    unopened = {c["conversation_id"]: c for c in payload["unopened_conversations"]}
    assert 9 not in unopened and 12 not in unopened
    limits = get_config().whats_missing
    assert len(unopened) == min(199, limits.max_unopened_conversations)
    assert payload["unopened_conversations"][0]["conversation_id"] == 20  # the one with a gist is kept first
    assert unopened[20]["summary"] == "volunteer firefighters" and unopened[20]["keywords"] == ["fire department"]
    without = next(c for c in payload["unopened_conversations"] if c["conversation_id"] != 20)
    assert set(without) == {"conversation_id", "title", "project_id"}  # no gist: title only
    assert payload["max_observations"] == limits.max_observations

    gaps = r.json()["gaps"]
    assert [(g["kind"], [c["conversation_id"] for c in g["conversations"]]) for g in gaps] == [
        ("counter_view", [20]), ("thin_spot", [])]
    named = gaps[0]["conversations"][0]
    assert named["title"] and named["in_listen_next"] is False and named["next_step_id"] is None
    assert gaps[0]["status"] == "suggested" and gaps[0]["ai_metadata"]["model"] == "fake-model"
    listed = client.get(f"/research_questions/{rq}/gaps").json()
    assert listed["research_question_id"] == rq and [g["id"] for g in listed["gaps"]] == [gaps[1]["id"], gaps[0]["id"]]

    # A second run is told what was already said.
    client.post(f"/research_questions/{rq}/whats_missing")
    assert json.loads(calls[1]["user"])["earlier_observations"] == [g["observation"] for g in gaps]

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.get(f"/research_questions/{rq}/gaps").status_code == 404
    assert client.post(f"/research_questions/{rq}/whats_missing").status_code == 404
    assert client.post(f"/question_gaps/{gaps[0]['id']}/dismiss").status_code == 404
    assert client.post(f"/question_gaps/{gaps[0]['id']}/listen_next").status_code == 404


def test_whats_missing_fails_cleanly(user_id, monkeypatch):
    rq = _rq("gaps fail cleanly")

    def cut_off(**_):
        raise AIProviderError("The model's answer was cut off.", 502)

    monkeypatch.setattr(gaps_svc, "structured_json", cut_off)
    r = client.post(f"/research_questions/{rq}/whats_missing")
    assert r.status_code == 502 and r.json() == {"detail": "The model's answer was cut off.", "source": "ai"}
    assert client.get(f"/research_questions/{rq}/gaps").json()["gaps"] == []


def _gap(user_id: int, rq: int, conversation_ids: list[int], observation="A group not heard from yet.") -> int:
    with PrototypeSession() as db:
        g = QuestionGap(user_id=user_id, research_question_id=rq, gap_kind="perspective", observation=observation,
                        conversation_ids_json=json.dumps(conversation_ids), status="suggested",
                        ai_metadata=json.dumps(_AI))
        db.add(g)
        db.commit()
        return g.id


def test_listen_next_adds_next_steps_once_per_conversation(user_id):
    rq = _rq("Belonging")
    gap = _gap(user_id, rq, [30, 31])
    with PrototypeSession() as db:  # 31 was suggested and dismissed before: it may be added again
        db.add(NextStepSuggestion(user_id=user_id, recommended_conversation_id=31, status="dismissed"))
        db.commit()

    assert client.post(f"/question_gaps/{gap}/listen_next", json={"conversation_ids": [32]}).status_code == 422
    r = client.post(f"/question_gaps/{gap}/listen_next", json={"conversation_ids": [30]})
    assert r.status_code == 200, r.text
    (step,) = r.json()["next_steps"]
    assert step["created"] is True and step["status"] == "suggested"  # the status the Listen next list shows
    assert (step["recommended_conversation_id"], step["research_question_id"]) == (30, rq)
    assert step["recommended_conversation_title"] and step["suggested_direction"] == "A group not heard from yet."
    assert "Belonging" in step["rationale"]
    assert step["ai_metadata"] == {**_AI, "origin": "question_gap", "question_gap_id": gap}
    assert [(c["conversation_id"], c["in_listen_next"]) for c in r.json()["gap"]["conversations"]] == [(30, True), (31, False)]
    assert step["id"] in [s["id"] for s in client.get("/next_steps").json()]

    # Without a body: every named conversation. 30 is already listed, so only 31 is created.
    both = client.post(f"/question_gaps/{gap}/listen_next").json()["next_steps"]
    assert [(s["recommended_conversation_id"], s["created"]) for s in both] == [(30, False), (31, True)]
    assert both[0]["id"] == step["id"]
    with PrototypeSession() as db:
        live = db.query(NextStepSuggestion).filter(NextStepSuggestion.user_id == user_id,
                                                   NextStepSuggestion.status != "dismissed").all()
        assert sorted(s.recommended_conversation_id for s in live) == [30, 31]
    assert client.post(f"/question_gaps/{gap}/listen_next").json()["next_steps"][1]["created"] is False
    listed = [i for i in client.get("/activity").json()["items"] if i["kind"] == "question_gap_listen_next"]
    assert [i["refs"]["conversation_ids"] for i in listed] == [[31], [30]]  # nothing logged when nothing was added

    nothing_named = _gap(user_id, rq, [])
    assert client.post(f"/question_gaps/{nothing_named}/listen_next").status_code == 409


def test_dismissing_a_gap_is_a_status_change(user_id):
    rq = _rq("dismiss a gap")
    gap = _gap(user_id, rq, [30])
    assert client.post(f"/question_gaps/{gap}/dismiss").json() == {"id": gap, "status": "dismissed"}
    assert client.post(f"/question_gaps/{gap}/dismiss").json()["status"] == "dismissed"
    assert client.get(f"/research_questions/{rq}/gaps").json()["gaps"] == []
    assert client.post(f"/question_gaps/{gap}/listen_next").status_code == 409
    with PrototypeSession() as db:
        assert db.get(QuestionGap, gap).status == "dismissed"
    assert [i["kind"] for i in client.get("/activity?filter=set_aside").json()["items"]] == ["question_gap_dismissed"]


def test_suggestions_and_gaps_read_under_a_merged_questions_target(user_id):
    source, target = _rq("merged source"), _rq("merged target")
    snippet = _snippets(12)[0]
    gap = _gap(user_id, source, [])
    with PrototypeSession() as db:
        p = PassageSuggestion(user_id=user_id, research_question_id=source, conversation_id=12,
                              snippet_id=snippet["snippet_id"], start_index=0, end_index=4, reason="r",
                              status="suggested", ai_metadata=json.dumps(_AI))
        db.add(p)
        db.commit()
        passage = p.id
    assert client.post(f"/research_questions/{source}/merge_into", json={"target_id": target}).status_code == 200

    for asked in (source, target):
        gaps = client.get(f"/research_questions/{asked}/gaps").json()
        assert gaps["research_question_id"] == target and [g["id"] for g in gaps["gaps"]] == [gap]
        assert gaps["gaps"][0]["research_question_id"] == target
        passages = client.get(f"/research_questions/{asked}/passage_suggestions").json()
        assert [s["id"] for s in passages["suggestions"]] == [passage]
    # Accepting files the new highlight under the target, the question that holds it now.
    highlight = client.post(f"/passage_suggestions/{passage}/accept").json()["highlight"]
    assert highlight["research_question_ids"] == [target]
