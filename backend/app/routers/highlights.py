from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..cortico.write_back import delete_highlight_copy, detach_deleted_highlight, push_highlight, sync_status
from ..db import get_prototype_db
from ..models_prototype import ConnectionEvidence, RLHighlight
from ..schemas import HighlightCreate, HighlightQuestionsUpdate, HighlightUpdate
from ..services import activity, cortico_highlights, questions
from ..services.highlights import create_highlight_for_user

router = APIRouter(tags=["highlights"])
config = get_config()


def _serialize(h: RLHighlight, sync: dict | None = None, research_question_ids: list[int] | None = None) -> dict:
    return {
        "cortico_sync": sync,
        "id": h.id,
        "conversation_id": h.conversation_id,
        "title": h.title,
        "notes": h.notes,
        "starred": h.starred,
        "created_at": h.created_at.isoformat(),
        "research_question_ids": research_question_ids or [],
        "snippets": [
            {"snippet_id": s.snippet_id, "start_index": s.start_index, "end_index": s.end_index}
            for s in h.snippets
        ],
    }


def serialize_highlight(prototype_db: Session, h: RLHighlight, sync: dict | None = None) -> dict:
    return _serialize(h, sync, questions.question_ids_by_highlight(prototype_db, [h.id]).get(h.id))


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
    ids = [h.id for h in highlights]
    syncs = sync_status(prototype_db, ids)
    rq_ids = questions.question_ids_by_highlight(prototype_db, ids)
    return [_serialize(h, syncs.get(h.id), rq_ids.get(h.id)) for h in highlights]


@router.get("/conversations/{conversation_id}/cortico_highlights")
def list_cortico_highlights(
    conversation_id: int,
    prototype_db: Session = Depends(get_prototype_db),
    source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
):
    """Highlights other people and tools made in Cortico on this conversation. Empty in
    snapshot mode."""
    if source.conversation(conversation_id) is None:
        raise HTTPException(404, "conversation not found")
    return cortico_highlights.list_for_conversation(prototype_db, source, user.id, conversation_id)


@router.post("/conversations/{conversation_id}/highlights")
def create_highlight(
    conversation_id: int,
    body: HighlightCreate,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    highlight, sync = create_highlight_for_user(prototype_db, source, user.id, conversation_id, body)
    return serialize_highlight(prototype_db, highlight, sync)


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
    return serialize_highlight(
        prototype_db, highlight, {"status": row.status, "cortico_id": row.cortico_id, "error": row.error}
    )


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
    return serialize_highlight(prototype_db, highlight, sync_status(prototype_db, [highlight.id]).get(highlight.id))


@router.put("/highlights/{highlight_id}/questions")
def set_highlight_questions(
    highlight_id: int,
    body: HighlightQuestionsUpdate,
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Replaces the research questions this highlight is filed under. An empty list files it
    under none. A merged question is stored as the question it was merged into. The links
    stay local: Cortico highlights have no research questions."""
    highlight = _owned_highlight(prototype_db, highlight_id, user)
    rq_ids = questions.checked_question_ids(prototype_db, user.id, body.research_question_ids)
    if questions.set_highlight_questions(prototype_db, highlight, rq_ids):
        activity.log_activity(
            prototype_db, user.id, "user", activity.HIGHLIGHT_FILED,
            f"You filed a highlight under {activity.plural(len(rq_ids), 'question')}."
            if rq_ids
            else "You took a highlight out of its questions.",
            highlight_id=highlight.id, conversation_id=highlight.conversation_id, research_question_ids=rq_ids,
        )
    prototype_db.commit()
    prototype_db.refresh(highlight)
    return serialize_highlight(prototype_db, highlight, sync_status(prototype_db, [highlight.id]).get(highlight.id))


@router.delete("/highlights/{highlight_id}")
def delete_highlight(
    highlight_id: int,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    highlight = _owned_highlight(prototype_db, highlight_id, user)
    # Evidence that cited the highlight keeps its passage and loses the link, before anything
    # is deleted in Cortico: a local delete that failed after it would lose the Cortico copy.
    prototype_db.query(ConnectionEvidence).filter(ConnectionEvidence.highlight_id == highlight.id).update(
        {ConnectionEvidence.highlight_id: None}, synchronize_session=False
    )
    prototype_db.flush()
    sync = None
    if source.kind == "cortico_api":
        row = delete_highlight_copy(prototype_db, source, highlight.id)
        if row is None or row.cortico_id is None:
            sync = {"status": "not_in_cortico", "cortico_id": None, "error": None}
        else:
            sync = {"status": row.status, "cortico_id": row.cortico_id, "error": row.error}
    detach_deleted_highlight(prototype_db, highlight.id)
    questions.delete_links_for_highlight(prototype_db, highlight.id)
    prototype_db.delete(highlight)
    prototype_db.commit()
    return {"deleted": True, "cortico_sync": sync}
