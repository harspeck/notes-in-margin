"""The set-aside list, bringing a connection back, and the activity log."""
import itertools

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import CorticoSession, PrototypeSession
from app.main import app
from app.models_prototype import (
    Activity,
    CodeFamily,
    NextStepSuggestion,
    ReflectionConnection,
    ReflectionConnectionRevision,
    SetAside,
    VoiceMemo,
)
from app.services import activity
from app.services.ai_review import persist_review_result

client = TestClient(app)
_user_ids = itertools.count(93000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _reflection() -> int:
    return client.post("/conversations/9/reflections", json={"text": "a reflection"}).json()["id"]


def _suggest(voice_memo_id: int, text: str, rq_id: int | None = None) -> int:
    with PrototypeSession() as db:
        c = ReflectionConnection(voice_memo_id=voice_memo_id, research_question_id=rq_id, insight_text=text,
                                 source="ai", status="suggested")
        db.add(c)
        db.commit()
        return c.id


def _file(vm_id: int, *decisions: dict) -> dict:
    r = client.post(f"/voice_memos/{vm_id}/file", json={"decisions": list(decisions)})
    assert r.status_code == 200, r.text
    return r.json()


def _status(connection_id: int) -> str:
    with PrototypeSession() as db:
        return db.get(ReflectionConnection, connection_id).status


def _kinds(filter: str = "all") -> list[str]:
    return [a["kind"] for a in client.get(f"/activity?filter={filter}&limit=200").json()["items"]]


def _memo_ids(rq_id: int) -> list[int]:
    section = next(s for s in client.get("/memo").json()["sections"] if s["research_question_id"] == rq_id)
    return [e["connection_id"] for e in section["entries"]]


def test_set_aside_lists_retracted_and_rejected_with_when_and_where(user_id):
    rq = client.post("/research_questions", json={"name": "set aside rq"}).json()
    vm_id = _reflection()
    kept = _suggest(vm_id, "kept then retracted", rq["id"])
    rejected = _suggest(vm_id, "rejected at review", rq["id"])
    _file(vm_id, {"connection_id": kept, "action": "accept"}, {"connection_id": rejected, "action": "reject"})
    assert client.get("/memo/set_aside").json()["items"][0]["connection_id"] == rejected

    assert client.post(f"/memo/connections/{kept}/retract").json()["status"] == "retracted"
    items = client.get("/memo/set_aside").json()["items"]
    assert [(i["connection_id"], i["kind"], i["restores_to"]) for i in items] == [
        (kept, "retracted", "accepted"),
        (rejected, "rejected", "suggested"),
    ]
    first = items[0]
    assert first["insight_text"] == "kept then retracted"
    assert (first["research_question_id"], first["research_question_name"]) == (rq["id"], "set aside rq")
    assert first["conversation_id"] == 9 and first["conversation_title"] and first["voice_memo_id"] == vm_id
    assert first["set_aside_at"].endswith("+00:00") and first["set_aside_at_source"] == "recorded"

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.get("/memo/set_aside").json() == {"items": []}
    assert client.post(f"/memo/connections/{kept}/restore").status_code == 404


def test_restoring_a_retracted_entry_puts_back_its_filed_status_with_a_revision_first(user_id):
    """The one route besides file_to_memo that sets accepted or edited: it only restores the
    filed status an entry already had."""
    rq = client.post("/research_questions", json={"name": "restore rq"}).json()
    vm_id = _reflection()
    accepted = _suggest(vm_id, "accepted as is", rq["id"])
    edited = _suggest(vm_id, "before the edit", rq["id"])
    _file(vm_id, {"connection_id": accepted, "action": "accept"},
          {"connection_id": edited, "action": "edit", "insight_text": "after the edit"})
    assert (_status(accepted), _status(edited)) == ("accepted", "edited")

    for cid in (accepted, edited):
        client.post(f"/memo/connections/{cid}/retract")
    assert _memo_ids(rq["id"]) == []

    with PrototypeSession() as db:
        revisions_before = db.query(ReflectionConnectionRevision).filter_by(connection_id=accepted).count()
    restored = client.post(f"/memo/connections/{accepted}/restore").json()
    assert restored == {"id": accepted, "status": "accepted", "restored_from": "retracted",
                        "voice_memo_id": vm_id, "in_memo": True}
    assert client.post(f"/memo/connections/{edited}/restore").json()["status"] == "edited"
    assert sorted(_memo_ids(rq["id"])) == sorted([accepted, edited])
    assert client.get("/memo/set_aside").json() == {"items": []}

    with PrototypeSession() as db:
        revisions = db.query(ReflectionConnectionRevision).filter_by(connection_id=accepted).all()
        assert len(revisions) == revisions_before + 1 and revisions[-1].insight_text == "accepted as is"
        record = db.query(SetAside).filter_by(connection_id=accepted).one()
        assert record.previous_status == "accepted" and record.restored_at is not None

    # Not set aside any more, so a second restore is refused. A second round still works,
    # and the revision the first restore wrote doesn't turn "accepted" into "edited".
    assert client.post(f"/memo/connections/{accepted}/restore").status_code == 409
    client.post(f"/memo/connections/{accepted}/retract")
    assert client.post(f"/memo/connections/{accepted}/restore").json()["status"] == "accepted"
    assert client.post("/memo/connections/999999999/restore").status_code == 404


def test_entry_retracted_before_set_aside_rows_existed_still_restores(user_id):
    vm_id = _reflection()
    with PrototypeSession() as db:
        plain = ReflectionConnection(voice_memo_id=vm_id, insight_text="old plain", source="ai", status="retracted")
        changed = ReflectionConnection(voice_memo_id=vm_id, insight_text="old edited", source="user",
                                       status="retracted", edited_from_ai=True)
        db.add_all([plain, changed])
        db.commit()
        plain_id, changed_id = plain.id, changed.id

    items = {i["connection_id"]: i for i in client.get("/memo/set_aside").json()["items"]}
    assert items[plain_id]["set_aside_at_source"] == "last_changed" and items[plain_id]["set_aside_at"]
    assert (items[plain_id]["restores_to"], items[changed_id]["restores_to"]) == ("accepted", "edited")
    assert client.post(f"/memo/connections/{plain_id}/restore").json()["status"] == "accepted"
    assert client.post(f"/memo/connections/{changed_id}/restore").json()["status"] == "edited"


def test_restoring_a_rejected_insight_returns_it_to_review_not_to_the_memo(user_id):
    rq = client.post("/research_questions", json={"name": "rejected restore rq"}).json()
    vm_id = _reflection()
    kept = _suggest(vm_id, "kept", rq["id"])
    rejected = _suggest(vm_id, "rejected then wanted back", rq["id"])
    _file(vm_id, {"connection_id": kept, "action": "accept"}, {"connection_id": rejected, "action": "reject"})
    assert client.get(f"/voice_memos/{vm_id}").json()["status"] == "filed"  # this reflection was filed once

    restored = client.post(f"/memo/connections/{rejected}/restore").json()
    assert restored == {"id": rejected, "status": "suggested", "restored_from": "rejected",
                        "voice_memo_id": vm_id, "in_memo": False}
    assert _memo_ids(rq["id"]) == [kept]  # nothing reached the memo

    review = client.get(f"/voice_memos/{vm_id}/review").json()
    assert next(c for c in review["connections"] if c["id"] == rejected)["status"] == "suggested"
    finish = client.get("/desk").json()["finish"]
    assert [(f["voice_memo_id"], f["undecided_count"]) for f in finish] == [(vm_id, 1)]

    # Only file_to_memo moves it into the memo.
    _file(vm_id, {"connection_id": rejected, "action": "accept"})
    assert sorted(_memo_ids(rq["id"])) == sorted([kept, rejected])
    assert client.get("/desk").json()["finish"] == []


def test_filed_or_suggested_connections_cannot_be_restored(user_id):
    vm_id = _reflection()
    suggested = _suggest(vm_id, "still undecided")
    assert client.post(f"/memo/connections/{suggested}/restore").status_code == 409
    _file(vm_id, {"connection_id": suggested, "action": "accept"})
    assert client.post(f"/memo/connections/{suggested}/restore").status_code == 409
    assert _status(suggested) == "accepted"


def test_activity_log_records_filing_memo_and_question_changes(user_id):
    rq = client.post("/research_questions", json={"name": "activity rq"}).json()
    other = client.post("/research_questions", json={"name": "activity rq two"}).json()
    vm_id = _reflection()
    a, b, c = (_suggest(vm_id, t, rq["id"]) for t in ("keep me", "edit me", "reject me"))
    _file(vm_id, {"connection_id": a, "action": "accept"},
          {"connection_id": b, "action": "edit", "insight_text": "edited"},
          {"connection_id": c, "action": "reject"},
          {"connection_id": None, "action": "accept", "insight_text": "my own", "research_question_id": rq["id"]})
    client.patch(f"/memo/connections/{a}", json={"insight_text": "kept, then edited in the memo"})
    client.post(f"/memo/connections/{a}/retract")
    client.post(f"/memo/connections/{a}/restore")
    client.post(f"/memo/connections/{c}/restore")
    client.patch(f"/research_questions/{rq['id']}", json={"name": "activity rq, reworded"})
    client.patch(f"/research_questions/{rq['id']}", json={"name": "activity rq, reworded"})  # no change: no row
    client.patch(f"/research_questions/{other['id']}", json={"archived": True})
    client.patch(f"/research_questions/{other['id']}", json={"archived": False})
    merge = client.post(f"/research_questions/{other['id']}/merge_into", json={"target_id": rq["id"]}).json()
    client.post(f"/rq_merges/{merge['merge']['id']}/undo")

    assert _kinds()[::-1] == [
        "rq_created", "rq_created", "reflection_typed",
        "insight_kept", "insight_edited", "insight_set_aside", "insight_added",
        "memo_entry_edited", "memo_entry_retracted", "memo_entry_restored", "insight_returned_to_review",
        "rq_reworded", "rq_archived", "rq_restored", "rq_merged", "rq_merge_undone",
    ]
    assert _kinds("ai") == []
    assert _kinds("set_aside")[::-1] == ["insight_set_aside", "memo_entry_retracted"]
    assert _kinds("changed")[::-1] == [
        "insight_edited", "memo_entry_edited", "rq_reworded", "rq_archived", "rq_restored", "rq_merged", "rq_merge_undone",
    ]

    items = client.get("/activity?limit=200").json()["items"]
    rejected = next(i for i in items if i["kind"] == "insight_set_aside")
    assert rejected["actor"] == "user" and "reject me" in rejected["summary"]
    assert rejected["refs"] == {"connection_id": c, "research_question_id": rq["id"], "conversation_id": 9, "voice_memo_id": vm_id}
    assert rejected["created_at"].endswith("+00:00")
    assert client.get("/activity?filter=nonsense").status_code == 422


def test_activity_log_records_ai_suggestions_next_steps_and_code_families(user_id):
    rq = client.post("/research_questions", json={"name": "ai activity rq"}).json()
    vm_id = _reflection()
    with PrototypeSession() as db, CorticoSession() as cortico:
        persist_review_result(
            prototype_db=db, cortico_db=cortico, voice_memo=db.get(VoiceMemo, vm_id), prompt="p", config=get_config(),
            result={"extracted_interests": [], "connections": [
                {"research_question_id": rq["id"], "insight_text": "one", "evidence": []},
                {"research_question_id": None, "insight_text": "two", "evidence": []},
            ]},
        )
        step = NextStepSuggestion(user_id=user_id, suggested_direction="hear more about libraries", status="suggested")
        other_step = NextStepSuggestion(user_id=user_id, suggested_direction="hear more about trust", status="suggested")
        family = CodeFamily(user_id=user_id, research_question_id=rq["id"], name="Belonging", definition="d", status="suggested")
        dropped = CodeFamily(user_id=user_id, research_question_id=rq["id"], name="Distance", definition="d", status="suggested")
        db.add_all([step, other_step, family, dropped])
        db.commit()
        step_id, other_step_id, family_id, dropped_id = step.id, other_step.id, family.id, dropped.id

    client.post(f"/next_steps/{step_id}/accept")
    client.post(f"/next_steps/{step_id}/accept")  # already accepted: not logged twice
    client.post(f"/next_steps/{other_step_id}/dismiss")
    client.post(f"/code_families/{family_id}/accept")
    client.post(f"/code_families/{family_id}/star", json={"starred": True})
    client.post(f"/code_families/{family_id}/star", json={"starred": False})
    client.post(f"/code_families/{dropped_id}/dismiss")

    assert _kinds()[::-1] == [
        "rq_created", "reflection_typed", "insights_suggested",
        "next_step_accepted", "next_step_dismissed",
        "code_family_accepted", "code_family_starred", "code_family_dismissed",
    ]
    ai = client.get("/activity?filter=ai").json()["items"]
    assert [(i["actor"], i["kind"], i["refs"]["count"]) for i in ai] == [("ai", "insights_suggested", 2)]
    assert ai[0]["summary"] == "The AI suggested 2 insights for your reflection."
    assert ai[0]["refs"]["voice_memo_id"] == vm_id
    assert _kinds("set_aside")[::-1] == ["next_step_dismissed", "code_family_dismissed"]


def test_activity_pages_newest_first_and_is_per_user(user_id):
    with PrototypeSession() as db:
        for n in range(5):
            activity.log_activity(db, user_id, "user", activity.RQ_CREATED, f"row {n}", research_question_id=n, skipped=None)
        activity.log_activity(db, user_id + 500000, "user", activity.RQ_CREATED, "someone else's")
        db.commit()

    first = client.get("/activity?limit=2").json()
    assert [i["summary"] for i in first["items"]] == ["row 4", "row 3"]
    assert first["items"][0]["refs"] == {"research_question_id": 4}  # None refs are dropped
    second = client.get(f"/activity?limit=2&before={first['next_before']}").json()
    assert [i["summary"] for i in second["items"]] == ["row 2", "row 1"]
    last = client.get(f"/activity?limit=2&before={second['next_before']}").json()
    assert [i["summary"] for i in last["items"]] == ["row 0"] and last["next_before"] is None


def test_log_activity_never_raises_and_joins_the_callers_transaction(user_id, monkeypatch):
    with PrototypeSession() as db:
        activity.log_activity(db, user_id, "user", activity.RQ_CREATED, "rolled back with its caller")
        db.rollback()
        activity.log_activity(db, user_id, "robot", activity.RQ_CREATED, "bad actor")  # refused, not raised
        monkeypatch.setattr(activity, "Activity", lambda **_: 1 / 0)
        activity.log_activity(db, user_id, "user", activity.RQ_CREATED, "the model blew up")
        db.commit()
        assert db.query(Activity).filter(Activity.user_id == user_id).count() == 0


def test_every_kind_constant_is_registered():
    constants = {
        value for name, value in vars(activity).items()
        if name.isupper() and isinstance(value, str) and name not in ("CHANGED", "SET_ASIDE", "OTHER")
    }
    assert constants == set(activity.KINDS)
    assert set(activity.KINDS.values()) <= {activity.CHANGED, activity.SET_ASIDE, activity.OTHER}
