from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource, as_source
from ..db import get_prototype_db
from ..models_prototype import NextStepSuggestion
from ..ownership import owned_next_step
from ..services.next_steps import generate_next_step_suggestions

router = APIRouter(prefix="/next_steps", tags=["next_steps"])
config = get_config()


def _serialize(s: NextStepSuggestion, titles: dict[int, str]) -> dict:
    return {
        "id": s.id,
        "research_question_id": s.research_question_id,
        "suggested_direction": s.suggested_direction,
        "recommended_conversation_id": s.recommended_conversation_id,
        "recommended_conversation_title": titles.get(s.recommended_conversation_id)
        if s.recommended_conversation_id
        else None,
        "rationale": s.rationale,
        "status": s.status,
        "ai_metadata": json.loads(s.ai_metadata) if s.ai_metadata else None,
        "created_at": s.created_at.isoformat(),
    }


def _titles_for(cortico_db: Session, suggestions: list[NextStepSuggestion]) -> dict[int, str]:
    conv_ids = {s.recommended_conversation_id for s in suggestions if s.recommended_conversation_id}
    if not conv_ids:
        return {}
    return {cid: c.title for cid, c in as_source(cortico_db).conversations_by_ids(conv_ids).items()}


@router.post("/generate")
def generate(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    created = generate_next_step_suggestions(
        prototype_db=prototype_db, cortico_db=cortico_db, config=config, user_id=user.id
    )
    titles = _titles_for(cortico_db, created)
    return [_serialize(s, titles) for s in created]


@router.get("")
def list_suggestions(
    include_dismissed: bool = False,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    query = prototype_db.query(NextStepSuggestion).filter(NextStepSuggestion.user_id == user.id)
    if not include_dismissed:
        query = query.filter(NextStepSuggestion.status != "dismissed")
    suggestions = query.order_by(NextStepSuggestion.created_at.desc()).all()
    titles = _titles_for(cortico_db, suggestions)
    return [_serialize(s, titles) for s in suggestions]


@router.post("/{suggestion_id}/accept")
def accept(
    suggestion_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    s = owned_next_step(prototype_db, suggestion_id, user)
    s.status = "accepted"
    prototype_db.commit()
    return {"id": s.id, "status": s.status}


@router.post("/{suggestion_id}/dismiss")
def dismiss(
    suggestion_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    s = owned_next_step(prototype_db, suggestion_id, user)
    s.status = "dismissed"
    prototype_db.commit()
    return {"id": s.id, "status": s.status}
