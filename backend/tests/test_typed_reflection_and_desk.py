"""Typed reflections, per-user reading state, the desk, and review drafts (snapshot mode;
tests/test_cortico_api_mode.py covers the same routes against the fake Cortico)."""
import itertools

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import ReflectionConnection, RLHighlight, VoiceMemo

client = TestClient(app)
_user_ids = itertools.count(91000)


@pytest.fixture
def user_id():
    """A fresh user per test, because every test file shares one database."""
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _act_as(uid: int) -> None:
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)


def _suggest(voice_memo_id: int, text: str = "an AI proposal") -> int:
    with PrototypeSession() as db:
        c = ReflectionConnection(voice_memo_id=voice_memo_id, insight_text=text, source="ai", status="suggested")
        db.add(c)
        db.commit()
        return c.id


def test_typed_reflection_behaves_like_a_transcribed_voice_memo(user_id):
    rq = client.post("/research_questions", json={"name": "typed reflection rq"}).json()
    r = client.post("/conversations/9/reflections", json={"text": "  The library came up again.  "})
    assert r.status_code == 200, r.text
    vm = r.json()
    assert vm["typed"] is True
    assert vm["audio_path"] is None and vm["duration"] is None
    assert vm["status"] == "ready_for_review"
    assert vm["transcription_edited"] == "The library came up again."
    assert vm["transcription_progress"] == 1.0

    assert client.get(f"/voice_memos/{vm['id']}").json()["typed"] is True
    assert [m["id"] for m in client.get("/conversations/9/voice_memos").json()] == [vm["id"]]
    assert client.get(f"/voice_memos/{vm['id']}/review").json()["connections"] == []

    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq["id"],
                           "insight_text": "libraries are trusted", "evidence": []}]}
    assert client.post(f"/voice_memos/{vm['id']}/file", json=body).status_code == 200

    memo = client.get("/memo").json()
    entry = next(s for s in memo["sections"] if s["research_question_id"] == rq["id"])["entries"][0]
    assert entry["voice_memo_id"] == vm["id"]
    assert entry["voice_memo_audio_path"] is None
    assert entry["voice_memo_typed"] is True
    assert entry["reflection_timestamp"] is None


def test_recorded_voice_memo_is_not_typed(user_id):
    with PrototypeSession() as db:
        vm = VoiceMemo(user_id=user_id, conversation_id=9, audio_path="db/local/audio/x.webm", duration=3.0,
                       transcription_edited="spoken", status="ready_for_review")
        db.add(vm)
        db.commit()
        vm_id = vm.id
    got = client.get(f"/voice_memos/{vm_id}").json()
    assert got["typed"] is False and got["audio_path"] == "db/local/audio/x.webm"


def test_typed_reflection_validates_text_and_conversation(user_id):
    assert client.post("/conversations/9/reflections", json={"text": ""}).status_code == 422
    assert client.post("/conversations/9/reflections", json={"text": "   "}).status_code == 422
    assert client.post("/conversations/9/reflections", json={"text": "x" * 20001}).status_code == 422
    assert client.post("/conversations/999999999/reflections", json={"text": "hello"}).status_code == 404


def test_reading_position_upserts_and_keeps_fields_left_out(user_id):
    first = client.put("/conversations/9/reading_position")  # no body: just the open
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["position_seconds"] is None and first["position_snippet_id"] is None
    assert first["first_opened_at"].endswith("+00:00")

    moved = client.put("/conversations/9/reading_position", json={"position_seconds": 61.5, "snippet_id": 1936}).json()
    assert (moved["position_seconds"], moved["position_snippet_id"]) == (61.5, 1936)
    assert moved["first_opened_at"] == first["first_opened_at"]
    assert moved["last_opened_at"] >= first["last_opened_at"]

    # An empty body bumps last opened and keeps the position.
    again = client.put("/conversations/9/reading_position", json={}).json()
    assert (again["position_seconds"], again["position_snippet_id"]) == (61.5, 1936)
    only_seconds = client.put("/conversations/9/reading_position", json={"position_seconds": 70}).json()
    assert (only_seconds["position_seconds"], only_seconds["position_snippet_id"]) == (70.0, 1936)

    assert client.put("/conversations/999999999/reading_position").status_code == 404
    assert client.put("/conversations/9/reading_position", json={"position_seconds": -1}).status_code == 422


def test_desk_keep_reading_finish_and_last_conversation(user_id):
    empty = client.get("/desk").json()
    assert empty == {"keep_reading": [], "finish": [], "last_conversation": None}

    client.put("/conversations/9/reading_position", json={"position_seconds": 12.0, "snippet_id": 1936})
    client.put("/conversations/10/reading_position")
    client.put("/conversations/1/reading_position")
    with PrototypeSession() as db:
        db.add_all([
            RLHighlight(user_id=user_id, conversation_id=9, project_id=5, starred=True),
            RLHighlight(user_id=user_id, conversation_id=9, project_id=5),
            RLHighlight(user_id=user_id + 500000, conversation_id=9, project_id=5),  # someone else's
        ])
        db.commit()

    desk = client.get("/desk").json()
    assert [c["conversation_id"] for c in desk["keep_reading"]] == [1, 10, 9]  # newest first
    nine = desk["keep_reading"][2]
    assert (nine["highlight_count"], nine["starred_count"]) == (2, 1)
    assert (nine["position_seconds"], nine["position_snippet_id"]) == (12.0, 1936)
    assert nine["project_id"] == 5 and isinstance(nine["project_name"], str)
    assert nine["title"] and "duration_seconds" in nine and "start_time" in nine
    assert desk["last_conversation"]["conversation_id"] == 1

    # A reflection moves conversation 9 from "keep reading" to "finish".
    vm = client.post("/conversations/9/reflections", json={"text": "a thought"}).json()
    desk = client.get("/desk").json()
    assert [c["conversation_id"] for c in desk["keep_reading"]] == [1, 10]
    assert [(f["voice_memo_id"], f["state"], f["undecided_count"]) for f in desk["finish"]] == [(vm["id"], "not_reviewed", 0)]
    assert desk["finish"][0]["typed"] is True and desk["finish"][0]["conversation_title"]

    a, b = _suggest(vm["id"]), _suggest(vm["id"])
    finish = client.get("/desk").json()["finish"]
    assert [(f["state"], f["undecided_count"]) for f in finish] == [("undecided", 2)]

    # Deciding one leaves one; deciding both clears the lane.
    client.post(f"/voice_memos/{vm['id']}/file", json={"decisions": [{"connection_id": a, "action": "reject"}]})
    assert client.get("/desk").json()["finish"][0]["undecided_count"] == 1
    client.post(f"/voice_memos/{vm['id']}/file", json={"decisions": [{"connection_id": b, "action": "accept"}]})
    assert client.get("/desk").json()["finish"] == []


def test_desk_finish_covers_conversations_never_opened_and_is_per_user(user_id):
    """The old browser list only looked at recently opened conversations."""
    with PrototypeSession() as db:
        vm = VoiceMemo(user_id=user_id, conversation_id=12, audio_path="db/local/audio/y.webm",
                       transcription_edited="t", status="filed")
        db.add(vm)
        db.commit()
        vm_id = vm.id
    _suggest(vm_id)
    finish = client.get("/desk").json()["finish"]
    assert [(f["voice_memo_id"], f["conversation_id"], f["undecided_count"]) for f in finish] == [(vm_id, 12, 1)]

    _act_as(user_id + 500000)
    assert client.get("/desk").json() == {"keep_reading": [], "finish": [], "last_conversation": None}


def test_keep_reading_is_capped(user_id):
    for conversation_id in range(12, 22):
        client.put(f"/conversations/{conversation_id}/reading_position")
    keep = client.get("/desk").json()["keep_reading"]
    assert len(keep) == 8 and keep[0]["conversation_id"] == 21


def test_review_draft_round_trip_and_filing_deletes_it(user_id):
    vm = client.post("/conversations/9/reflections", json={"text": "draft me"}).json()
    url = f"/voice_memos/{vm['id']}/review_draft"
    assert client.get(url).json() == {"voice_memo_id": vm["id"], "payload": None, "updated_at": None}

    payload = {"decisions": {"12": {"action": "edit", "insight_text": "half typed"}}, "step": 2}
    saved = client.put(url, json={"payload": payload}).json()
    assert saved["payload"] == payload and saved["updated_at"]
    client.put(url, json={"payload": {"step": 3}})
    assert client.get(url).json()["payload"] == {"step": 3}  # replaced, not merged

    assert client.put(url, json={"payload": {"blob": "x" * 200_001}}).status_code == 413
    assert client.put(url, json={"payload": [1, 2]}).status_code == 422
    assert client.get(url).json()["payload"] == {"step": 3}

    assert client.delete(url).json() == {"deleted": True}
    assert client.delete(url).json() == {"deleted": False}

    client.put(url, json={"payload": {"step": 1}})
    client.post(f"/voice_memos/{vm['id']}/file", json={"decisions": []})
    assert client.get(url).json()["payload"] is None


def test_review_draft_of_another_user_is_a_404(user_id):
    vm = client.post("/conversations/9/reflections", json={"text": "mine"}).json()
    url = f"/voice_memos/{vm['id']}/review_draft"
    client.put(url, json={"payload": {"a": 1}})
    _act_as(user_id + 500000)
    assert client.get(url).status_code == 404
    assert client.put(url, json={"payload": {}}).status_code == 404
    assert client.delete(url).status_code == 404
    assert client.get(f"/voice_memos/{vm['id']}").status_code == 404
