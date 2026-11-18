"""Conversation summaries for the user's scope: how many exist, and making the missing ones."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, current_user, get_source, workspace_project_ids
from ..config import get_config
from ..cortico.source import ConversationRef, CorticoSource
from ..db import get_prototype_db
from ..services import gists

router = APIRouter(prefix="/gists", tags=["gists"])
config = get_config()


def _scope(prototype_db: Session, source: CorticoSource, user: CurrentUser) -> list[ConversationRef]:
    project_ids = workspace_project_ids(active_workspace(prototype_db, user))
    if config.data_source == "cortico_api" and not project_ids:
        return []
    return source.scope_conversations(project_ids or None)


@router.get("/status")
def gist_status(
    prototype_db: Session = Depends(get_prototype_db), source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
):
    return gists.status(prototype_db, _scope(prototype_db, source, user))


@router.post("/generate")
def generate_gists(
    prototype_db: Session = Depends(get_prototype_db), source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
):
    """Paid: one model call per conversation, at most `gists.max_per_request` per request.
    Call again until `remaining` is 0."""
    return gists.generate_missing(prototype_db, source, _scope(prototype_db, source, user), user.id, config)
