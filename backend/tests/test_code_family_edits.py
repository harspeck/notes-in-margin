"""User edits to code families: create, replace members, rename, star and merge. Every
connection id is checked against the RQ group's filed entries, and every edit writes a
revision before it changes the family."""
import json

from fastapi.testclient import TestClient

from app.config import get_config
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import (
    CodeFamily,
    CodeFamilyMember,
    CodeFamilyMergeSuggestion,
    CodeFamilyRevision,
    CodeFamilyStar,
    ReflectionConnection,
    ResearchQuestion,
    VoiceMemo,
)
from app.services.code_families import accept_merge, merge_families

client = TestClient(app)
USER = get_config().default_user_id


def _rq(db) -> int:
    rq = ResearchQuestion(user_id=USER, project_id=5, name="edit rq")
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


def _revisions(db, family_id) -> list[CodeFamilyRevision]:
    return (
        db.query(CodeFamilyRevision)
        .filter(CodeFamilyRevision.code_family_id == family_id)
        .order_by(CodeFamilyRevision.id)
        .all()
    )


# --- create ----------------------------------------------------------------------------


def test_create_family_is_accepted_user_made_and_reports_the_cap():
    with PrototypeSession() as db:
        rq = _rq(db)
        c1, c2 = _connection(db, rq), _connection(db, rq)

    resp = client.post(
        f"/research_questions/{rq}/code_families",
        json={"name": "Trust", "definition": "Entries about trust.", "connection_ids": [c1, c2, c1]},
    )
    assert resp.status_code == 200
    body = resp.json()
    fam = body["family"]
    assert fam["status"] == "accepted" and fam["ai_metadata"] is None and fam["starred"] is False
    assert fam["research_question_id"] == rq
    assert sorted(m["connection_id"] for m in fam["members"]) == sorted([c1, c2])
    assert body["cap"] == {"live_count": 1, "max_per_rq": 5, "near_cap": False, "at_cap": False}


def test_create_family_is_not_blocked_by_the_cap():
    with PrototypeSession() as db:
        rq = _rq(db)
        for i in range(5):
            _family(db, rq, name=f"f{i}")
    resp = client.post(f"/research_questions/{rq}/code_families", json={"name": "Sixth", "definition": "d"})
    assert resp.status_code == 200
    assert resp.json()["cap"]["live_count"] == 6 and resp.json()["cap"]["at_cap"] is True


def test_create_family_rejects_connections_that_are_not_filed_in_the_group():
    with PrototypeSession() as db:
        rq, other = _rq(db), _rq(db)
        ok = _connection(db, rq)
        suggested = _connection(db, rq, status="suggested")
        retracted = _connection(db, rq, status="retracted")
        foreign = _connection(db, other)

    for bad in (suggested, retracted, foreign, 999_999):
        resp = client.post(
            f"/research_questions/{rq}/code_families",
            json={"name": "x", "definition": "d", "connection_ids": [ok, bad]},
        )
        assert resp.status_code == 422, bad
    assert client.post(
        f"/research_questions/{rq}/code_families", json={"name": " ", "definition": "d"}
    ).status_code == 422
    assert client.post(
        "/research_questions/999999/code_families", json={"name": "x", "definition": "d"}
    ).status_code == 404
    with PrototypeSession() as db:
        assert db.query(CodeFamily).filter(CodeFamily.research_question_id == rq).count() == 0


def test_create_on_a_merged_group_accepts_source_entries_and_writes_to_the_target():
    with PrototypeSession() as db:
        a, b = _rq(db), _rq(db)
        ca = _connection(db, a)
    client.post(f"/research_questions/{a}/merge_into", json={"target_id": b})
    resp = client.post(
        f"/research_questions/{a}/code_families", json={"name": "x", "definition": "d", "connection_ids": [ca]}
    )
    assert resp.status_code == 200
    assert resp.json()["family"]["research_question_id"] == b


# --- members and rename ----------------------------------------------------------------


def test_replace_members_writes_a_revision_first():
    with PrototypeSession() as db:
        rq = _rq(db)
        c1, c2, c3 = (_connection(db, rq) for _ in range(3))
        fid = _family(db, rq, name="Orig", member_ids=[c1, c2])

    resp = client.put(f"/code_families/{fid}/members", json={"connection_ids": [c2, c3]})
    assert resp.status_code == 200
    assert sorted(m["connection_id"] for m in resp.json()["members"]) == sorted([c2, c3])

    with PrototypeSession() as db:
        (rev,) = _revisions(db, fid)
        assert rev.name == "Orig"
        assert sorted(json.loads(rev.member_connection_ids_json)) == sorted([c1, c2])
        assert {m.connection_id for m in db.get(CodeFamily, fid).members} == {c2, c3}


def test_replace_members_validates_ids_and_state():
    with PrototypeSession() as db:
        rq, other = _rq(db), _rq(db)
        c1 = _connection(db, rq)
        foreign = _connection(db, other)
        fid = _family(db, rq, member_ids=[c1])
        dismissed = _family(db, rq, status="dismissed")

    assert client.put(f"/code_families/{fid}/members", json={"connection_ids": [foreign]}).status_code == 422
    assert client.put(f"/code_families/{dismissed}/members", json={"connection_ids": [c1]}).status_code == 409
    assert client.put("/code_families/999999/members", json={"connection_ids": []}).status_code == 404
    with PrototypeSession() as db:
        assert _revisions(db, fid) == []
        assert {m.connection_id for m in db.get(CodeFamily, fid).members} == {c1}


def test_rename_writes_a_revision_first():
    with PrototypeSession() as db:
        rq = _rq(db)
        c1 = _connection(db, rq)
        fid = _family(db, rq, name="Before", member_ids=[c1])

    client.patch(f"/code_families/{fid}", json={"name": "After"})
    with PrototypeSession() as db:
        (rev,) = _revisions(db, fid)
        assert rev.name == "Before" and json.loads(rev.member_connection_ids_json) == [c1]
        assert db.get(CodeFamily, fid).name == "After"


# --- star ------------------------------------------------------------------------------


def test_starring_a_suggested_family_accepts_it():
    with PrototypeSession() as db:
        rq = _rq(db)
        fid = _family(db, rq, status="suggested")

    body = client.post(f"/code_families/{fid}/star", json={"starred": True}).json()
    assert body["starred"] is True and body["status"] == "accepted"

    listed = client.get(f"/research_questions/{rq}/code_families").json()
    assert [f["starred"] for f in listed if f["id"] == fid] == [True]

    body = client.post(f"/code_families/{fid}/star", json={"starred": False}).json()
    assert body["starred"] is False and body["status"] == "accepted"


def test_starring_a_dismissed_or_merged_family_is_refused():
    with PrototypeSession() as db:
        rq = _rq(db)
        dismissed = _family(db, rq, status="dismissed")
        merged = _family(db, rq, status="merged")
    assert client.post(f"/code_families/{dismissed}/star", json={"starred": True}).status_code == 409
    assert client.post(f"/code_families/{merged}/star", json={"starred": True}).status_code == 409
    assert client.post("/code_families/999999/star", json={"starred": True}).status_code == 404
    with PrototypeSession() as db:
        assert db.get(CodeFamilyStar, dismissed) is None


# --- merge -----------------------------------------------------------------------------


def test_user_merge_folds_members_keeps_the_star_and_logs_sources():
    with PrototypeSession() as db:
        rq = _rq(db)
        c1, c2, c3 = (_connection(db, rq) for _ in range(3))
        a = _family(db, rq, name="A", status="accepted", member_ids=[c1])
        b = _family(db, rq, name="B", member_ids=[c2, c3])
        db.add(CodeFamilyStar(code_family_id=b, starred=True))
        db.commit()

    resp = client.post("/code_families/merge", json={"family_ids": [a, b], "name": "A+B", "definition": "both"})
    assert resp.status_code == 200
    body = resp.json()
    fam = body["family"]
    assert fam["id"] == a  # the accepted family survives
    assert fam["name"] == "A+B" and fam["status"] == "accepted" and fam["starred"] is True
    assert sorted(m["connection_id"] for m in fam["members"]) == sorted([c1, c2, c3])
    assert [f["id"] for f in body["families"]] == [a]

    with PrototypeSession() as db:
        assert db.get(CodeFamily, b).status == "merged"
        assert {m.connection_id for m in db.get(CodeFamily, b).members} == {c2, c3}
        (rev,) = _revisions(db, a)
        assert rev.name == "A" and json.loads(rev.member_connection_ids_json) == [c1]


def test_user_merge_validation():
    with PrototypeSession() as db:
        rq, other = _rq(db), _rq(db)
        a = _family(db, rq)
        b = _family(db, rq)
        dismissed = _family(db, rq, status="dismissed")
        foreign = _family(db, other)

    def merge(ids):
        return client.post("/code_families/merge", json={"family_ids": ids, "name": "n", "definition": "d"})

    assert merge([a]).status_code == 422
    assert merge([a, a]).status_code == 422
    assert merge([a, 999_999]).status_code == 404
    assert merge([a, dismissed]).status_code == 409
    assert merge([a, foreign]).status_code == 409
    assert client.post(
        "/code_families/merge", json={"family_ids": [a, b], "name": "", "definition": "d"}
    ).status_code == 422
    with PrototypeSession() as db:
        assert db.get(CodeFamily, a).status == "suggested" and db.get(CodeFamily, b).status == "suggested"


def test_merge_families_is_the_shared_core_of_accept_merge(monkeypatch):
    import app.services.code_families as svc

    calls = []
    real = svc.merge_families

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "merge_families", spy)
    with PrototypeSession() as db:
        rq = _rq(db)
        a, b = _family(db, rq), _family(db, rq)
        suggestion = CodeFamilyMergeSuggestion(
            user_id=USER, research_question_id=rq, family_ids_json=json.dumps([a, b]),
            target_family_id=a, proposed_name="AB", proposed_definition="d", status="suggested",
        )
        db.add(suggestion)
        db.commit()
        accept_merge(db, suggestion)
        assert len(calls) == 1 and calls[0]["name"] == "AB"
        assert len(_revisions(db, a)) == 1


def test_merge_families_direct():
    with PrototypeSession() as db:
        rq = _rq(db)
        c1 = _connection(db, rq)
        a = db.get(CodeFamily, _family(db, rq, status="accepted"))
        b = db.get(CodeFamily, _family(db, rq, member_ids=[c1]))
        target = merge_families(db, target=a, sources=[b], name="n", definition="d")
        db.commit()
        assert target.id == a.id and b.status == "merged"
        assert {m.connection_id for m in target.members} == {c1}
