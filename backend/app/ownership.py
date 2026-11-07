"""Load a row by id only if the signed-in user owns it.

A row owned by someone else raises the same 404 as a missing row, so a caller can't
learn which ids exist."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .models_prototype import (
    CodeFamily,
    CodeFamilyMergeSuggestion,
    NextStepSuggestion,
    ReflectionConnection,
    ResearchQuestion,
    RQMerge,
    VoiceMemo,
)


def _owned(db: Session, model, row_id: int, user: CurrentUser, detail: str):
    row = db.get(model, row_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, detail)
    return row


def owned_rq(db: Session, rq_id: int, user: CurrentUser) -> ResearchQuestion:
    return _owned(db, ResearchQuestion, rq_id, user, "research question not found")


def owns_rq(db: Session, rq_id: int, user: CurrentUser) -> bool:
    rq = db.get(ResearchQuestion, rq_id)
    return rq is not None and rq.user_id == user.id


def owned_rq_merge(db: Session, merge_id: int, user: CurrentUser) -> RQMerge:
    return _owned(db, RQMerge, merge_id, user, "research question merge not found")


def owned_voice_memo(db: Session, voice_memo_id: int, user: CurrentUser) -> VoiceMemo:
    return _owned(db, VoiceMemo, voice_memo_id, user, "voice memo not found")


def owned_connection(db: Session, connection_id: int, user: CurrentUser) -> ReflectionConnection | None:
    """The connection if its voice memo belongs to the user, else None."""
    connection = db.get(ReflectionConnection, connection_id)
    if connection is None:
        return None
    vm = db.get(VoiceMemo, connection.voice_memo_id)
    if vm is None or vm.user_id != user.id:
        return None
    return connection


def owned_family(db: Session, family_id: int, user: CurrentUser) -> CodeFamily:
    return _owned(db, CodeFamily, family_id, user, "code family not found")


def owned_merge_suggestion(db: Session, merge_id: int, user: CurrentUser) -> CodeFamilyMergeSuggestion:
    return _owned(db, CodeFamilyMergeSuggestion, merge_id, user, "merge suggestion not found")


def owned_next_step(db: Session, suggestion_id: int, user: CurrentUser) -> NextStepSuggestion:
    return _owned(db, NextStepSuggestion, suggestion_id, user, "suggestion not found")
