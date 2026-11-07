from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..models_prototype import SectionCheckpoint
from ..schemas import CheckpointSet

router = APIRouter(prefix="/conversations/{conversation_id}/checkpoints", tags=["checkpoints"])
config = get_config()


@router.put("")
def set_checkpoint(
    conversation_id: int,
    body: CheckpointSet,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    conversation = source.conversation(conversation_id)
    if conversation is None:
        raise HTTPException(404, "conversation not found")
    valid_question_ids = {q.id for q in source.guide_questions(conversation)}
    if body.guide_question_id not in valid_question_ids:
        raise HTTPException(400, "guide_question_id is not part of this conversation's guide")

    # At most one checkpoint per guide question per conversation — moving replaces the old one.
    existing = (
        prototype_db.query(SectionCheckpoint)
        .filter(
            SectionCheckpoint.user_id == user.id,
            SectionCheckpoint.conversation_id == conversation_id,
            SectionCheckpoint.guide_question_id == body.guide_question_id,
        )
        .one_or_none()
    )
    if existing:
        existing.snippet_id = body.snippet_id
    else:
        prototype_db.add(
            SectionCheckpoint(
                user_id=user.id,
                conversation_id=conversation_id,
                guide_question_id=body.guide_question_id,
                snippet_id=body.snippet_id,
            )
        )
    prototype_db.commit()
    return {"guide_question_id": body.guide_question_id, "snippet_id": body.snippet_id}


@router.delete("/{guide_question_id}")
def delete_checkpoint(
    conversation_id: int, guide_question_id: int, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    existing = (
        prototype_db.query(SectionCheckpoint)
        .filter(
            SectionCheckpoint.user_id == user.id,
            SectionCheckpoint.conversation_id == conversation_id,
            SectionCheckpoint.guide_question_id == guide_question_id,
        )
        .one_or_none()
    )
    if existing is None:
        raise HTTPException(404, "checkpoint not found")
    prototype_db.delete(existing)
    prototype_db.commit()
    return {"deleted": True}
