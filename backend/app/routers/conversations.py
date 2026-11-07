from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, current_user, get_source, workspace_project_ids
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..models_prototype import SectionCheckpoint, TranscriptState
from ..schemas import TranscriptStateUpdate

router = APIRouter(prefix="/conversations", tags=["conversations"])
config = get_config()


@router.get("")
def list_conversations(
    source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
    prototype_db: Session = Depends(get_prototype_db),
):
    """Snapshot mode with no workspace keeps the full corpus (the prototype's intent). With a
    workspace, or in cortico_api mode, the list is the workspace's projects."""
    project_ids = workspace_project_ids(active_workspace(prototype_db, user))
    if config.data_source == "cortico_api" and not project_ids:
        raise HTTPException(409, "Choose the projects to work in first (POST /workspaces).")
    conversations = source.list_conversations(project_ids or None)
    return [
        {
            "id": c.id,
            "title": c.title,
            "project_id": c.project_id,
            "duration_seconds": c.duration_seconds,
            "language": c.language,
            "audio_url": c.audio_url,
            "has_audio": c.has_audio,
            "start_time": c.start_time,
            "num_participants": c.num_participants,
        }
        for c in conversations
    ]


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: int,
    source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
    prototype_db: Session = Depends(get_prototype_db),
):
    conversation = source.conversation(conversation_id)
    if conversation is None:
        raise HTTPException(404, "conversation not found")

    snippet_payload = [
        {
            "snippet_id": s.id,
            "order_index": s.order_index,
            "speaker_name": s.speaker_name,
            "is_facilitator": s.is_facilitator,
            "start_offset": s.start_offset,
            "end_offset": s.end_offset,
            "text": s.text,
            "words": s.words,
        }
        for s in source.snippets(conversation_id)
    ]

    guide_questions = source.guide_questions(conversation)

    checkpoints = (
        prototype_db.query(SectionCheckpoint)
        .filter(
            SectionCheckpoint.user_id == user.id,
            SectionCheckpoint.conversation_id == conversation_id,
        )
        .all()
    )
    checkpoint_by_question = {c.guide_question_id: c for c in checkpoints}

    guide_payload = [
        {
            "guide_question_id": q.id,
            "text": q.text,
            "shorthand_code": q.shorthand_code,
            "order_index": q.order_index,
            "checkpoint_snippet_id": checkpoint_by_question[q.id].snippet_id
            if q.id in checkpoint_by_question
            else None,
        }
        for q in guide_questions
    ]

    state = prototype_db.get(TranscriptState, (user.id, conversation_id))

    return {
        "id": conversation.id,
        "title": conversation.title,
        "project_id": conversation.project_id,
        "audio_url": conversation.audio_url,
        "has_audio": conversation.has_audio,
        "duration_seconds": conversation.duration_seconds,
        "language": conversation.language,
        "snippets": snippet_payload,
        "guide_questions": guide_payload,
        "transcript_state": {
            "reached_end_at": state.reached_end_at.isoformat() if state and state.reached_end_at else None,
            "prompt_dismissed_at": state.prompt_dismissed_at.isoformat()
            if state and state.prompt_dismissed_at
            else None,
        },
    }


@router.put("/{conversation_id}/state")
def update_transcript_state(
    conversation_id: int,
    body: TranscriptStateUpdate,
    user: CurrentUser = Depends(current_user),
    prototype_db: Session = Depends(get_prototype_db),
):
    state = prototype_db.get(TranscriptState, (user.id, conversation_id))
    if state is None:
        state = TranscriptState(user_id=user.id, conversation_id=conversation_id)
        prototype_db.add(state)

    now = datetime.now(timezone.utc)
    if body.reached_end_at:
        state.reached_end_at = now
    if body.prompt_dismissed is not None:
        state.prompt_dismissed_at = now if body.prompt_dismissed else None

    prototype_db.commit()
    return {
        "reached_end_at": state.reached_end_at.isoformat() if state.reached_end_at else None,
        "prompt_dismissed_at": state.prompt_dismissed_at.isoformat() if state.prompt_dismissed_at else None,
    }
