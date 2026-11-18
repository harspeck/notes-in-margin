"""Listening interests: the user's editable "what I want to hear next" list, the one-time
import from voice memos, and what next-step generation reads. No model is called: review
output is hand-built and passed to persist_review_result."""
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import CorticoSession, PrototypeSession
from app.main import app
from app.models_prototype import ListeningInterest, VoiceMemo
from app.services.ai_review import persist_review_result
from app.services.next_steps import _all_extracted_interests

client = TestClient(app)
_user_ids = itertools.count(92000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _memo(user_id: int, interests: list[str] | None, conversation_id: int = 9) -> int:
    with PrototypeSession() as db:
        vm = VoiceMemo(user_id=user_id, conversation_id=conversation_id, audio_path="", status="ready_for_review",
                       transcription_edited="t",
                       extracted_interests=json.dumps(interests) if interests is not None else None)
        db.add(vm)
        db.commit()
        return vm.id


def _persist(vm_id: int, interests: list[str]) -> None:
    with PrototypeSession() as db, CorticoSession() as cortico:
        persist_review_result(
            prototype_db=db, cortico_db=cortico, voice_memo=db.get(VoiceMemo, vm_id),
            result={"connections": [], "extracted_interests": interests}, prompt="p", config=get_config(),
        )


def test_first_get_imports_existing_interests_once(user_id):
    first = _memo(user_id, ["more about libraries", "rural voices"])
    _memo(user_id, ["rural voices", "  how  trust is  rebuilt "], conversation_id=10)
    _memo(user_id, None)

    rows = client.get("/listening_interests").json()
    assert [r["text"] for r in rows] == ["more about libraries", "rural voices", "how trust is rebuilt"]
    assert {r["origin"] for r in rows} == {"reflection"} and {r["status"] for r in rows} == {"active"}
    assert rows[0]["voice_memo_id"] == first

    # Removing every line must not bring the import back.
    for r in rows:
        assert client.delete(f"/listening_interests/{r['id']}").json() == {"id": r["id"], "status": "removed"}
    assert client.get("/listening_interests").json() == []
    with PrototypeSession() as db:
        kept = db.query(ListeningInterest).filter(ListeningInterest.user_id == user_id).all()
        assert len(kept) == 3 and {k.status for k in kept} == {"removed"}  # status change, not a delete


def test_user_can_add_edit_and_remove_lines(user_id):
    _memo(user_id, ["from a reflection"])
    added = client.post("/listening_interests", json={"text": "  younger   participants "}).json()
    assert added["text"] == "younger participants" and added["origin"] == "user" and added["voice_memo_id"] is None
    # Adding first still imports the earlier reflections' lines.
    assert [r["text"] for r in client.get("/listening_interests").json()] == ["from a reflection", "younger participants"]

    edited = client.patch(f"/listening_interests/{added['id']}", json={"text": "younger voices"}).json()
    assert edited["text"] == "younger voices" and edited["origin"] == "user"

    assert client.post("/listening_interests", json={"text": "   "}).status_code == 422
    assert client.post("/listening_interests", json={"text": "x" * 501}).status_code == 422
    assert client.patch(f"/listening_interests/{added['id']}", json={"text": ""}).status_code == 422

    client.delete(f"/listening_interests/{added['id']}")
    assert client.patch(f"/listening_interests/{added['id']}", json={"text": "back"}).status_code == 409
    assert [r["text"] for r in client.get("/listening_interests").json()] == ["from a reflection"]


def test_another_users_line_is_a_404(user_id):
    mine = client.post("/listening_interests", json={"text": "mine"}).json()
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.patch(f"/listening_interests/{mine['id']}", json={"text": "theirs"}).status_code == 404
    assert client.delete(f"/listening_interests/{mine['id']}").status_code == 404
    assert client.get("/listening_interests").json() == []


def test_review_adds_extracted_interests_and_skips_active_duplicates(user_id):
    client.post("/listening_interests", json={"text": "rural voices"})
    vm_id = _memo(user_id, None)
    _persist(vm_id, ["rural voices", "school boards", "school boards", ""])

    rows = client.get("/listening_interests").json()
    assert [(r["text"], r["origin"], r["voice_memo_id"]) for r in rows] == [
        ("rural voices", "user", None),
        ("school boards", "reflection", vm_id),
    ]
    with PrototypeSession() as db:  # the per-memo column is still written
        assert json.loads(db.get(VoiceMemo, vm_id).extracted_interests) == ["rural voices", "school boards", "school boards", ""]


def test_first_review_still_imports_older_reflections(user_id):
    _memo(user_id, ["older interest"])
    vm_id = _memo(user_id, None, conversation_id=10)
    _persist(vm_id, ["newer interest"])
    assert [r["text"] for r in client.get("/listening_interests").json()] == ["older interest", "newer interest"]


def test_next_steps_read_the_active_lines(user_id):
    with PrototypeSession() as db:
        assert _all_extracted_interests(db, user_id) == []  # none: generation still gets a list

    _memo(user_id, ["from a reflection", "to be removed"])
    rows = client.get("/listening_interests").json()
    client.delete(f"/listening_interests/{rows[1]['id']}")
    client.post("/listening_interests", json={"text": "typed by the user"})
    with PrototypeSession() as db:
        assert _all_extracted_interests(db, user_id) == ["from a reflection", "typed by the user"]
