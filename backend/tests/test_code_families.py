"""Structural validation + apply-results logic for code-family synthesis, mirroring
test_next_steps.py: pure/DB-level helpers are unit-tested directly against real data; the
live gpt-5.6-luna round trip is exercised manually (see docs/prototype-log.md) rather than
in automated tests."""
from fastapi.testclient import TestClient
import pytest

from app.auth import CurrentUser, current_user
from app.db import PrototypeSession
from app.main import app
import json

from app.config import CodeFamiliesConfig
from app.models_prototype import (
    CodeFamily,
    CodeFamilyMember,
    CodeFamilyMergeSuggestion,
    ReflectionConnection,
    ResearchQuestion,
    VoiceMemo,
)
from app.services.code_families import (
    _apply_family_results,
    _apply_merge_suggestions,
    _valid_connection_ids,
    accept_merge,
    cap_status,
)

client = TestClient(app)

FIXTURE_USER = 80001


@pytest.fixture(autouse=True)
def _act_as_fixture_user():
    """Routes only serve rows the caller owns, and this module's rows belong to FIXTURE_USER."""
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=FIXTURE_USER)
    yield
    app.dependency_overrides.pop(current_user, None)


_DEFAULT_AI_METADATA = {"model": "m", "reasoning_effort": "low", "prompt": "p"}


def _make_rq(db, user_id=80001):
    rq = ResearchQuestion(user_id=user_id, project_id=5, name="test rq", display_order=0)
    db.add(rq)
    db.commit()
    db.refresh(rq)
    return rq


def _make_connection(db, rq_id, status="accepted", insight="an insight"):
    vm = VoiceMemo(user_id=80001, conversation_id=9, audio_path="x", status="filed")
    db.add(vm)
    db.commit()
    db.refresh(vm)
    c = ReflectionConnection(
        voice_memo_id=vm.id,
        research_question_id=rq_id,
        insight_text=insight,
        source="user",
        status=status,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_valid_connection_ids_only_includes_accepted_or_edited_in_this_rq():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        other_rq = _make_rq(db)
        accepted = _make_connection(db, rq.id, status="accepted")
        edited = _make_connection(db, rq.id, status="edited")
        suggested = _make_connection(db, rq.id, status="suggested")
        wrong_rq = _make_connection(db, other_rq.id, status="accepted")

        valid = _valid_connection_ids(db, rq.id)
        assert valid == {accepted.id, edited.id}
        assert suggested.id not in valid
        assert wrong_rq.id not in valid
    finally:
        db.close()


def test_apply_family_results_creates_new_family_with_valid_members_only():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1 = _make_connection(db, rq.id)
        c2 = _make_connection(db, rq.id)
        valid_ids = {c1.id, c2.id}

        raw = [
            {
                "existing_family_id": None,
                "name": "Trust in institutions",
                "definition": "Entries about trusting or distrusting local institutions.",
                "member_connection_ids": [c1.id, c2.id, 999_999],  # last one invalid, must be dropped
            }
        ]
        touched, _ = _apply_family_results(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_families=raw,
            valid_connection_ids=valid_ids,
            existing_family_ids=set(),
            ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert len(touched) == 1
        family = touched[0]
        assert family.name == "Trust in institutions"
        assert {m.connection_id for m in family.members} == {c1.id, c2.id}
    finally:
        db.close()


def test_apply_family_results_attaches_to_existing_family_instead_of_duplicating():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1 = _make_connection(db, rq.id)
        c2 = _make_connection(db, rq.id)

        existing = CodeFamily(
            user_id=80001, research_question_id=rq.id, name="Existing family", definition="d", status="accepted"
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
        db.add(CodeFamilyMember(code_family_id=existing.id, connection_id=c1.id))
        db.commit()

        raw = [
            {
                "existing_family_id": existing.id,
                "name": None,
                "definition": None,
                "member_connection_ids": [c1.id, c2.id],  # c1 already a member, c2 new
            }
        ]
        touched, _ = _apply_family_results(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_families=raw,
            valid_connection_ids={c1.id, c2.id},
            existing_family_ids={existing.id},
            ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert len(touched) == 1
        assert touched[0].id == existing.id

        all_families = db.query(CodeFamily).filter(CodeFamily.research_question_id == rq.id).all()
        assert len(all_families) == 1  # no duplicate created
        assert {m.connection_id for m in existing.members} == {c1.id, c2.id}
    finally:
        db.close()


def test_apply_family_results_drops_family_with_no_valid_members():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        raw = [
            {
                "existing_family_id": None,
                "name": "Hallucinated family",
                "definition": "d",
                "member_connection_ids": [999_999],
            }
        ]
        touched, _ = _apply_family_results(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_families=raw,
            valid_connection_ids=set(),
            existing_family_ids=set(),
            ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert touched == []
        assert db.query(CodeFamily).filter(CodeFamily.research_question_id == rq.id).count() == 0
    finally:
        db.close()


def test_apply_family_results_falls_back_to_new_family_when_existing_id_invalid():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1 = _make_connection(db, rq.id)

        raw = [
            {
                "existing_family_id": 999_999,  # not in existing_family_ids
                "name": "Fallback family",
                "definition": "d",
                "member_connection_ids": [c1.id],
            }
        ]
        touched, _ = _apply_family_results(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_families=raw,
            valid_connection_ids={c1.id},
            existing_family_ids=set(),
            ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert len(touched) == 1
        assert touched[0].name == "Fallback family"
    finally:
        db.close()


def test_rename_redefine_accept_persist_and_reload():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1 = _make_connection(db, rq.id)
        family = CodeFamily(
            user_id=80001, research_question_id=rq.id, name="Orig", definition="orig def", status="suggested"
        )
        db.add(family)
        db.commit()
        db.refresh(family)
        db.add(CodeFamilyMember(code_family_id=family.id, connection_id=c1.id))
        db.commit()
        family_id = family.id
        rq_id = rq.id
        c1_id = c1.id
    finally:
        db.close()

    renamed = client.patch(f"/code_families/{family_id}", json={"name": "Renamed", "definition": "new def"}).json()
    assert renamed["name"] == "Renamed"
    assert renamed["definition"] == "new def"
    assert len(renamed["members"]) == 1
    assert renamed["members"][0]["connection_id"] == c1_id

    accepted = client.post(f"/code_families/{family_id}/accept").json()
    assert accepted["status"] == "accepted"

    default_list = client.get(f"/research_questions/{rq_id}/code_families").json()
    assert any(f["id"] == family_id and f["name"] == "Renamed" and f["status"] == "accepted" for f in default_list)


def test_dismissed_family_hidden_by_default_but_not_deleted():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        family = CodeFamily(
            user_id=80001, research_question_id=rq.id, name="To dismiss", definition="d", status="suggested"
        )
        db.add(family)
        db.commit()
        db.refresh(family)
        family_id = family.id
        rq_id = rq.id
    finally:
        db.close()

    client.post(f"/code_families/{family_id}/dismiss")

    default_list = client.get(f"/research_questions/{rq_id}/code_families").json()
    assert all(f["id"] != family_id for f in default_list)

    with_dismissed = client.get(f"/research_questions/{rq_id}/code_families?include_dismissed=true").json()
    assert any(f["id"] == family_id for f in with_dismissed)


def test_synthesize_on_unknown_research_question_404s():
    resp = client.post("/research_questions/999999/code_families/synthesize")
    assert resp.status_code == 404


_LIMITS = CodeFamiliesConfig(max_per_rq=5, near_cap_at=4, max_new_per_run=2)


def _make_family(db, rq_id, name="fam", status="suggested", member_ids=()):
    f = CodeFamily(user_id=80001, research_question_id=rq_id, name=name, definition="d", status=status)
    db.add(f)
    db.commit()
    db.refresh(f)
    for cid in member_ids:
        db.add(CodeFamilyMember(code_family_id=f.id, connection_id=cid))
    db.commit()
    db.refresh(f)
    return f


def test_cap_status_limits_new_families_per_run_and_per_rq():
    assert cap_status(0, _LIMITS, override_cap=False).max_new_this_run == 2
    assert cap_status(0, _LIMITS, override_cap=False).near_cap is False
    four = cap_status(4, _LIMITS, override_cap=False)
    assert four.max_new_this_run == 1 and four.near_cap is True
    assert cap_status(5, _LIMITS, override_cap=False).max_new_this_run == 0
    # the user can opt past the cap, but still only max_new_per_run at a time
    assert cap_status(5, _LIMITS, override_cap=True).max_new_this_run == 2
    assert cap_status(9, _LIMITS, override_cap=True).max_new_this_run == 2


def test_apply_family_results_withholds_new_families_beyond_max_new_but_still_attaches():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1, c2, c3 = (_make_connection(db, rq.id) for _ in range(3))
        existing = _make_family(db, rq.id, status="accepted")
        raw = [
            {"existing_family_id": None, "name": "New A", "definition": "a", "member_connection_ids": [c1.id]},
            {"existing_family_id": None, "name": "New B", "definition": "b", "member_connection_ids": [c2.id]},
            {"existing_family_id": existing.id, "name": None, "definition": None, "member_connection_ids": [c3.id]},
        ]
        touched, withheld = _apply_family_results(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_families=raw,
            valid_connection_ids={c1.id, c2.id, c3.id},
            existing_family_ids={existing.id},
            ai_metadata=_DEFAULT_AI_METADATA,
            max_new=1,
        )
        assert withheld == 1
        assert [f.name for f in touched] == ["New A", existing.name]
        assert {m.connection_id for m in existing.members} == {c3.id}
        names = {f.name for f in db.query(CodeFamily).filter(CodeFamily.research_question_id == rq.id)}
        assert "New B" not in names
    finally:
        db.close()


def test_apply_merge_suggestions_validates_ids_and_skips_pending_duplicates():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        other_rq = _make_rq(db)
        a = _make_family(db, rq.id, status="suggested")
        b = _make_family(db, rq.id, status="accepted")
        foreign = _make_family(db, other_rq.id)
        raw = [
            {"family_ids": [a.id, b.id], "name": "Broad", "definition": "d", "rationale": "overlap"},
            {"family_ids": [b.id, a.id], "name": "Dup", "definition": "d", "rationale": "same set"},
            {"family_ids": [a.id, foreign.id], "name": "Bad", "definition": "d", "rationale": "x"},
        ]
        created = _apply_merge_suggestions(
            db,
            research_question_id=rq.id,
            user_id=80001,
            raw_merges=raw,
            live_family_ids={a.id, b.id},
            ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert len(created) == 1
        assert set(json.loads(created[0].family_ids_json)) == {a.id, b.id}
        assert created[0].target_family_id == b.id  # the accepted family survives

        again = _apply_merge_suggestions(
            db, research_question_id=rq.id, user_id=80001, raw_merges=raw[:1],
            live_family_ids={a.id, b.id}, ai_metadata=_DEFAULT_AI_METADATA,
        )
        assert again == []  # already pending, not stacked up
    finally:
        db.close()


def test_accept_merge_folds_members_into_target_and_keeps_sources_logged():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        c1, c2, c3 = (_make_connection(db, rq.id) for _ in range(3))
        a = _make_family(db, rq.id, name="A", member_ids=[c1.id, c2.id])
        b = _make_family(db, rq.id, name="B", status="accepted", member_ids=[c2.id, c3.id])
        suggestion = CodeFamilyMergeSuggestion(
            user_id=80001, research_question_id=rq.id, family_ids_json=json.dumps([a.id, b.id]),
            target_family_id=b.id, proposed_name="A+B", proposed_definition="broader", status="suggested",
        )
        db.add(suggestion)
        db.commit()

        target = accept_merge(db, suggestion)
        assert target.id == b.id
        assert target.name == "A+B" and target.definition == "broader" and target.status == "accepted"
        assert sorted(m.connection_id for m in target.members) == sorted([c1.id, c2.id, c3.id])
        db.refresh(a)
        assert a.status == "merged"
        assert {m.connection_id for m in a.members} == {c1.id, c2.id}  # source kept intact
        assert suggestion.status == "accepted"
    finally:
        db.close()


def test_accept_merge_refuses_when_a_family_was_dismissed_since():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        a = _make_family(db, rq.id)
        b = _make_family(db, rq.id, status="dismissed")
        suggestion = CodeFamilyMergeSuggestion(
            user_id=80001, research_question_id=rq.id, family_ids_json=json.dumps([a.id, b.id]),
            target_family_id=a.id, proposed_name="x", proposed_definition="y", status="suggested",
        )
        db.add(suggestion)
        db.commit()
        merge_id, rq_id = suggestion.id, rq.id
    finally:
        db.close()

    assert client.post(f"/code_family_merges/{merge_id}/accept").status_code == 409
    # and a stale suggestion isn't shown as pending
    state = client.get(f"/research_questions/{rq_id}/code_families/state").json()
    assert state["merge_suggestions"] == []


def test_state_endpoint_reports_cap_and_hides_merged_families():
    db = PrototypeSession()
    try:
        rq = _make_rq(db)
        for i in range(4):
            _make_family(db, rq.id, name=f"f{i}")
        merged = _make_family(db, rq.id, name="gone", status="merged")
        rq_id, merged_id = rq.id, merged.id
    finally:
        db.close()

    state = client.get(f"/research_questions/{rq_id}/code_families/state").json()
    assert state["cap"] == {"live_count": 4, "max_per_rq": 5, "near_cap": True, "at_cap": False}
    assert all(f["id"] != merged_id for f in state["families"])
    with_all = client.get(f"/research_questions/{rq_id}/code_families?include_dismissed=true").json()
    assert any(f["id"] == merged_id for f in with_all)
