"""Moving a filed insight to another question, merging two insights by hand, and undo."""
import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import (
    CodeFamilyRevision,
    ConnectionEvidence,
    InsightMerge,
    InsightMove,
    ReflectionConnection,
    ReflectionConnectionRevision,
)

client = TestClient(app)
_user_ids = itertools.count(95000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _rq(name: str) -> int:
    return client.post("/research_questions", json={"name": name}).json()["id"]


def _snippets(conversation_id: int) -> list[int]:
    snippets = client.get(f"/conversations/{conversation_id}").json()["snippets"]
    return [s["snippet_id"] for s in snippets if not s["is_facilitator"] and len(s["words"]) >= 12]


def _ev(snippet_id: int, start: int = 0, end: int = 4, **extra) -> dict:
    return {"snippet_id": snippet_id, "start_index": start, "end_index": end, **extra}


def _insight(conversation_id: int, rq_id: int | None, text: str, evidence: list[dict]) -> tuple[int, int]:
    """Files a new insight through file_to_memo. Returns (connection id, voice memo id)."""
    vm = client.post(f"/conversations/{conversation_id}/reflections", json={"text": "a reflection"}).json()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq_id,
                           "insight_text": text, "evidence": evidence}]}
    r = client.post(f"/voice_memos/{vm['id']}/file", json=body)
    assert r.status_code == 200, r.text
    return r.json()["connections"][0]["id"], vm["id"]


def _memo() -> dict:
    return client.get("/memo").json()


def _entries(rq_id: int | None) -> dict[int, dict]:
    memo = _memo()
    entries = (
        memo["unmapped"]
        if rq_id is None
        else next(s for s in memo["sections"] if s["research_question_id"] == rq_id)["entries"]
    )
    return {e["connection_id"]: e for e in entries}


def _row(connection_id: int) -> ReflectionConnection:
    with PrototypeSession() as db:
        return db.get(ReflectionConnection, connection_id)


def _revisions(connection_id: int) -> list[ReflectionConnectionRevision]:
    with PrototypeSession() as db:
        return db.query(ReflectionConnectionRevision).filter_by(connection_id=connection_id).order_by(
            ReflectionConnectionRevision.id).all()


# --- move -------------------------------------------------------------------------------


def test_moving_an_insight_writes_a_revision_and_a_move_row_and_keeps_its_status(user_id):
    a, b = _rq("move from"), _rq("move to")
    (s9, *_) = _snippets(9)
    c, _ = _insight(9, a, "an insight to move", [_ev(s9)])

    r = client.patch(f"/memo/connections/{c}", json={"research_question_id": b})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": c, "status": "accepted", "research_question_id": b, "moved": True,
                        "removed_from_family_ids": []}
    assert c in _entries(b) and c not in _entries(a)
    assert _entries(b)[c]["research_question_id"] == b
    row = _row(c)
    assert (row.status, row.source, row.edited_from_ai) == ("accepted", "user", False)  # a move is not an edit
    (revision,) = _revisions(c)
    assert revision.insight_text == "an insight to move" and len(json.loads(revision.evidence_snapshot)) == 1
    with PrototypeSession() as db:
        (move,) = db.query(InsightMove).filter_by(connection_id=c).all()
        assert (move.from_research_question_id, move.to_research_question_id, move.user_id) == (a, b, user_id)

    # To "doesn't fit a question yet": an explicit null.
    r = client.patch(f"/memo/connections/{c}", json={"research_question_id": None})
    assert r.json()["research_question_id"] is None and r.json()["moved"] is True
    assert c in _entries(None) and _entries(None)[c]["research_question_id"] is None
    with PrototypeSession() as db:
        moves = db.query(InsightMove).filter_by(connection_id=c).order_by(InsightMove.id).all()
        assert [(m.from_research_question_id, m.to_research_question_id) for m in moves] == [(a, b), (b, None)]
    assert len(_revisions(c)) == 2

    kinds = [i["kind"] for i in client.get("/activity?filter=changed").json()["items"]]
    assert kinds.count("memo_entry_moved") == 2 and "memo_entry_edited" not in kinds


def test_an_edit_without_a_question_still_edits_and_a_move_with_text_does_both(user_id):
    a, b = _rq("edit a"), _rq("edit b")
    c, _ = _insight(9, a, "before", [])
    # No research_question_id in the body: not a move, exactly the old behaviour.
    r = client.patch(f"/memo/connections/{c}", json={"insight_text": "after"})
    assert (r.json()["status"], r.json()["moved"], r.json()["research_question_id"]) == ("edited", False, a)

    r = client.patch(f"/memo/connections/{c}", json={"insight_text": "after again", "research_question_id": b})
    assert (r.json()["status"], r.json()["moved"]) == ("edited", True)
    assert _entries(b)[c]["insight_text"] == "after again"
    # Naming the question it is already under is not a move.
    r = client.patch(f"/memo/connections/{c}", json={"research_question_id": b})
    assert r.json()["moved"] is False
    with PrototypeSession() as db:
        assert db.query(InsightMove).filter_by(connection_id=c).count() == 1


def test_move_validates_the_target_and_resolves_a_merged_one(user_id):
    a, archived, source, target = _rq("mv a"), _rq("mv archived"), _rq("mv source"), _rq("mv target")
    c, _ = _insight(9, a, "stays put on a bad target", [])
    client.patch(f"/research_questions/{archived}", json={"archived": True})
    client.post(f"/research_questions/{source}/merge_into", json={"target_id": target})

    assert client.patch(f"/memo/connections/{c}", json={"research_question_id": archived}).status_code == 409
    assert client.patch(f"/memo/connections/{c}", json={"research_question_id": 999_999_999}).status_code == 404
    assert _row(c).research_question_id == a and _revisions(c) == []
    assert client.patch(f"/memo/connections/{c}", json={"research_question_id": source}).json()[
        "research_question_id"] == target

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.patch(f"/memo/connections/{c}", json={"research_question_id": None}).status_code == 404


def test_moving_out_of_a_question_takes_the_insight_out_of_that_questions_code_families(user_id):
    a, b = _rq("family home"), _rq("family elsewhere")
    c, _ = _insight(9, a, "a member", [])
    stays, _ = _insight(9, a, "another member", [])
    family = client.post(f"/research_questions/{a}/code_families",
                         json={"name": "fam", "definition": "def", "connection_ids": [c, stays]}).json()["family"]

    r = client.patch(f"/memo/connections/{c}", json={"research_question_id": b})
    assert r.json()["removed_from_family_ids"] == [family["id"]]
    (listed,) = client.get(f"/research_questions/{a}/code_families").json()
    assert [m["connection_id"] for m in listed["members"]] == [stays]
    with PrototypeSession() as db:  # the removed member stays auditable
        (revision,) = db.query(CodeFamilyRevision).filter_by(code_family_id=family["id"]).all()
        assert json.loads(revision.member_connection_ids_json) == [c, stays]


# --- merge ------------------------------------------------------------------------------


def test_merge_gives_the_target_the_sources_evidence_once_and_takes_the_source_out_of_the_memo(user_id):
    rq = _rq("merge insights")
    s9a, s9b = _snippets(9)[:2]
    (s12, *_) = _snippets(12)
    h12 = client.post("/conversations/12/highlights", json={"snippets": [_ev(s12, 0, 6)]}).json()["id"]
    target, target_vm = _insight(9, rq, "trust erodes through access", [_ev(s9a, 0, 4), _ev(s9b, 2, 6, source="ai")])
    # Shares one identical span with the target, and brings two of its own from conversation 12.
    source, _ = _insight(12, rq, "daytime meetings keep people out",
                         [_ev(s9a, 0, 4), _ev(s12, 1, 5), _ev(s12, 8, 11, source="ai")])
    assert len(_revisions(target)) == 0

    r = client.post(f"/memo/connections/{source}/merge_into", json={"target_connection_id": target})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == {"id": source, "status": "merged"}
    assert body["target"] == {"id": target, "status": "accepted", "evidence_count": 4}
    merge = body["merge"]
    assert (merge["source_connection_id"], merge["target_connection_id"], merge["status"]) == (source, target, "active")
    assert merge["added_evidence_count"] == 2 and merge["source_previous_status"] == "accepted"
    assert merge["created_at"].endswith("+00:00") and merge["undone_at"] is None

    entries = _entries(rq)
    assert source not in entries
    entry = entries[target]
    assert entry["insight_text"] == "trust erodes through access"
    # Every evidence row carries its own conversation, so the client can say "4 highlights
    # from 2 conversations".
    assert [(e["snippet_id"], e["conversation_id"]) for e in entry["evidence"]] == [
        (s9a, 9), (s9b, 9), (s12, 12), (s12, 12)]
    assert all(e["conversation_title"] for e in entry["evidence"])
    assert entry["conversation_ids"] == [9, 12] and entry["conversation_id"] == 9
    assert entry["merged_from"] == [{"merge_id": merge["id"], "connection_id": source,
                                     "insight_text": "daytime meetings keep people out",
                                     "merged_at": merge["created_at"]}]
    # Provenance went through resolve_evidence: the copied span that overlaps the user's
    # highlight in conversation 12 is linked to it; the AI-only one stays AI.
    copied = {(e["start_index"], e["end_index"]): e for e in entry["evidence"] if e["snippet_id"] == s12}
    assert (copied[(1, 5)]["highlight_id"], copied[(1, 5)]["source"]) == (h12, "user")
    assert (copied[(8, 11)]["highlight_id"], copied[(8, 11)]["source"]) == (None, "ai")

    # A revision of the target was written first, holding its two original passages.
    (revision,) = _revisions(target)
    assert len(json.loads(revision.evidence_snapshot)) == 2
    with PrototypeSession() as db:  # the source keeps its own evidence; nothing was deleted
        assert db.query(ConnectionEvidence).filter_by(connection_id=source).count() == 3

    # Organize and the reflection's review show the same per-evidence conversations.
    organize = client.get("/organize").json()
    (org_entry,) = [e for g in organize["focused"] + organize["other"] for e in g["entries"] if e["connection_id"] == target]
    assert org_entry["evidence"] == entry["evidence"]
    review = client.get(f"/voice_memos/{target_vm}/review").json()["connections"]
    (in_review,) = [c for c in review if c["id"] == target]
    assert [e["conversation_id"] for e in in_review["evidence"]] == [9, 9, 12, 12]
    assert all(e["conversation_title"] for e in in_review["evidence"])

    assert "insights_merged" in [i["kind"] for i in client.get("/activity?filter=changed").json()["items"]]


def test_merge_is_refused_unless_both_are_filed_distinct_and_the_users(user_id):
    rq = _rq("merge refusals")
    a, _ = _insight(9, rq, "a", [])
    b, _ = _insight(9, rq, "b", [])
    retracted, _ = _insight(9, rq, "retracted", [])
    client.post(f"/memo/connections/{retracted}/retract")

    assert client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": a}).status_code == 409
    assert client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": retracted}).status_code == 409
    assert client.post(f"/memo/connections/{retracted}/merge_into", json={"target_connection_id": a}).status_code == 409
    assert client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": 999_999_999}).status_code == 404
    with PrototypeSession() as db:
        assert db.query(InsightMerge).filter(InsightMerge.user_id == user_id).count() == 0

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": b}).status_code == 404
    mine, _ = _insight(9, None, "mine", [])
    assert client.post(f"/memo/connections/{mine}/merge_into", json={"target_connection_id": b}).status_code == 404
    assert _row(b).status == "accepted" and _row(mine).status == "accepted"


def test_undo_removes_exactly_what_the_merge_added_and_restores_the_sources_status(user_id):
    rq = _rq("undo merge")
    s9a, s9b = _snippets(9)[:2]
    (s12, *_) = _snippets(12)
    target, _ = _insight(9, rq, "target", [_ev(s9a, 0, 4)])
    source, _ = _insight(12, rq, "source", [_ev(s12, 1, 5)])
    client.patch(f"/memo/connections/{source}", json={"insight_text": "source, reworded"})  # now "edited"
    merge_id = client.post(f"/memo/connections/{source}/merge_into",
                           json={"target_connection_id": target}).json()["merge"]["id"]
    # Evidence the user adds to the target after the merge is theirs and must survive undo.
    kept_evidence = [_ev(s9a, 0, 4), _ev(s12, 1, 5), _ev(s9b, 3, 9)]
    assert client.patch(f"/memo/connections/{target}", json={"evidence": kept_evidence}).status_code == 200
    revisions_before = len(_revisions(target))

    r = client.post(f"/insight_merges/{merge_id}/undo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merge"]["status"] == "undone" and body["merge"]["undone_at"].endswith("+00:00")
    assert body["source"] == {"id": source, "status": "edited"}
    assert body["target"]["evidence_count"] == 2
    entries = _entries(rq)
    assert [(e["snippet_id"], e["start_index"]) for e in entries[target]["evidence"]] == [(s9a, 0), (s9b, 3)]
    assert entries[target]["merged_from"] == [] and entries[target]["conversation_ids"] == [9]
    assert entries[source]["insight_text"] == "source, reworded"
    assert len(_revisions(target)) == revisions_before + 1  # the removal is on the record

    assert client.post(f"/insight_merges/{merge_id}/undo").status_code == 409  # already undone
    with PrototypeSession() as db:
        assert db.get(InsightMerge, merge_id) is not None  # a status change, never a delete
    assert "insight_merge_undone" in [i["kind"] for i in client.get("/activity").json()["items"]]

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.post(f"/insight_merges/{merge_id}/undo").status_code == 404


def test_undo_is_refused_when_stale(user_id):
    rq = _rq("stale undo")
    a, _ = _insight(9, rq, "a", [])
    b, _ = _insight(9, rq, "b", [])
    c, _ = _insight(9, rq, "c", [])
    first = client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": b}).json()["merge"]["id"]
    second = client.post(f"/memo/connections/{b}/merge_into", json={"target_connection_id": c}).json()["merge"]["id"]

    # b, the first merge's target, has itself been merged away since.
    assert client.post(f"/insight_merges/{first}/undo").status_code == 409
    assert _row(a).status == "merged"
    assert client.post(f"/insight_merges/{second}/undo").status_code == 200
    assert client.post(f"/insight_merges/{first}/undo").status_code == 200
    assert {_row(i).status for i in (a, b, c)} == {"accepted"}

    again = client.post(f"/memo/connections/{a}/merge_into", json={"target_connection_id": b}).json()["merge"]["id"]
    client.post(f"/memo/connections/{b}/retract")
    assert client.post(f"/insight_merges/{again}/undo").status_code == 409  # its target left the memo


def test_a_merged_insight_cannot_be_refiled_restored_or_edited(user_id):
    rq = _rq("merged is not filed")
    target, _ = _insight(9, rq, "target", [])
    source, source_vm = _insight(9, rq, "source", [])
    client.post(f"/memo/connections/{source}/merge_into", json={"target_connection_id": target})

    # Re-filing the source's reflection must not put it back in the memo beside its target.
    body = {"decisions": [{"connection_id": source, "action": "accept"}]}
    r = client.post(f"/voice_memos/{source_vm}/file", json=body)
    assert r.status_code == 200
    assert [c["status"] for c in r.json()["connections"] if c["id"] == source] == ["merged"]
    assert client.patch(f"/memo/connections/{source}", json={"insight_text": "x"}).status_code == 404
    assert client.post(f"/memo/connections/{source}/restore").status_code == 409
    assert client.post(f"/memo/connections/{source}/retract").status_code == 404
    assert source not in {i["connection_id"] for i in client.get("/memo/set_aside").json()["items"]}
    assert source not in _entries(rq)


def test_a_merged_insights_evidence_can_be_edited_with_highlights_from_its_other_conversation(user_id):
    rq = _rq("edit across conversations")
    (s9, *_) = _snippets(9)
    s12, other12 = _snippets(12)[:2]
    h12 = client.post("/conversations/12/highlights", json={"snippets": [_ev(s12, 0, 6)]}).json()["id"]
    unrelated = client.post("/conversations/13/highlights", json={"snippets": [_ev(_snippets(13)[0], 0, 6)]}).json()["id"]
    target, _ = _insight(9, rq, "target", [_ev(s9)])
    source, _ = _insight(12, rq, "source", [_ev(s12, 0, 6, highlight_id=h12)])
    client.post(f"/memo/connections/{source}/merge_into", json={"target_connection_id": target})

    # The client sends the list back with one passage dropped and one added by overlap.
    evidence = [_ev(s12, 0, 6, highlight_id=h12), _ev(s12, 3, 9)]
    assert client.patch(f"/memo/connections/{target}", json={"evidence": evidence}).status_code == 200
    saved = _entries(rq)[target]["evidence"]
    assert [(e["highlight_id"], e["source"], e["conversation_id"]) for e in saved] == [(h12, "user", 12), (h12, "user", 12)]
    # A highlight that covers neither the reflection's conversation nor that snippet is refused.
    bad = [_ev(other12, 0, 3, highlight_id=unrelated)]
    assert client.patch(f"/memo/connections/{target}", json={"evidence": bad}).status_code == 400
