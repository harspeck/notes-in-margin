"""Guards found by the pre-merge review: one AI review per reflection, model-returned
question ids, cited highlights, and insight merges that share evidence."""
import itertools

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import PrototypeSession
from app.llm import AIProviderError, StructuredResult
from app.main import app
from app.models_prototype import ConnectionEvidence, ReflectionConnection, ReviewRun, VoiceMemo
from app.services import ai_review

client = TestClient(app)
_user_ids = itertools.count(97000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _as(uid):
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)


def _conversation():
    conversation = client.get("/conversations").json()[0]["id"]
    first = client.get(f"/conversations/{conversation}").json()["snippets"][0]
    snippet = first.get("snippet_id", first.get("id"))
    return conversation, snippet


def _reflection(conversation):
    r = client.post(f"/conversations/{conversation}/reflections", json={"text": "A thought worth keeping."})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _file(vm, rq, text, evidence):
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq,
                           "insight_text": text, "evidence": evidence}]}
    r = client.post(f"/voice_memos/{vm}/file", json=body)
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["connections"] if c["insight_text"] == text)


def _ev(snippet, a=0, b=2):
    return {"snippet_id": snippet, "start_index": a, "end_index": b}


def test_a_reflection_is_reviewed_by_the_ai_once(user_id, monkeypatch):
    conversation, _ = _conversation()
    vm = _reflection(conversation)
    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        return StructuredResult(provider="anthropic", model="m", reasoning_effort="low", data={
            "connections": [{"research_question_id": None, "insight_text": "one insight", "evidence": []}],
            "extracted_interests": []})

    monkeypatch.setattr(ai_review, "structured_json", model)
    first = client.post(f"/voice_memos/{vm}/review")
    assert first.status_code == 200, first.text
    second = client.post(f"/voice_memos/{vm}/review")
    assert second.status_code == 200
    assert len(calls) == 1
    assert [c["id"] for c in second.json()["connections"]] == [c["id"] for c in first.json()["connections"]]


def test_a_review_already_running_answers_409_and_a_failed_one_can_be_retried(user_id, monkeypatch):
    conversation, _ = _conversation()
    vm = _reflection(conversation)
    calls = []

    def fails(**kwargs):
        calls.append(kwargs)
        raise AIProviderError("The AI is busy.", 503)

    monkeypatch.setattr(ai_review, "structured_json", fails)
    assert client.post(f"/voice_memos/{vm}/review").status_code == 503
    assert client.post(f"/voice_memos/{vm}/review").status_code == 503  # failed, so tried again
    assert len(calls) == 2
    with PrototypeSession() as db:
        db.get(ReviewRun, vm).status = "running"  # another request is in the middle of it
        db.commit()
    assert client.post(f"/voice_memos/{vm}/review").status_code == 409
    assert len(calls) == 2


def test_a_question_id_the_model_invents_or_borrows_is_dropped(user_id):
    other = next(_user_ids)
    _as(other)
    theirs = client.post("/research_questions", json={"name": "Someone else's question"}).json()["id"]
    _as(user_id)
    mine = client.post("/research_questions", json={"name": "My question"}).json()["id"]
    conversation, _ = _conversation()
    vm = _reflection(conversation)
    with PrototypeSession() as db:
        ai_review.persist_review_result(
            prototype_db=db, cortico_db=None, voice_memo=db.get(VoiceMemo, vm), prompt="p", config=get_config(),
            result={"extracted_interests": [], "connections": [
                {"research_question_id": 99999999, "insight_text": "invented", "evidence": []},
                {"research_question_id": theirs, "insight_text": "borrowed", "evidence": []},
                {"research_question_id": mine, "insight_text": "mine", "evidence": []}]},
        )
        db.commit()
    by_text = {c["insight_text"]: c["research_question_id"] for c in client.get(f"/voice_memos/{vm}/review").json()["connections"]}
    assert by_text == {"invented": None, "borrowed": None, "mine": mine}


def test_a_highlight_an_insight_cites_can_be_deleted_and_the_passage_stays(user_id):
    conversation, snippet = _conversation()
    h = client.post(f"/conversations/{conversation}/highlights", json={"snippets": [_ev(snippet)]}).json()["id"]
    vm = _reflection(conversation)
    insight = _file(vm, None, "cites the highlight", [_ev(snippet)])
    with PrototypeSession() as db:
        assert [e.highlight_id for e in db.query(ConnectionEvidence).filter_by(connection_id=insight["id"])] == [h]
    assert client.delete(f"/highlights/{h}").status_code == 200
    with PrototypeSession() as db:
        rows = db.query(ConnectionEvidence).filter_by(connection_id=insight["id"]).all()
        assert [(e.snippet_id, e.highlight_id) for e in rows] == [(snippet, None)]


def test_undoing_one_merge_keeps_evidence_another_merge_also_brought(user_id):
    conversation, snippet = _conversation()
    vm = _reflection(conversation)
    a = _file(vm, None, "source a", [_ev(snippet, 0, 2)])["id"]
    b = _file(vm, None, "source b", [_ev(snippet, 0, 2), _ev(snippet, 3, 5)])["id"]
    t = _file(vm, None, "target", [])["id"]
    first = client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": t})
    assert first.status_code == 200, first.text
    second = client.post(f"/memo/connections/{b}/merge_into", json={"target_connection_id": t})
    assert second.status_code == 200, second.text
    merges = {a: first.json()["merge"]["id"], b: second.json()["merge"]["id"]}

    def spans():
        with PrototypeSession() as db:
            return sorted((e.start_index, e.end_index) for e in db.query(ConnectionEvidence).filter_by(connection_id=t))

    assert spans() == [(0, 2), (3, 5)]
    assert client.post(f"/insight_merges/{merges[a]}/undo").status_code == 200
    assert spans() == [(0, 2), (3, 5)]  # b is still merged in and cites (0, 2)
    assert client.post(f"/insight_merges/{merges[b]}/undo").status_code == 200
    assert spans() == []
    with PrototypeSession() as db:
        assert {db.get(ReflectionConnection, i).status for i in (a, b)} == {"accepted"}
