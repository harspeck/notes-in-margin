"""Routes keyed by an object id only serve that object's owner. Anyone else gets the same
404 as for an id that doesn't exist, so ids can't be probed."""
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import (
    AppSetting,
    CodeFamily,
    CodeFamilyMergeSuggestion,
    NextStepSuggestion,
    ReflectionConnection,
    ResearchQuestion,
    SectionCheckpoint,
    VoiceMemo,
)
from app.services.ai_review import DEFAULT_REVIEW_PROMPT, REVIEW_PROMPT_SETTING_KEY

client = TestClient(app)

OWNER = get_config().default_user_id
OTHER = 424242
MISSING = 999_999_999


@contextmanager
def acting_as(user_id: int):
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id)
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_user, None)


def _seed(*rows):
    db = PrototypeSession()
    try:
        for row in rows:
            db.add(row)
            db.commit()
            db.refresh(row)
        return [row.id for row in rows]
    finally:
        db.close()


def _rq(name="owned rq") -> int:
    (rq_id,) = _seed(ResearchQuestion(user_id=OWNER, project_id=5, name=name, display_order=0))
    return rq_id


def _voice_memo() -> int:
    (vm_id,) = _seed(
        VoiceMemo(
            user_id=OWNER,
            conversation_id=9,
            audio_path="db/local/audio/test.webm",
            duration=5.0,
            transcription_edited="owner's reflection",
            status="ready_for_review",
        )
    )
    return vm_id


def _filed_connection(rq_id: int) -> int:
    (connection_id,) = _seed(
        ReflectionConnection(
            voice_memo_id=_voice_memo(), research_question_id=rq_id, insight_text="owner's insight",
            source="user", status="accepted",
        )
    )
    return connection_id


def _assert_hidden(method: str, path: str, missing_path: str, **kwargs):
    """As OTHER, `path` 404s with exactly the body a nonexistent id gets."""
    with acting_as(OTHER):
        foreign = client.request(method, path, **kwargs)
        missing = client.request(method, missing_path, **kwargs)
    assert foreign.status_code == 404, foreign.text
    assert missing.status_code == 404, missing.text
    assert foreign.json() == missing.json()


def test_research_question_routes_hide_other_users_rqs():
    rq_id = _rq()
    target_id = _rq("owned target")

    _assert_hidden("PATCH", f"/research_questions/{rq_id}", f"/research_questions/{MISSING}", json={"name": "x"})
    _assert_hidden("POST", f"/research_questions/{rq_id}/focus", f"/research_questions/{MISSING}/focus",
                   json={"focused": True})
    _assert_hidden("POST", f"/research_questions/{rq_id}/merge_into", f"/research_questions/{MISSING}/merge_into",
                   json={"target_id": target_id})

    with acting_as(OTHER):
        other_rq = client.post("/research_questions", json={"name": "other's rq"}).json()
        # Merging their own RQ into the owner's must not reveal or touch the owner's RQ.
        resp = client.post(f"/research_questions/{other_rq['id']}/merge_into", json={"target_id": rq_id})
        assert resp.status_code == 404
        client.put("/research_questions/reorder", json={"ordered_ids": [other_rq["id"], rq_id]})

    db = PrototypeSession()
    try:
        assert db.get(ResearchQuestion, rq_id).display_order == 0
    finally:
        db.close()

    resp = client.patch(f"/research_questions/{rq_id}", json={"name": "renamed by owner"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed by owner"

    merged = client.post(f"/research_questions/{rq_id}/merge_into", json={"target_id": target_id})
    assert merged.status_code == 200
    merge_id = merged.json()["merge"]["id"]
    _assert_hidden("POST", f"/rq_merges/{merge_id}/undo", f"/rq_merges/{MISSING}/undo")
    assert client.post(f"/rq_merges/{merge_id}/undo").status_code == 200


def test_memo_connection_routes_hide_other_users_entries():
    rq_id = _rq()
    connection_id = _filed_connection(rq_id)

    _assert_hidden("PATCH", f"/memo/connections/{connection_id}", f"/memo/connections/{MISSING}",
                   json={"insight_text": "hijacked"})
    _assert_hidden("POST", f"/memo/connections/{connection_id}/retract", f"/memo/connections/{MISSING}/retract")
    with acting_as(OTHER):
        client.put("/memo/reorder", json={"research_question_id": rq_id, "ordered_connection_ids": [connection_id]})

    db = PrototypeSession()
    try:
        c = db.get(ReflectionConnection, connection_id)
        assert (c.status, c.insight_text, c.memo_order) == ("accepted", "owner's insight", None)
    finally:
        db.close()

    resp = client.post(f"/memo/connections/{connection_id}/retract")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retracted"


def test_voice_memo_routes_hide_other_users_memos():
    vm_id = _voice_memo()

    _assert_hidden("GET", f"/voice_memos/{vm_id}", f"/voice_memos/{MISSING}")
    _assert_hidden("PATCH", f"/voice_memos/{vm_id}", f"/voice_memos/{MISSING}",
                   json={"transcription_edited": "hijacked"})

    resp = client.get(f"/voice_memos/{vm_id}")
    assert resp.status_code == 200
    assert resp.json()["transcription_edited"] == "owner's reflection"


def test_review_routes_hide_other_users_memos():
    vm_id = _voice_memo()
    rq_id = _rq()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq_id,
                           "insight_text": "x", "evidence": []}]}

    _assert_hidden("GET", f"/voice_memos/{vm_id}/review", f"/voice_memos/{MISSING}/review")
    _assert_hidden("POST", f"/voice_memos/{vm_id}/review", f"/voice_memos/{MISSING}/review")
    _assert_hidden("POST", f"/voice_memos/{vm_id}/file", f"/voice_memos/{MISSING}/file", json=body)

    # Filing onto someone else's research question is refused like a missing one.
    other_vm_id = _seed(VoiceMemo(user_id=OTHER, conversation_id=9, audio_path="x", status="ready_for_review"))[0]
    with acting_as(OTHER):
        foreign = client.post(f"/voice_memos/{other_vm_id}/file", json=body)
        missing_body = {"decisions": [{**body["decisions"][0], "research_question_id": MISSING}]}
        missing = client.post(f"/voice_memos/{other_vm_id}/file", json=missing_body)
    assert foreign.status_code == 400
    assert foreign.json()["detail"] == f"research question {rq_id} not found"
    assert missing.status_code == 400

    assert client.get(f"/voice_memos/{vm_id}/review").status_code == 200
    filed = client.post(f"/voice_memos/{vm_id}/file", json=body)
    assert filed.status_code == 200
    assert filed.json()["connections"][0]["research_question_id"] == rq_id


def test_code_family_routes_hide_other_users_families():
    rq_id = _rq()
    family_id, other_family_id = _seed(
        CodeFamily(user_id=OWNER, research_question_id=rq_id, name="A", definition="d", status="suggested"),
        CodeFamily(user_id=OWNER, research_question_id=rq_id, name="B", definition="d", status="accepted"),
    )
    (merge_id,) = _seed(
        CodeFamilyMergeSuggestion(
            user_id=OWNER, research_question_id=rq_id, family_ids_json=f"[{family_id}, {other_family_id}]",
            target_family_id=other_family_id, proposed_name="A+B", proposed_definition="d", status="suggested",
        )
    )

    rq_path, missing_rq = f"/research_questions/{rq_id}", f"/research_questions/{MISSING}"
    _assert_hidden("GET", f"{rq_path}/code_families", f"{missing_rq}/code_families")
    _assert_hidden("GET", f"{rq_path}/code_families/state", f"{missing_rq}/code_families/state")
    _assert_hidden("POST", f"{rq_path}/code_families/synthesize", f"{missing_rq}/code_families/synthesize")
    _assert_hidden("POST", f"{rq_path}/code_families", f"{missing_rq}/code_families",
                   json={"name": "n", "definition": "d"})
    fam, missing_fam = f"/code_families/{family_id}", f"/code_families/{MISSING}"
    _assert_hidden("PATCH", fam, missing_fam, json={"name": "hijacked"})
    _assert_hidden("PUT", f"{fam}/members", f"{missing_fam}/members", json={"connection_ids": []})
    _assert_hidden("POST", f"{fam}/star", f"{missing_fam}/star", json={"starred": True})
    _assert_hidden("POST", f"{fam}/accept", f"{missing_fam}/accept")
    _assert_hidden("POST", f"{fam}/dismiss", f"{missing_fam}/dismiss")
    _assert_hidden("POST", "/code_families/merge", "/code_families/merge",
                   json={"family_ids": [family_id, other_family_id], "name": "n", "definition": "d"})
    _assert_hidden("POST", f"/code_family_merges/{merge_id}/dismiss", f"/code_family_merges/{MISSING}/dismiss")
    _assert_hidden("POST", f"/code_family_merges/{merge_id}/accept", f"/code_family_merges/{MISSING}/accept")

    db = PrototypeSession()
    try:
        assert db.get(CodeFamily, family_id).name == "A"
        assert db.get(CodeFamily, family_id).status == "suggested"
        assert db.get(CodeFamilyMergeSuggestion, merge_id).status == "suggested"
    finally:
        db.close()

    listed = client.get(f"{rq_path}/code_families")
    assert listed.status_code == 200
    assert {f["id"] for f in listed.json()} == {family_id, other_family_id}
    assert client.post(f"{fam}/accept").json() == {"id": family_id, "status": "accepted"}
    assert client.post(f"/code_family_merges/{merge_id}/dismiss").json()["status"] == "dismissed"


def test_next_step_routes_hide_other_users_suggestions():
    (suggestion_id,) = _seed(
        NextStepSuggestion(user_id=OWNER, suggested_direction="owner's", rationale="r", status="suggested")
    )

    _assert_hidden("POST", f"/next_steps/{suggestion_id}/accept", f"/next_steps/{MISSING}/accept")
    _assert_hidden("POST", f"/next_steps/{suggestion_id}/dismiss", f"/next_steps/{MISSING}/dismiss")

    assert client.post(f"/next_steps/{suggestion_id}/accept").json() == {"id": suggestion_id, "status": "accepted"}


def test_checkpoints_and_transcript_state_are_per_user():
    _seed(SectionCheckpoint(user_id=OWNER, conversation_id=9, guide_question_id=777001, snippet_id=1))

    with acting_as(OTHER):
        assert client.delete("/conversations/9/checkpoints/777001").status_code == 404

    client.put("/conversations/9/state", json={"prompt_dismissed": True})
    try:
        with acting_as(OTHER):
            other_view = client.get("/conversations/9").json()
        assert other_view["transcript_state"]["prompt_dismissed_at"] is None
        assert client.get("/conversations/9").json()["transcript_state"]["prompt_dismissed_at"] is not None
    finally:
        client.put("/conversations/9/state", json={"prompt_dismissed": False})

    assert client.delete("/conversations/9/checkpoints/777001").status_code == 200


def test_organize_lists_only_the_callers_research_questions():
    rq_id = _rq("owner's organize rq")
    with acting_as(OTHER):
        organize = client.get("/organize").json()
    shown = {g["rq"]["id"] for g in organize["focused"] + organize["other"]}
    assert rq_id not in shown
    owner_view = client.get("/organize").json()
    assert rq_id in {g["rq"]["id"] for g in owner_view["focused"] + owner_view["other"]}


def test_review_prompt_is_per_user_and_only_the_legacy_user_sees_the_global_one():
    db = PrototypeSession()
    try:
        db.add(AppSetting(key=REVIEW_PROMPT_SETTING_KEY, value="the original global prompt"))
        db.commit()
    finally:
        db.close()
    try:
        # The legacy single user (101) with no prompt of their own still gets the global one.
        assert client.get("/settings/review-prompt").json()["text"] == "the original global prompt"

        assert client.put("/settings/review-prompt", json={"text": "owner's prompt"}).status_code == 200
        assert client.get("/settings/review-prompt").json()["text"] == "owner's prompt"
        with acting_as(OTHER):
            # Anyone else starts from the default, never another user's prompt.
            assert client.get("/settings/review-prompt").json()["text"] == DEFAULT_REVIEW_PROMPT
            client.put("/settings/review-prompt", json={"text": "other's prompt"})
            assert client.post("/settings/review-prompt/reset").json()["text"] == DEFAULT_REVIEW_PROMPT
            # A reset is the default, not the global fallback.
            assert client.get("/settings/review-prompt").json()["text"] == DEFAULT_REVIEW_PROMPT
        assert client.get("/settings/review-prompt").json()["text"] == "owner's prompt"
    finally:
        db = PrototypeSession()
        try:
            for key in (REVIEW_PROMPT_SETTING_KEY, f"{REVIEW_PROMPT_SETTING_KEY}:{OWNER}",
                        f"{REVIEW_PROMPT_SETTING_KEY}:{OTHER}"):
                row = db.get(AppSetting, key)
                if row is not None:
                    db.delete(row)
            db.commit()
        finally:
            db.close()
