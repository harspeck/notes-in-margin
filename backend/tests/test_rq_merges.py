"""RQ merges: a merged RQ's entries and families read under its target, the merge is
logged and reversible, and invalid merges (self, into an archived or merged RQ, cycles)
are refused. GET /memo filters by config.default_user_id, so RQs here use that user."""
from fastapi.testclient import TestClient

from app.config import get_config
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import (
    CodeFamily,
    CodeFamilyMember,
    ReflectionConnection,
    ResearchQuestion,
    RQMerge,
    VoiceMemo,
)
from app.services.code_families import _valid_connection_ids, synthesize_code_families
from app.services.rq_merges import resolve_rq_id, rq_group_ids

client = TestClient(app)
config = get_config()
USER = config.default_user_id


def _rq(db, name="merge rq", archived=False) -> int:
    rq = ResearchQuestion(user_id=USER, project_id=5, name=name, archived=archived)
    db.add(rq)
    db.commit()
    return rq.id


def _connection(db, rq_id, status="accepted", insight="an insight") -> int:
    vm = VoiceMemo(user_id=USER, conversation_id=9, audio_path="x", status="filed")
    db.add(vm)
    db.commit()
    c = ReflectionConnection(
        voice_memo_id=vm.id, research_question_id=rq_id, insight_text=insight, source="user", status=status
    )
    db.add(c)
    db.commit()
    return c.id


def _family(db, rq_id, name="fam", status="suggested", member_ids=()) -> int:
    f = CodeFamily(user_id=USER, research_question_id=rq_id, name=name, definition="d", status=status)
    db.add(f)
    db.commit()
    for cid in member_ids:
        db.add(CodeFamilyMember(code_family_id=f.id, connection_id=cid))
    db.commit()
    return f.id


def _merge(source_id, target_id):
    return client.post(f"/research_questions/{source_id}/merge_into", json={"target_id": target_id})


def test_merge_archives_source_and_resolves_to_target():
    with PrototypeSession() as db:
        a, b = _rq(db, "A"), _rq(db, "B")

    resp = _merge(a, b)
    assert resp.status_code == 200
    body = resp.json()
    assert body["merge"]["source_rq_id"] == a and body["merge"]["target_rq_id"] == b
    assert body["merge"]["status"] == "active"
    assert body["source"]["archived"] is True and body["source"]["merged_into"] == b

    with PrototypeSession() as db:
        assert resolve_rq_id(db, a) == b
        assert resolve_rq_id(db, b) == b
        assert sorted(rq_group_ids(db, b)) == sorted([a, b])
        assert db.get(ResearchQuestion, a).archived is True


def test_merges_resolve_transitively():
    with PrototypeSession() as db:
        a, b, c = _rq(db, "A"), _rq(db, "B"), _rq(db, "C")
    assert _merge(a, b).status_code == 200
    assert _merge(b, c).status_code == 200
    with PrototypeSession() as db:
        assert resolve_rq_id(db, a) == c
        assert sorted(rq_group_ids(db, c)) == sorted([a, b, c])


def test_invalid_merges_are_refused():
    with PrototypeSession() as db:
        a, b, c = _rq(db, "A"), _rq(db, "B"), _rq(db, "C")
        archived = _rq(db, "archived", archived=True)

    assert _merge(a, a).status_code == 409  # self
    assert _merge(a, archived).status_code == 409  # archived target
    assert _merge(archived, a).status_code == 409  # archived source
    assert _merge(a, 999_999).status_code == 404
    assert _merge(999_999, a).status_code == 404

    assert _merge(a, b).status_code == 200
    assert _merge(a, c).status_code == 409  # already merged
    assert _merge(c, a).status_code == 409  # into a merged RQ
    assert _merge(b, c).status_code == 200
    assert _merge(c, a).status_code == 409  # would be a cycle; a is merged anyway

    with PrototypeSession() as db:
        assert db.query(RQMerge).filter(RQMerge.source_rq_id == a, RQMerge.status == "active").count() == 1


def test_resolver_survives_a_cycle_in_the_data():
    with PrototypeSession() as db:
        a, b = _rq(db, "A"), _rq(db, "B")
        db.add(RQMerge(user_id=USER, source_rq_id=a, target_rq_id=b, status="active"))
        db.add(RQMerge(user_id=USER, source_rq_id=b, target_rq_id=a, status="active"))
        db.commit()
        assert resolve_rq_id(db, a) in (a, b)
        assert sorted(rq_group_ids(db, a)) == sorted([a, b])
        for m in db.query(RQMerge).filter(RQMerge.source_rq_id.in_([a, b])):
            m.status = "undone"
        db.commit()


def test_undo_restores_the_source_and_is_logged():
    with PrototypeSession() as db:
        a, b = _rq(db, "A"), _rq(db, "B")
    merge_id = _merge(a, b).json()["merge"]["id"]

    resp = client.post(f"/rq_merges/{merge_id}/undo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["merge"]["status"] == "undone" and body["merge"]["undone_at"] is not None
    assert body["source"]["archived"] is False and body["source"]["merged_into"] is None
    assert body["families_kept_with_target"] == []

    with PrototypeSession() as db:
        assert resolve_rq_id(db, a) == a
        assert db.get(RQMerge, merge_id) is not None  # kept, not deleted

    assert client.post(f"/rq_merges/{merge_id}/undo").status_code == 409
    assert client.post("/rq_merges/999999/undo").status_code == 404
    assert _merge(a, b).status_code == 200  # can merge again after undo


def test_memo_buckets_a_merged_rqs_entries_under_its_target():
    with PrototypeSession() as db:
        a, b = _rq(db, "memo source"), _rq(db, "memo target")
        ca = _connection(db, a, insight="from source")
        cb = _connection(db, b, insight="from target")

    merge_id = _merge(a, b).json()["merge"]["id"]
    memo = client.get("/memo").json()
    sections = {s["research_question_id"]: s for s in memo["sections"]}
    assert a not in sections
    assert {e["connection_id"] for e in sections[b]["entries"]} == {ca, cb}
    assert sections[b]["merged_from"] == [a]
    assert all(e["connection_id"] != ca for e in memo["unmapped"])

    client.post(f"/rq_merges/{merge_id}/undo")
    memo = client.get("/memo").json()
    sections = {s["research_question_id"]: s for s in memo["sections"]}
    assert [e["connection_id"] for e in sections[a]["entries"]] == [ca]
    assert [e["connection_id"] for e in sections[b]["entries"]] == [cb]


def test_memo_reorder_accepts_entries_from_the_merged_group():
    with PrototypeSession() as db:
        a, b = _rq(db), _rq(db)
        ca, cb = _connection(db, a), _connection(db, b)
    _merge(a, b)
    client.put("/memo/reorder", json={"research_question_id": b, "ordered_connection_ids": [ca, cb]})
    with PrototypeSession() as db:
        assert db.get(ReflectionConnection, ca).memo_order == 0
        assert db.get(ReflectionConnection, cb).memo_order == 1


def test_cap_and_valid_entries_span_the_merged_group():
    with PrototypeSession() as db:
        a, b = _rq(db), _rq(db)
        ca, cb = _connection(db, a), _connection(db, b)
        for i in range(2):
            _family(db, a, name=f"a{i}")
        for i in range(3):
            _family(db, b, name=f"b{i}")

    before = client.get(f"/research_questions/{b}/code_families/state").json()
    assert before["cap"]["live_count"] == 3

    _merge(a, b)
    state = client.get(f"/research_questions/{b}/code_families/state").json()
    assert state["cap"] == {"live_count": 5, "max_per_rq": 5, "near_cap": True, "at_cap": True}
    assert len(state["families"]) == 5
    with PrototypeSession() as db:
        assert _valid_connection_ids(db, b) == {ca, cb}


def test_synthesis_on_a_merged_group_at_cap_still_skips_nothing_filed():
    """Synthesis resolves to the target and counts the whole group; with nothing filed it
    returns before any model call."""
    with PrototypeSession() as db:
        a, b = _rq(db), _rq(db)
        for i in range(3):
            _family(db, a, name=f"a{i}")
    _merge(a, b)
    with PrototypeSession() as db:
        result = synthesize_code_families(
            prototype_db=db, cortico_db=None, config=config, user_id=USER, research_question_id=a
        )
    assert result.families == []
    assert result.cap.live_count == 3


def test_list_research_questions_reports_merges():
    with PrototypeSession() as db:
        a, b = _rq(db, "listed source"), _rq(db, "listed target")
    merge_id = _merge(a, b).json()["merge"]["id"]

    default = {rq["id"]: rq for rq in client.get("/research_questions").json()}
    assert a not in default
    assert default[b]["merged_into"] is None
    assert default[b]["merged_sources"] == [{"merge_id": merge_id, "id": a, "name": "listed source", "into_id": b}]
    assert default[b]["focused"] is False

    everything = {rq["id"]: rq for rq in client.get("/research_questions?include_archived=true").json()}
    assert everything[a]["merged_into"] == b


def test_undo_reports_cross_rq_families_kept_with_the_target():
    with PrototypeSession() as db:
        a, b = _rq(db), _rq(db)
        ca, cb = _connection(db, a), _connection(db, b)
        fa = _family(db, a, name="fa", status="accepted", member_ids=[ca])
        fb = _family(db, b, name="fb", member_ids=[cb])
    merge_id = _merge(a, b).json()["merge"]["id"]

    merged = client.post("/code_families/merge", json={"family_ids": [fa, fb], "name": "both", "definition": "d"})
    assert merged.status_code == 200

    body = client.post(f"/rq_merges/{merge_id}/undo").json()
    assert body["families_kept_with_target"] == [{"id": fa, "name": "both"}]
    with PrototypeSession() as db:
        assert db.get(CodeFamily, fa).research_question_id == b

