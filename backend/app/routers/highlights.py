from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..cortico.write_back import delete_highlight_copy, push_highlight, sync_status
from ..db import get_prototype_db
from ..models_prototype import HighlightSnippet, RLHighlight
from ..schemas import HighlightCreate, HighlightUpdate

router = APIRouter(tags=["highlights"])
config = get_config()


def _serialize(h: RLHighlight, sync: dict | None = None) -> dict:
    return {
        "cortico_sync": sync,
        "id": h.id,
        "conversation_id": h.conversation_id,
        "title": h.title,
        "notes": h.notes,
        "starred": h.starred,
        "created_at": h.created_at.isoformat(),
        "snippets": [
            {"snippet_id": s.snippet_id, "start_index": s.start_index, "end_index": s.end_index}
            for s in h.snippets
        ],
    }


@router.get("/conversations/{conversation_id}/highlights")
def list_highlights(conversation_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)):
    highlights = (
        prototype_db.query(RLHighlight)
        .filter(
            RLHighlight.user_id == user.id,
            RLHighlight.conversation_id == conversation_id,
        )
        .order_by(RLHighlight.created_at)
        .all()
    )
    syncs = sync_status(prototype_db, [h.id for h in highlights])
    return [_serialize(h, syncs.get(h.id)) for h in highlights]


@router.post("/conversations/{conversation_id}/highlights")
def create_highlight(
    conversation_id: int,
    body: HighlightCreate,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    conversation = source.conversation(conversation_id)
    if conversation is None:
        raise HTTPException(404, "conversation not found")
    if conversation.project_id is None:
        raise HTTPException(409, "This conversation isn't in one of your workspace's projects.")

    highlight = RLHighlight(
        user_id=user.id,
        conversation_id=conversation_id,
        project_id=conversation.project_id,
        title=body.title,
        notes=body.notes,
    )
    prototype_db.add(highlight)
    prototype_db.flush()
    for snip in body.snippets:
        prototype_db.add(
            HighlightSnippet(
                highlight_id=highlight.id,
                snippet_id=snip.snippet_id,
                start_index=snip.start_index,
                end_index=snip.end_index,
            )
        )
    prototype_db.commit()
    prototype_db.refresh(highlight)
    sync = None
    if source.kind == "cortico_api" and config.cortico.write_back.highlights:
        row = push_highlight(prototype_db, source, highlight, conversation, tool_id=config.cortico.tool_id)
        sync = {"status": row.status, "cortico_id": row.cortico_id, "error": row.error}
    return _serialize(highlight, sync)


def _owned_highlight(prototype_db: Session, highlight_id: int, user: CurrentUser) -> RLHighlight:
    highlight = prototype_db.get(RLHighlight, highlight_id)
    if highlight is None or highlight.user_id != user.id:
        raise HTTPException(404, "highlight not found")
    return highlight


@router.post("/highlights/{highlight_id}/cortico_sync")
def retry_highlight_sync(
    highlight_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Retries pushing a highlight to Cortico. Safe to call repeatedly: a highlight that
    already has a Cortico copy is left alone."""
    if source.kind != "cortico_api":
        raise HTTPException(400, "Write-back only runs when DATA_SOURCE is cortico_api.")
    highlight = _owned_highlight(prototype_db, highlight_id, user)
    conversation = source.conversation(highlight.conversation_id)
    if conversation is None:
        raise HTTPException(404, "conversation not found")
    row = push_highlight(prototype_db, source, highlight, conversation, tool_id=config.cortico.tool_id)
    return _serialize(highlight, {"status": row.status, "cortico_id": row.cortico_id, "error": row.error})


@router.patch("/highlights/{highlight_id}")
def update_highlight(
    highlight_id: int,
    body: HighlightUpdate,
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    # Title, notes and star stay local: Cortico's highlight update takes only title and
    # metadata, and the margin note is this app's own annotation.
    highlight = _owned_highlight(prototype_db, highlight_id, user)
    if body.title is not None:
        highlight.title = body.title
    if body.notes is not None:
        highlight.notes = body.notes
    if body.starred is not None:
        highlight.starred = body.starred
    prototype_db.commit()
    prototype_db.refresh(highlight)
    return _serialize(highlight, sync_status(prototype_db, [highlight.id]).get(highlight.id))


@router.delete("/highlights/{highlight_id}")
def delete_highlight(
    highlight_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    highlight = _owned_highlight(prototype_db, highlight_id, user)
    sync = None
    if source.kind == "cortico_api":
        row = delete_highlight_copy(prototype_db, source, highlight.id)
        sync = {"status": row.status, "cortico_id": row.cortico_id, "error": row.error} if row else None
    prototype_db.delete(highlight)
    prototype_db.commit()
    return {"deleted": True, "cortico_sync": sync}
