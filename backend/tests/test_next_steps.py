"""Structural validation + gathering logic for next-step suggestions, mirroring the
pattern in test_ai_review_validation.py: pure helpers are unit-tested directly against
real Cortico ids; the live gpt-5.6-luna round trip itself is exercised manually (see
docs/prototype-log.md) rather than in automated tests."""
import json

from fastapi.testclient import TestClient
import pytest

from app.auth import CurrentUser, current_user
from app.db import CorticoSession, PrototypeSession
from app.main import app
from app.models_prototype import NextStepSuggestion, RLHighlight, VoiceMemo
from app.services.next_steps import (
    _all_extracted_interests,
    _engaged_conversation_ids,
    _valid_conversation_ids,
)

client = TestClient(app)


def test_valid_conversation_ids_keeps_real_ones_and_drops_fake_ones():
    db = CorticoSession()
    try:
        result = _valid_conversation_ids(db, {9, 10, 999_999_999})
        assert result == {9, 10}
    finally:
        db.close()


def test_valid_conversation_ids_empty_input_is_empty_output():
    db = CorticoSession()
    try:
        assert _valid_conversation_ids(db, set()) == set()
    finally:
        db.close()


def test_extracted_interests_accumulate_across_voice_memos():
    db = PrototypeSession()
    try:
        user_id = 90001
        vm1 = VoiceMemo(
            user_id=user_id,
            conversation_id=9,
            audio_path="db/local/audio/a.webm",
            status="filed",
            extracted_interests=json.dumps(["more about libraries"]),
        )
        vm2 = VoiceMemo(
            user_id=user_id,
            conversation_id=10,
            audio_path="db/local/audio/b.webm",
            status="filed",
            extracted_interests=json.dumps(["more about civic trust"]),
        )
        db.add_all([vm1, vm2])
        db.commit()

        interests = _all_extracted_interests(db, user_id)
        assert "more about libraries" in interests
        assert "more about civic trust" in interests
    finally:
        db.close()


def test_engaged_conversation_ids_covers_highlights_and_voice_memos():
    db = PrototypeSession()
    try:
        user_id = 90002
        db.add(RLHighlight(user_id=user_id, conversation_id=5, project_id=3, notes="n"))
        db.add(VoiceMemo(user_id=user_id, conversation_id=6, audio_path="x", status="recorded"))
        db.commit()

        engaged = _engaged_conversation_ids(db, user_id)
        assert {5, 6} <= engaged
    finally:
        db.close()


@pytest.fixture
def act_as_90003():
    """Accept and dismiss only serve the suggestion's owner."""
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=90003)
    yield
    app.dependency_overrides.pop(current_user, None)


def test_accept_and_dismiss_persist_and_are_reflected_on_reload(act_as_90003):
    db = PrototypeSession()
    try:
        suggestion = NextStepSuggestion(
            user_id=90003,
            suggested_direction="hear more about libraries",
            rationale="matches an extracted interest",
            status="suggested",
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)
        suggestion_id = suggestion.id
    finally:
        db.close()

    accepted = client.post(f"/next_steps/{suggestion_id}/accept").json()
    assert accepted["status"] == "accepted"

    dismissed_id = suggestion_id
    # Re-dismiss to also check the dismiss path persists independently.
    db = PrototypeSession()
    try:
        s2 = NextStepSuggestion(
            user_id=90003,
            suggested_direction="hear more about trust",
            rationale="matches another interest",
            status="suggested",
        )
        db.add(s2)
        db.commit()
        db.refresh(s2)
        dismissed_id = s2.id
    finally:
        db.close()

    dismissed = client.post(f"/next_steps/{dismissed_id}/dismiss").json()
    assert dismissed["status"] == "dismissed"

    db = PrototypeSession()
    try:
        reloaded_accept = db.get(NextStepSuggestion, suggestion_id)
        reloaded_dismiss = db.get(NextStepSuggestion, dismissed_id)
        assert reloaded_accept.status == "accepted"
        assert reloaded_dismiss.status == "dismissed"
    finally:
        db.close()


def test_dismissed_suggestions_hidden_by_default_but_not_deleted():
    db = PrototypeSession()
    try:
        s = NextStepSuggestion(
            user_id=config_user_id(),
            suggested_direction="a suggestion to dismiss",
            rationale="r",
            status="suggested",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    client.post(f"/next_steps/{sid}/dismiss")

    default_list = client.get("/next_steps").json()
    assert all(item["id"] != sid for item in default_list)

    with_dismissed = client.get("/next_steps?include_dismissed=true").json()
    assert any(item["id"] == sid for item in with_dismissed)


def config_user_id() -> int:
    from app.config import get_config

    return get_config().default_user_id
