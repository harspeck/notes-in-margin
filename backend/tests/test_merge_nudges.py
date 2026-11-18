"""Merge nudges. Like test_code_families.py, the logic after the model call is tested by
feeding hand-built model output (invalid ids included) to `_apply_nudges`. The routes that
call the model run against a stand-in for `structured_json`; no test calls a model."""
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import MergeNudgesConfig, get_config
from app.db import PrototypeSession
from app.llm import AIProviderError, StructuredResult
from app.main import app
from app.models_prototype import InsightMerge, MergeNudge, ReflectionConnection, ResearchQuestion, RQMerge
from app.routers import merge_nudges as nudges_router
from app.services import merge_nudges as svc

client = TestClient(app)
_user_ids = itertools.count(96000)
_AI = {"model": "m", "reasoning_effort": "low", "prompt": "p"}
_LIMITS = MergeNudgesConfig(max_question_pairs_per_run=2, max_insight_pairs_per_run=2)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _rq(name: str, description: str | None = None) -> int:
    return client.post("/research_questions", json={"name": name, "description": description}).json()["id"]


def _insight(rq_id: int | None, text: str, evidence: list[dict] | None = None, conversation_id: int = 9) -> int:
    vm = client.post(f"/conversations/{conversation_id}/reflections", json={"text": "a reflection"}).json()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq_id,
                           "insight_text": text, "evidence": evidence or []}]}
    return client.post(f"/voice_memos/{vm['id']}/file", json=body).json()["connections"][0]["id"]


def _apply(user_id: int, question_pairs=(), insight_pairs=(), *, questions=(), insights=None, limits=_LIMITS,
           question_weight=None, insight_weight=None) -> list[MergeNudge]:
    with PrototypeSession() as db:
        created = svc._apply_nudges(
            db, user_id=user_id, raw_question_pairs=list(question_pairs), raw_insight_pairs=list(insight_pairs),
            valid_question_ids=set(questions), question_of_insight=insights or {},
            question_weight=question_weight or {}, insight_weight=insight_weight or {}, limits=limits, ai_metadata=_AI,
        )
        for n in created:
            db.refresh(n)
            db.expunge(n)
        return created


def _qp(a, b, reason="both gather passages about being known by name", wording="Where do people feel known?"):
    return {"question_a_id": a, "question_b_id": b, "reason": reason, "suggested_wording": wording}


def _ip(a, b, reason="they say nearly the same thing"):
    return {"connection_a_id": a, "connection_b_id": b, "reason": reason}


def _fake_model(monkeypatch, data):
    """Stands in for the model: records each call and answers with `data` (or data(payload))."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs["user"])
        return StructuredResult(data=data(payload) if callable(data) else data, provider="anthropic",
                                model="fake-model", reasoning_effort="low")

    monkeypatch.setattr(svc, "structured_json", fake)
    return calls


# --- _apply_nudges: hand-built model output ---------------------------------------------


def test_apply_keeps_only_pairs_of_real_distinct_ids_and_stores_them_as_suggested(user_id):
    created = _apply(
        user_id,
        question_pairs=[_qp(1, 2), _qp(1, 999_999), _qp(2, 2), _qp(3, 1, reason="  ")],
        insight_pairs=[_ip(10, 11), _ip(10, 12), _ip(10, 999_999), _ip(11, 11)],
        questions={1, 2, 3},
        insights={10: 1, 11: 1, 12: 2},  # 12 is under another question
    )
    assert [(n.kind, n.item_a_id, n.item_b_id) for n in created] == [("questions", 1, 2), ("insights", 10, 11)]
    question, insight = created
    assert all(n.status == "suggested" and json.loads(n.ai_metadata) == _AI and n.user_id == user_id for n in created)
    assert question.suggested_wording == "Where do people feel known?" and question.research_question_id is None
    assert insight.suggested_wording is None and insight.research_question_id == 1


def test_apply_never_repeats_a_pair_and_enforces_the_caps(user_id):
    first = _apply(user_id, question_pairs=[_qp(1, 2), _qp(2, 1)], questions={1, 2, 3, 4})
    assert len(first) == 1  # the same pair the other way round is the same pair
    # Still pending, kept separate or accepted: never suggested again, in either order.
    assert _apply(user_id, question_pairs=[_qp(2, 1)], questions={1, 2, 3, 4}) == []
    with PrototypeSession() as db:
        db.get(MergeNudge, first[0].id).status = "dismissed"
        db.commit()
    assert _apply(user_id, question_pairs=[_qp(1, 2)], questions={1, 2, 3, 4}) == []

    capped = _apply(user_id, question_pairs=[_qp(1, 3), _qp(1, 4), _qp(3, 4)], questions={1, 2, 3, 4})
    assert [(n.item_a_id, n.item_b_id) for n in capped] == [(1, 3), (1, 4)]  # max_question_pairs_per_run: 2
    insights = {i: 7 for i in (20, 21, 22, 23)}
    capped = _apply(user_id, insight_pairs=[_ip(20, 21), _ip(999, 20), _ip(22, 23), _ip(20, 22)], insights=insights)
    assert [(n.item_a_id, n.item_b_id) for n in capped] == [(20, 21), (22, 23)]  # invalid ones don't use the cap


def test_apply_skips_pairs_the_user_already_merged_by_hand(user_id):
    q1, q2, q3 = _rq("hand a"), _rq("hand b"), _rq("hand c")
    i1, i2, i3 = _insight(q3, "one"), _insight(q3, "two"), _insight(q3, "three")
    with PrototypeSession() as db:
        db.add(RQMerge(user_id=user_id, source_rq_id=q1, target_rq_id=q2, status="undone"))  # merged, then undone
        db.add(InsightMerge(user_id=user_id, source_connection_id=i1, target_connection_id=i2,
                            source_previous_status="accepted", status="active"))
        db.commit()
    created = _apply(user_id, question_pairs=[_qp(q2, q1), _qp(q1, q3)], insight_pairs=[_ip(i2, i1), _ip(i1, i3)],
                     questions={q1, q2, q3}, insights={i1: q3, i2: q3, i3: q3})
    assert [(n.item_a_id, n.item_b_id) for n in created] == [(q1, q3), (i1, i3)]


def test_apply_picks_the_heavier_side_as_the_default_target(user_id):
    created = _apply(user_id, question_pairs=[_qp(1, 2), _qp(3, 4)], questions={1, 2, 3, 4},
                     question_weight={1: 0, 2: 5, 3: 2, 4: 2})
    assert [n.default_target_id for n in created] == [2, 3]  # more insights wins; a tie goes to the first


# --- generate ---------------------------------------------------------------------------


def test_generate_sends_the_users_questions_and_stores_validated_nudges(user_id, monkeypatch):
    a, b = _rq("Where do newcomers first feel known?"), _rq("Belonging", "where people feel they belong")
    archived = _rq("archived question")
    client.patch(f"/research_questions/{archived}", json={"archived": True})
    i1, i2 = _insight(b, "the library knows people by name"), _insight(b, "librarians greet you by name")
    other = _insight(a, "newcomers start at the library")
    calls = _fake_model(monkeypatch, {
        "question_pairs": [_qp(a, b), _qp(a, archived)],
        "insight_pairs": [_ip(i1, i2), _ip(i1, other)],  # the second crosses questions
    })

    r = client.post("/merge_nudges/generate")
    assert r.status_code == 200, r.text
    (call,) = calls
    payload = json.loads(call["user"])
    assert call["system"] == svc.MERGE_NUDGE_PROMPT and call["name"] == "merge_nudges"
    assert [q["id"] for q in payload["research_questions"]] == [a, b]  # the archived one is not sent
    assert payload["research_questions"][1] == {
        "id": b, "name": "Belonging", "description": "where people feel they belong",
        "insights": [{"connection_id": i1, "insight_text": "the library knows people by name"},
                     {"connection_id": i2, "insight_text": "librarians greet you by name"}]}
    limits = get_config().merge_nudges
    assert (payload["max_question_pairs"], payload["max_insight_pairs"]) == (
        limits.max_question_pairs_per_run, limits.max_insight_pairs_per_run)

    created = r.json()
    assert [(n["kind"], [i["id"] for i in n["items"]]) for n in created] == [("questions", [a, b]), ("insights", [i1, i2])]
    question, insight = created
    assert question["status"] == "suggested" and question["ai_metadata"]["model"] == "fake-model"
    assert question["ai_metadata"]["prompt"] == svc.MERGE_NUDGE_PROMPT
    assert question["default_target_id"] == b  # Belonging holds more insights
    assert question["items"][1] == {"id": b, "name": "Belonging", "description": "where people feel they belong",
                                    "insight_count": 2, "highlight_count": 0}
    assert question["suggested_wording"] and question["merged_preview"] is None
    assert insight["research_question_id"] == b and insight["research_question_name"] == "Belonging"
    assert insight["items"][0]["insight_text"] == "the library knows people by name"
    assert insight["items"][0]["conversation_id"] == 9 and insight["items"][0]["conversation_title"]
    assert insight["merged_preview"] == {"evidence_count": 0, "conversation_count": 1}
    assert client.get("/merge_nudges").json() == created

    # A second run is told what is already decided, and repeats are dropped whatever it says.
    assert client.post("/merge_nudges/generate").json() == []
    again = json.loads(calls[1]["user"])
    assert again["already_decided_question_pairs"] == [sorted([a, b])]
    assert again["already_decided_insight_pairs"] == [sorted([i1, i2])]
    activity = client.get("/activity?filter=ai").json()["items"]
    assert [(x["kind"], x["refs"]["count"]) for x in activity] == [("merge_nudges_suggested", 0), ("merge_nudges_suggested", 2)]

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.get("/merge_nudges").json() == []
    assert client.post(f"/merge_nudges/{question['id']}/accept").status_code == 404
    assert client.post(f"/merge_nudges/{question['id']}/dismiss").status_code == 404


def test_generate_spends_no_call_when_nothing_could_pair_and_fails_cleanly(user_id, monkeypatch):
    calls = _fake_model(monkeypatch, {"question_pairs": [], "insight_pairs": []})
    only = _rq("the only question")
    _insight(only, "the only insight")
    assert client.post("/merge_nudges/generate").json() == [] and calls == []

    _rq("a second question")

    def refuse(**_):
        raise AIProviderError("The model is overloaded. Try again in a moment.", 503)

    monkeypatch.setattr(svc, "structured_json", refuse)
    r = client.post("/merge_nudges/generate")
    assert r.status_code == 503 and r.json() == {"detail": "The model is overloaded. Try again in a moment.", "source": "ai"}
    with PrototypeSession() as db:
        assert db.query(MergeNudge).filter_by(user_id=user_id).count() == 0


# --- accept and dismiss -----------------------------------------------------------------


def _nudge(user_id: int, kind: str, a: int, b: int, **extra) -> int:
    with PrototypeSession() as db:
        n = MergeNudge(user_id=user_id, kind=kind, item_a_id=a, item_b_id=b, default_target_id=extra.pop("target", a),
                       reason="they look alike", status="suggested", ai_metadata=json.dumps(_AI), **extra)
        db.add(n)
        db.commit()
        return n.id


def test_accepting_a_question_nudge_merges_through_merge_rq(user_id, monkeypatch):
    a, b = _rq("newcomers"), _rq("belonging")
    nudge = _nudge(user_id, "questions", a, b, target=b, suggested_wording="Where do people come to belong?")
    spied = []
    real = nudges_router.merge_rq
    monkeypatch.setattr(nudges_router, "merge_rq", lambda *args, **kw: spied.append(
        (kw["source"].id, kw["target"].id)) or real(*args, **kw))

    assert client.post(f"/merge_nudges/{nudge}/accept", json={"target_id": 999}).status_code == 422
    r = client.post(f"/merge_nudges/{nudge}/accept", json={"apply_suggested_wording": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["kind"], body["source_id"], body["target_id"]) == ("accepted", "questions", a, b)
    assert (body["merge"]["source_rq_id"], body["merge"]["target_rq_id"], body["merge"]["status"]) == (a, b, "active")
    assert spied == [(a, b)]  # the by-hand function, called once
    by_id = {q["id"]: q for q in client.get("/research_questions?include_archived=true").json()}
    assert by_id[a]["merged_into"] == b and by_id[b]["name"] == "Where do people come to belong?"
    with PrototypeSession() as db:
        assert db.get(MergeNudge, nudge).result_merge_id == body["merge"]["id"]

    assert client.post(f"/merge_nudges/{nudge}/accept").status_code == 409  # already accepted
    assert client.post(f"/merge_nudges/{nudge}/dismiss").status_code == 409
    assert client.get("/merge_nudges").json() == []
    # Undo is the ordinary question-merge undo.
    assert client.post(f"/rq_merges/{body['merge']['id']}/undo").status_code == 200
    kinds = [i["kind"] for i in client.get("/activity?filter=changed").json()["items"]]
    assert "rq_merged" in kinds and "rq_reworded" in kinds


def test_accepting_an_insight_nudge_merges_through_merge_insights_and_the_body_picks_the_target(user_id, monkeypatch):
    rq = _rq("insight nudge")
    snippets = [s["snippet_id"] for s in client.get("/conversations/9").json()["snippets"] if len(s["words"]) >= 12]
    a = _insight(rq, "trust erodes through access decisions", [{"snippet_id": snippets[0], "start_index": 0, "end_index": 4}])
    b = _insight(rq, "daytime meetings keep people out", [{"snippet_id": snippets[1], "start_index": 0, "end_index": 4}])
    nudge = _nudge(user_id, "insights", a, b, research_question_id=rq)
    (listed,) = client.get("/merge_nudges").json()
    assert listed["merged_preview"] == {"evidence_count": 2, "conversation_count": 1}
    assert [i["evidence_count"] for i in listed["items"]] == [1, 1]

    spied = []
    real = nudges_router.merge_insights
    monkeypatch.setattr(nudges_router, "merge_insights", lambda *args, **kw: spied.append(
        (kw["source"].id, kw["target"].id)) or real(*args, **kw))
    r = client.post(f"/merge_nudges/{nudge}/accept", json={"target_id": b})  # not the default side
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["source_id"], body["target_id"], body["merge"]["kind"]) == (a, b, "insights")
    assert body["merge"]["added_evidence_count"] == 1
    assert spied == [(a, b)]
    with PrototypeSession() as db:
        assert (db.get(ReflectionConnection, a).status, db.get(ReflectionConnection, b).status) == ("merged", "accepted")
    assert client.post(f"/insight_merges/{body['merge']['id']}/undo").status_code == 200


def test_accept_is_409_when_either_side_is_no_longer_live_and_the_list_hides_it(user_id):
    a, b, c = _rq("stale a"), _rq("stale b"), _rq("stale c")
    question_nudge = _nudge(user_id, "questions", a, b)
    x, y = _insight(c, "x"), _insight(c, "y")
    insight_nudge = _nudge(user_id, "insights", x, y, research_question_id=c)
    assert len(client.get("/merge_nudges").json()) == 2

    client.patch(f"/research_questions/{a}", json={"archived": True})
    client.post(f"/memo/connections/{y}/retract")
    assert client.get("/merge_nudges").json() == []
    assert client.post(f"/merge_nudges/{question_nudge}/accept").status_code == 409
    assert client.post(f"/merge_nudges/{insight_nudge}/accept").status_code == 409
    with PrototypeSession() as db:
        assert {db.get(MergeNudge, n).status for n in (question_nudge, insight_nudge)} == {"suggested"}
        assert db.get(ResearchQuestion, b).archived is False
        assert db.get(ReflectionConnection, x).status == "accepted"


def test_dismiss_keeps_them_separate_for_good(user_id, monkeypatch):
    a, b = _rq("keep a"), _rq("keep b")
    nudge = _nudge(user_id, "questions", a, b)
    assert client.post(f"/merge_nudges/{nudge}/dismiss").json() == {"id": nudge, "status": "dismissed"}
    assert client.post(f"/merge_nudges/{nudge}/dismiss").json()["status"] == "dismissed"  # idempotent
    assert client.post(f"/merge_nudges/{nudge}/accept").status_code == 409
    assert client.get("/merge_nudges").json() == []
    set_aside = client.get("/activity?filter=set_aside").json()["items"]
    assert [i["kind"] for i in set_aside] == ["merge_nudge_dismissed"]
    assert "keep a" in set_aside[0]["summary"] and "keep b" in set_aside[0]["summary"]

    _fake_model(monkeypatch, {"question_pairs": [_qp(b, a)], "insight_pairs": []})
    assert client.post("/merge_nudges/generate").json() == []
    with PrototypeSession() as db:
        assert db.query(MergeNudge).filter_by(user_id=user_id).count() == 1
