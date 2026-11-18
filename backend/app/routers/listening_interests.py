"""The user's "what I want to hear next" list. Next-step generation reads the active lines."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..models_prototype import ListeningInterest, ListeningSuggestion
from ..schemas import ListeningInterestCreate, ListeningInterestUpdate
from ..services import activity, listening_suggestions
from ..services.listening_interests import active_interests, add_line, ensure_imported
from ..timefmt import iso_utc

router = APIRouter(prefix="/listening_interests", tags=["listening_interests"])
config = get_config()


def _serialize(i: ListeningInterest) -> dict:
    return {
        "id": i.id,
        "text": i.text,
        "origin": i.origin,
        "voice_memo_id": i.voice_memo_id,
        "status": i.status,
        "created_at": iso_utc(i.created_at),
        "updated_at": iso_utc(i.updated_at),
    }


def _owned(prototype_db: Session, interest_id: int, user: CurrentUser) -> ListeningInterest:
    row = prototype_db.get(ListeningInterest, interest_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "listening interest not found")
    return row


def _text(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise HTTPException(422, "text must not be empty")
    return text


@router.get("")
def list_listening_interests(
    prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    if ensure_imported(prototype_db, user.id):
        prototype_db.commit()
    return [_serialize(i) for i in active_interests(prototype_db, user.id)]


@router.post("")
def add_listening_interest(
    body: ListeningInterestCreate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    row = add_line(prototype_db, user.id, _text(body.text), "user")
    prototype_db.commit()
    prototype_db.refresh(row)
    return _serialize(row)


@router.patch("/{interest_id}")
def edit_listening_interest(
    interest_id: int, body: ListeningInterestUpdate, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    row = _owned(prototype_db, interest_id, user)
    if row.status != "active":
        raise HTTPException(409, f"this listening interest is {row.status}")
    row.text = _text(body.text)
    prototype_db.commit()
    prototype_db.refresh(row)
    return _serialize(row)


@router.delete("/{interest_id}")
def remove_listening_interest(
    interest_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    row = _owned(prototype_db, interest_id, user)
    row.status = "removed"  # kept, so the line stays auditable and isn't re-imported
    prototype_db.commit()
    return {"id": row.id, "status": row.status}


# --- lines the AI offers -----------------------------------------------------------------


def _serialize_suggestion(s: ListeningSuggestion) -> dict:
    return {"id": s.id, "text": s.text, "reason": s.reason, "status": s.status, "created_at": iso_utc(s.created_at)}


def _owned_suggestion(prototype_db: Session, suggestion_id: int, user: CurrentUser) -> ListeningSuggestion:
    row = prototype_db.get(ListeningSuggestion, suggestion_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "listening suggestion not found")
    if row.status != "suggested":
        raise HTTPException(409, f"this suggestion is already {row.status}")
    return row


@router.get("/suggestions")
def list_listening_suggestions(
    prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    """The undecided offers. Never calls the model."""
    return [_serialize_suggestion(s) for s in listening_suggestions.pending(prototype_db, user.id)]


@router.post("/suggestions/refresh")
def refresh_listening_suggestions(
    prototype_db: Session = Depends(get_prototype_db), source: CorticoSource = Depends(get_source),
    user: CurrentUser = Depends(current_user),
):
    """Paid, but only when the cache rules in services/listening_suggestions.py allow a call."""
    result = listening_suggestions.refresh(prototype_db, source, user.id, config)
    return {**result, "suggestions": [_serialize_suggestion(s) for s in result["suggestions"]]}


@router.post("/suggestions/{suggestion_id}/accept")
def accept_listening_suggestion(
    suggestion_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    s = _owned_suggestion(prototype_db, suggestion_id, user)
    line = add_line(prototype_db, user.id, s.text, "suggestion")
    s.status = "accepted"
    s.listening_interest_id = line.id
    activity.log_activity(
        prototype_db, user.id, "user", activity.LISTENING_LINE_ACCEPTED,
        f"You kept a line the AI offered: \u201c{activity.excerpt(s.text)}\u201d", listening_suggestion_id=s.id,
    )
    prototype_db.commit()
    prototype_db.refresh(line)
    return {"suggestion": _serialize_suggestion(s), "interest": _serialize(line)}


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_listening_suggestion(
    suggestion_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    s = _owned_suggestion(prototype_db, suggestion_id, user)
    s.status = "dismissed"
    activity.log_activity(
        prototype_db, user.id, "user", activity.LISTENING_LINE_DISMISSED,
        f"You turned down a line the AI offered: \u201c{activity.excerpt(s.text)}\u201d", listening_suggestion_id=s.id,
    )
    prototype_db.commit()
    return _serialize_suggestion(s)
