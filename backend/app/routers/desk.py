"""Reading state and the desk: what the signed-in user has open and what is waiting for
them. Replaces the per-browser list the frontend kept in localStorage."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, current_user, get_source, workspace_project_ids
from ..config import get_config
from ..cortico.source import ConversationRef, CorticoSource
from ..db import get_prototype_db
from ..models_prototype import ReadingState, ReflectionConnection, RLHighlight, VoiceMemo
from ..schemas import ReadingPositionUpdate
from ..timefmt import iso_utc

router = APIRouter(tags=["desk"])
config = get_config()

KEEP_READING_LIMIT = 8
# A voice memo in one of these states is not waiting on the user.
_FINISH_STATUSES = ("transcribing", "ready_for_review", "filed")


def _serialize_state(state: ReadingState) -> dict:
    return {
        "conversation_id": state.conversation_id,
        "first_opened_at": iso_utc(state.first_opened_at),
        "last_opened_at": iso_utc(state.last_opened_at),
        "position_seconds": state.position_seconds,
        "position_snippet_id": state.position_snippet_id,
    }


@router.put("/conversations/{conversation_id}/reading_position")
def set_reading_position(
    conversation_id: int,
    body: ReadingPositionUpdate | None = None,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Records that the user has this conversation open, and where they are in it if the
    body says. No body, or an empty one, only records the open. A field left out keeps its
    stored value."""
    now = datetime.now(timezone.utc)
    sent = body.model_fields_set if body is not None else set()

    def apply(state: ReadingState) -> None:
        state.last_opened_at = now
        if "position_seconds" in sent:
            state.position_seconds = body.position_seconds
        if "snippet_id" in sent:
            state.position_snippet_id = body.snippet_id

    state = prototype_db.get(ReadingState, (user.id, conversation_id))
    if state is None:
        # Checked against Cortico only on the first open, so saving a position stays cheap.
        if source.conversation(conversation_id) is None:
            raise HTTPException(404, "conversation not found")
        state = ReadingState(user_id=user.id, conversation_id=conversation_id, first_opened_at=now)
        prototype_db.add(state)
    apply(state)
    try:
        prototype_db.commit()
    except IntegrityError:
        # Two first opens at once (the reader mounts twice in development): the other
        # request created the row, so update that one.
        prototype_db.rollback()
        state = prototype_db.get(ReadingState, (user.id, conversation_id))
        if state is None:
            raise
        apply(state)
        prototype_db.commit()
    return _serialize_state(state)


def _visible(source: CorticoSource, project_ids: list[int], conversation_ids: set[int]) -> dict[int, ConversationRef]:
    """The conversations among `conversation_ids` that the user's data source shows them: the
    active workspace's projects, or in snapshot mode with no workspace, the whole corpus
    (the same scope as GET /conversations)."""
    if config.data_source == "cortico_api" and not project_ids:
        return {}
    known = source.conversations_by_ids(conversation_ids)
    if project_ids:
        allowed = set(project_ids)
        known = {cid: c for cid, c in known.items() if c.project_id in allowed}
    return known


def _project_names(source: CorticoSource, project_ids: set[int]) -> dict[int, str]:
    lookup = getattr(source, "project_names", None)
    return lookup(project_ids) if lookup and project_ids else {}


@router.get("/desk")
def get_desk(
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    project_ids = workspace_project_ids(active_workspace(prototype_db, user))
    states = (
        prototype_db.query(ReadingState)
        .filter(ReadingState.user_id == user.id)
        .order_by(ReadingState.last_opened_at.desc(), ReadingState.conversation_id.desc())
        .all()
    )
    memos = (
        prototype_db.query(VoiceMemo)
        .filter(VoiceMemo.user_id == user.id, VoiceMemo.status.in_(_FINISH_STATUSES))
        .order_by(VoiceMemo.recorded_at.desc(), VoiceMemo.id.desc())
        .all()
    )
    conversations = _visible(
        source, project_ids, {s.conversation_id for s in states} | {vm.conversation_id for vm in memos}
    )
    states = [s for s in states if s.conversation_id in conversations]

    # keep_reading: opened, and not reflected on yet (a failed recording doesn't count).
    reflected = {
        row.conversation_id
        for row in prototype_db.query(VoiceMemo.conversation_id).filter(
            VoiceMemo.user_id == user.id, VoiceMemo.status != "failed"
        )
    }
    reading = [s for s in states if s.conversation_id not in reflected][:KEEP_READING_LIMIT]
    counts = {}
    if reading:
        counts = {
            row.conversation_id: (row.total, int(row.starred or 0))
            for row in prototype_db.query(
                RLHighlight.conversation_id,
                func.count(RLHighlight.id).label("total"),
                func.sum(case((RLHighlight.starred.is_(True), 1), else_=0)).label("starred"),
            )
            .filter(
                RLHighlight.user_id == user.id,
                RLHighlight.conversation_id.in_([s.conversation_id for s in reading]),
            )
            .group_by(RLHighlight.conversation_id)
        }
    names = _project_names(source, {conversations[s.conversation_id].project_id for s in reading} - {None})

    def reading_item(state: ReadingState) -> dict:
        conv = conversations[state.conversation_id]
        total, starred = counts.get(conv.id, (0, 0))
        return {
            "conversation_id": conv.id,
            "title": conv.title,
            "project_id": conv.project_id,
            "project_name": names.get(conv.project_id),
            "start_time": conv.start_time,
            "duration_seconds": conv.duration_seconds,
            "first_opened_at": iso_utc(state.first_opened_at),
            "last_opened_at": iso_utc(state.last_opened_at),
            "position_seconds": state.position_seconds,
            "position_snippet_id": state.position_snippet_id,
            "highlight_count": total,
            "starred_count": starred,
        }

    # finish: reflections with insights still undecided, counted from the connections
    # themselves, so it needs neither the review draft nor a list of recent conversations.
    memo_ids = [vm.id for vm in memos if vm.conversation_id in conversations]
    by_memo: dict[int, dict[str, int]] = {}
    if memo_ids:
        rows = (
            prototype_db.query(
                ReflectionConnection.voice_memo_id, ReflectionConnection.status, func.count(ReflectionConnection.id)
            )
            .filter(ReflectionConnection.voice_memo_id.in_(memo_ids))
            .group_by(ReflectionConnection.voice_memo_id, ReflectionConnection.status)
        )
        for voice_memo_id, status, count in rows:
            by_memo.setdefault(voice_memo_id, {})[status] = count

    finish = []
    for vm in memos:
        if vm.conversation_id not in conversations:
            continue
        statuses = by_memo.get(vm.id, {})
        undecided = statuses.get("suggested", 0)
        if undecided:
            state = "undecided"
        elif vm.status == "transcribing":
            state = "transcribing"
        elif vm.status == "ready_for_review" and not statuses:
            state = "not_reviewed"
        else:
            continue
        finish.append(
            {
                "voice_memo_id": vm.id,
                "conversation_id": vm.conversation_id,
                "conversation_title": conversations[vm.conversation_id].title,
                "state": state,
                "undecided_count": undecided,
                "typed": not vm.audio_path,
                "voice_memo_status": vm.status,
                "created_at": iso_utc(vm.recorded_at),
            }
        )

    last = states[0] if states else None
    return {
        "keep_reading": [reading_item(s) for s in reading],
        "finish": finish,
        "last_conversation": {
            "conversation_id": last.conversation_id,
            "title": conversations[last.conversation_id].title,
            "last_opened_at": iso_utc(last.last_opened_at),
            "position_seconds": last.position_seconds,
            "position_snippet_id": last.position_snippet_id,
        }
        if last
        else None,
    }
