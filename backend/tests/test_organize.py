"""The Organize page payload, RQ focus, and the codebook-prep export. Both GET routes
filter by config.default_user_id, and other test files also create RQs for that user, so
assertions here look up rows by id rather than counting everything."""
from fastapi.testclient import TestClient

from app.config import get_config
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import (
    CodeFamily,
    CodeFamilyMember,
    CodeFamilyStar,
    ConnectionEvidence,
    ReflectionConnection,
    ResearchQuestion,
    VoiceMemo,
)

client = TestClient(app)
USER = get_config().default_user_id


def _rq(db, name="organize rq") -> int:
    rq = ResearchQuestion(user_id=USER, project_id=5, name=name, description="desc")
    db.add(rq)
    db.commit()
    return rq.id


def _connection(db, rq_id, status="accepted", insight="an insight", evidence=()) -> int:
    vm = VoiceMemo(user_id=USER, conversation_id=9, audio_path="x", status="filed")
    db.add(vm)
    db.commit()
    c = ReflectionConnection(
        voice_memo_id=vm.id, research_question_id=rq_id, insight_text=insight, source="user", status=status
    )
    db.add(c)
    db.commit()
    for snippet_id, start, end in evidence:
        db.add(ConnectionEvidence(connection_id=c.id, snippet_id=snippet_id, start_index=start, end_index=end, source="ai"))
    db.commit()
    return c.id


def _family(db, rq_id, name="fam", status="accepted", member_ids=(), starred=False) -> int:
    f = CodeFamily(user_id=USER, research_question_id=rq_id, name=name, definition="def", status=status)
    db.add(f)
    db.commit()
    for cid in member_ids:
        db.add(CodeFamilyMember(code_family_id=f.id, connection_id=cid))
    if starred:
        db.add(CodeFamilyStar(code_family_id=f.id, starred=True))
    db.commit()
    return f.id


def _focus(rq_id, focused=True):
    return client.post(f"/research_questions/{rq_id}/focus", json={"focused": focused})


def _groups(payload) -> dict[int, dict]:
    return {g["rq"]["id"]: g for g in payload["focused"] + payload["other"]}


def test_focus_toggles_and_validates():
    with PrototypeSession() as db:
        rq, src, tgt = _rq(db), _rq(db), _rq(db)
    resp = _focus(rq)
    assert resp.status_code == 200 and resp.json()["focused"] is True
    assert _focus(rq, False).json()["focused"] is False
    assert _focus(999_999).status_code == 404

    client.post(f"/research_questions/{src}/merge_into", json={"target_id": tgt})
    assert _focus(src).status_code == 409


def test_organize_groups_fold_merged_sources_into_their_target():
    with PrototypeSession() as db:
        src, tgt, other = _rq(db, "org source"), _rq(db, "org target"), _rq(db, "org other")
        cs = _connection(db, src, insight="source entry")
        ct = _connection(db, tgt, insight="target entry")
        _connection(db, tgt, status="suggested")
        fs = _family(db, src, name="source fam", status="suggested", member_ids=[cs])
        _family(db, tgt, name="dismissed", status="dismissed")
    merge_id = client.post(f"/research_questions/{src}/merge_into", json={"target_id": tgt}).json()["merge"]["id"]
    _focus(tgt)

    payload = client.get("/organize").json()
    assert payload["focus_range"] == [5, 7]
    assert payload["focus_count"] == len(payload["focused"])
    focused_ids = [g["rq"]["id"] for g in payload["focused"]]
    assert tgt in focused_ids
    groups = _groups(payload)
    assert src not in groups and other in groups and other not in focused_ids

    g = groups[tgt]
    assert g["merged_sources"] == [{"merge_id": merge_id, "id": src, "name": "org source", "into_id": tgt}]
    assert g["entry_count"] == 2
    entries = {e["connection_id"]: e for e in g["entries"]}
    assert set(entries) == {cs, ct}
    assert entries[cs]["family_ids"] == [fs] and entries[ct]["family_ids"] == []
    assert entries[cs]["research_question_id"] == src
    assert entries[cs]["conversation_id"] == 9 and entries[cs]["conversation_title"]
    assert [f["id"] for f in g["families"]] == [fs]
    assert g["families"][0]["starred"] is False
    assert g["cap"] == {"live_count": 1, "max_per_rq": 5, "near_cap": False, "at_cap": False}


def test_codebook_prep_shape_and_filters():
    with PrototypeSession() as db:
        src, rq, unfocused = _rq(db, "cb source"), _rq(db, "cb rq"), _rq(db, "cb unfocused")
        c1 = _connection(db, rq, insight="kept", evidence=[(1936, 0, 4)])
        c2 = _connection(db, src, insight="from source")
        retracted = _connection(db, rq, status="retracted")
        starred = _family(db, rq, name="Starred", member_ids=[c1, c2, retracted], starred=True)
        _family(db, rq, name="Not starred", member_ids=[c1])
        _family(db, rq, name="Suggested", status="suggested")
        _family(db, rq, name="Dismissed but starred", status="dismissed", starred=True)
        _family(db, unfocused, name="Unfocused", starred=True)
    client.post(f"/research_questions/{src}/merge_into", json={"target_id": rq})
    _focus(rq)

    prep = client.get("/codebook_prep").json()
    assert set(prep) == {"generated_at", "user_id", "research_questions", "warnings"}
    assert prep["user_id"] == USER
    by_id = {r["id"]: r for r in prep["research_questions"]}
    assert unfocused not in by_id and src not in by_id

    r = by_id[rq]
    assert r["name"] == "cb rq" and r["description"] == "desc" and r["merged_from"] == [src]
    (code,) = r["codes"]
    assert code["family_id"] == starred and code["name"] == "Starred" and code["definition"] == "def"
    assert code["status"] == "accepted"
    entries = {e["connection_id"]: e for e in code["entries"]}
    assert set(entries) == {c1, c2}  # the retracted entry is left out
    assert entries[c1]["insight_text"] == "kept" and entries[c1]["conversation_id"] == 9
    (ev,) = entries[c1]["evidence"]
    assert (ev["snippet_id"], ev["start_index"], ev["end_index"], ev["source"]) == (1936, 0, 4, "ai")
    assert ev["text"]

    warnings = " ".join(prep["warnings"])
    assert "suggested code families" in warnings
    count = len(prep["research_questions"])
    assert ("focused" in warnings and "5-7" in warnings) == (count < 5 or count > 7)


def test_codebook_prep_warns_about_a_focused_rq_without_starred_families():
    with PrototypeSession() as db:
        rq = _rq(db, "nothing starred here")
        _family(db, rq, name="plain")
    _focus(rq)
    prep = client.get("/codebook_prep").json()
    assert any("nothing starred here" in w and "no starred" in w for w in prep["warnings"])
    by_id = {r["id"]: r for r in prep["research_questions"]}
    assert by_id[rq]["codes"] == []
