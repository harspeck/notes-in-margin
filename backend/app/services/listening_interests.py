"""The user's "what I want to hear next" lines (`rl_listening_interest`).

Before this table, the lines lived only in `voice_memo.extracted_interests`, one JSON list
per reflection. That column is still written by the AI review; this table is what the user
edits and what next-step generation reads.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models_prototype import ListeningInterest, VoiceMemo


def _clean(text) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _memo_interests(vm: VoiceMemo) -> list[str]:
    try:
        raw = json.loads(vm.extracted_interests) if vm.extracted_interests else []
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [t for t in (_clean(x) for x in raw) if t]


def ensure_imported(prototype_db: Session, user_id: int) -> int:
    """One-time import for a user with no rows at all: copies the interests the AI review
    already extracted from their voice memos. Adds to the session without committing and
    returns how many lines it added. A user who has any row, removed ones included, is
    left alone."""
    if prototype_db.query(ListeningInterest.id).filter(ListeningInterest.user_id == user_id).first():
        return 0
    seen: set[str] = set()
    memos = prototype_db.query(VoiceMemo).filter(VoiceMemo.user_id == user_id).order_by(VoiceMemo.recorded_at, VoiceMemo.id)
    for vm in memos:
        for text in _memo_interests(vm):
            if text in seen:
                continue
            seen.add(text)
            prototype_db.add(
                ListeningInterest(
                    user_id=user_id, text=text, origin="reflection", voice_memo_id=vm.id, status="active",
                    created_at=vm.recorded_at,
                )
            )
    if seen:
        prototype_db.flush()
    return len(seen)


def active_interests(prototype_db: Session, user_id: int) -> list[ListeningInterest]:
    return (
        prototype_db.query(ListeningInterest)
        .filter(ListeningInterest.user_id == user_id, ListeningInterest.status == "active")
        .order_by(ListeningInterest.created_at, ListeningInterest.id)
        .all()
    )


def add_line(prototype_db: Session, user_id: int, text: str, origin: str) -> ListeningInterest:
    """Adds one active line: the user's own, or a suggestion they accepted. Adds to the
    session without committing."""
    # Import first, or this row would make the user look already imported.
    ensure_imported(prototype_db, user_id)
    row = ListeningInterest(user_id=user_id, text=text, origin=origin, status="active")
    prototype_db.add(row)
    prototype_db.flush()
    return row


def add_from_reflection(prototype_db: Session, voice_memo: VoiceMemo, interests: list) -> list[ListeningInterest]:
    """Adds the interests a review extracted, skipping exact duplicates of the user's active
    lines. Call before `voice_memo.extracted_interests` is overwritten, so the one-time
    import sees the earlier value. Adds to the session without committing."""
    ensure_imported(prototype_db, voice_memo.user_id)
    existing = {i.text for i in active_interests(prototype_db, voice_memo.user_id)}
    added: list[ListeningInterest] = []
    for raw in interests or []:
        text = _clean(raw)
        if not text or text in existing:
            continue
        existing.add(text)
        row = ListeningInterest(
            user_id=voice_memo.user_id, text=text, origin="reflection", voice_memo_id=voice_memo.id, status="active"
        )
        prototype_db.add(row)
        added.append(row)
    if added:
        prototype_db.flush()
    return added
