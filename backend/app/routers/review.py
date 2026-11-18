from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..evidence_provenance import assigned_highlight_ids, check_evidence_snippets, evidence_row, highlight_spans
from ..evidence_text import evidence_conversations, quoted_texts
from ..models_prototype import (
    ReviewRun,
    ReflectionConnection,
    ReflectionConnectionRevision,
    ReviewDraft,
    RLHighlight,
    SetAside,
    VoiceMemo,
)
from ..ownership import owned_voice_memo, owns_rq
from ..schemas import FileToMemoRequest, ReviewDraftSave
from ..timefmt import iso_utc
from ..services import activity, ai_review

router = APIRouter(tags=["review"])
config = get_config()


def _evidence_spans(connections: list[ReflectionConnection], highlights: list[RLHighlight]) -> list[tuple[int, int, int]]:
    spans = [(e.snippet_id, e.start_index, e.end_index) for c in connections for e in c.evidence]
    spans += [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    return spans


def _serialize_connections(
    cortico_db, vm: VoiceMemo, connections: list[ReflectionConnection], text_by_span: dict[tuple[int, int, int], str]
) -> list[dict]:
    """Each evidence row carries its own conversation: a filed insight that another was
    merged into cites passages from other conversations than this reflection's."""
    by_snippet = evidence_conversations(cortico_db, {e.snippet_id for c in connections for e in c.evidence})
    return [_serialize_connection(c, text_by_span, by_snippet, vm.conversation_id) for c in connections]


def _serialize_connection(
    c: ReflectionConnection,
    text_by_span: dict[tuple[int, int, int], str],
    conversation_by_snippet: dict[int, tuple[int, str | None]],
    fallback_conversation_id: int,
) -> dict:
    return {
        "id": c.id,
        "research_question_id": c.research_question_id,
        "insight_text": c.insight_text,
        "source": c.source,
        "status": c.status,
        "edited_from_ai": c.edited_from_ai,
        "ai_metadata": json.loads(c.ai_metadata) if c.ai_metadata else None,
        "evidence": [
            {
                "id": e.id,
                "highlight_id": e.highlight_id,
                "snippet_id": e.snippet_id,
                "start_index": e.start_index,
                "end_index": e.end_index,
                "source": e.source,
                "text": text_by_span.get((e.snippet_id, e.start_index, e.end_index), ""),
                "conversation_id": conversation_by_snippet.get(e.snippet_id, (fallback_conversation_id, None))[0],
                "conversation_title": conversation_by_snippet.get(e.snippet_id, (None, None))[1],
            }
            for e in c.evidence
        ],
    }


def _starred_unassigned(
    prototype_db: Session, voice_memo: VoiceMemo, text_by_span: dict[tuple[int, int, int], str]
) -> list[dict]:
    starred = (
        prototype_db.query(RLHighlight)
        .filter(
            RLHighlight.user_id == voice_memo.user_id,
            RLHighlight.conversation_id == voice_memo.conversation_id,
            RLHighlight.starred.is_(True),
        )
        .all()
    )
    # Deterministic half of "starred highlights always appear among candidate evidence":
    # whatever the model did, every starred highlight is either cited by one of this
    # memo's (non-rejected) RQ connections or returned here for the user to assign.
    assigned = assigned_highlight_ids(
        prototype_db.query(ReflectionConnection)
        .filter(ReflectionConnection.voice_memo_id == voice_memo.id)
        .all()
    )
    return [
        {
            "highlight_id": h.id,
            "notes": h.notes,
            "snippets": [
                {
                    "snippet_id": s.snippet_id,
                    "start_index": s.start_index,
                    "end_index": s.end_index,
                    "text": text_by_span.get((s.snippet_id, s.start_index, s.end_index), ""),
                }
                for s in h.snippets
            ],
        }
        for h in starred
        if h.id not in assigned
    ]


REVIEW_RUN_TIMEOUT = timedelta(minutes=5)  # a run left "running" by a crash is retried after this


@router.post("/voice_memos/{voice_memo_id}/review")
def run_review(
    voice_memo_id: int,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    connections = (
        prototype_db.query(ReflectionConnection)
        .filter(ReflectionConnection.voice_memo_id == voice_memo_id)
        .order_by(ReflectionConnection.created_at)
        .all()
    )
    if not connections:
        # One review per reflection. The run is claimed before the model is called, so a
        # double click, a second tab or a retry cannot pay for and save a second set.
        run = prototype_db.get(ReviewRun, voice_memo_id)
        now = datetime.now(timezone.utc)
        if run is not None and run.status == "running":
            started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
            if now - started < REVIEW_RUN_TIMEOUT:
                raise HTTPException(409, "This reflection is already being connected to your questions. Give it a moment.")
        if run is None:
            run = ReviewRun(voice_memo_id=voice_memo_id)
            prototype_db.add(run)
        run.status = "running"
        run.started_at = now
        try:
            prototype_db.commit()
        except IntegrityError:
            prototype_db.rollback()
            raise HTTPException(409, "This reflection is already being connected to your questions. Give it a moment.")
        try:
            connections = ai_review.generate_connections(
                prototype_db=prototype_db, cortico_db=cortico_db, voice_memo=vm, config=config
            )
        except Exception:
            prototype_db.rollback()
            run = prototype_db.get(ReviewRun, voice_memo_id)
            if run is not None:
                run.status = "failed"
                prototype_db.commit()
            raise
        run.status = "done"
        prototype_db.commit()
    starred = (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == vm.user_id, RLHighlight.conversation_id == vm.conversation_id, RLHighlight.starred.is_(True))
        .all()
    )
    text_by_span = quoted_texts(cortico_db, _evidence_spans(connections, starred))
    return {
        "connections": _serialize_connections(cortico_db, vm, connections, text_by_span),
        "starred_unassigned": _starred_unassigned(prototype_db, vm, text_by_span),
    }


@router.get("/voice_memos/{voice_memo_id}/review")
def get_review(
    voice_memo_id: int,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    connections = (
        prototype_db.query(ReflectionConnection)
        .filter(ReflectionConnection.voice_memo_id == voice_memo_id)
        .order_by(ReflectionConnection.created_at)
        .all()
    )
    starred = (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == vm.user_id, RLHighlight.conversation_id == vm.conversation_id, RLHighlight.starred.is_(True))
        .all()
    )
    text_by_span = quoted_texts(cortico_db, _evidence_spans(connections, starred))
    return {
        "connections": _serialize_connections(cortico_db, vm, connections, text_by_span),
        "starred_unassigned": _starred_unassigned(prototype_db, vm, text_by_span),
    }


@router.post("/voice_memos/{voice_memo_id}/file")
def file_to_memo(
    voice_memo_id: int,
    body: FileToMemoRequest,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """The single write point for the memo. Nothing reaches status accepted/edited before
    this call; rejected proposals stay in the log with status='rejected' rather than being
    deleted, so the review stays auditable. (The one other route that sets a filed status is
    restore in routers/memo.py, which only puts back the status a retracted entry had.)"""
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    for decision in body.decisions:
        rq_id = decision.research_question_id
        if rq_id is not None and not owns_rq(prototype_db, rq_id, user):
            raise HTTPException(400, f"research question {rq_id} not found")

    check_evidence_snippets(
        prototype_db, cortico_db, vm,
        {ev.snippet_id for d in body.decisions for ev in (d.evidence or [])},
    )
    now = datetime.now(timezone.utc)
    spans = highlight_spans(prototype_db, vm.user_id, vm.conversation_id)
    refs = {"conversation_id": vm.conversation_id, "voice_memo_id": vm.id}

    for decision in body.decisions:
        if decision.connection_id is not None:
            connection = prototype_db.get(ReflectionConnection, decision.connection_id)
            if connection is None or connection.voice_memo_id != voice_memo_id:
                raise HTTPException(400, f"connection {decision.connection_id} not found on this memo")
            if connection.status in ("merged", "retracted"):
                # Retracted entries come back only through restore (routers/memo.py), which
                # closes their rl_set_aside row.
                # Folded into another insight (rl_insight_merge). A re-filed review must not
                # put it back in the memo beside its target; undoing the merge does that.
                continue

            if decision.action == "reject":
                if connection.status != "rejected":
                    prototype_db.add(
                        SetAside(
                            connection_id=connection.id, user_id=user.id, kind="rejected",
                            previous_status=connection.status, set_aside_at=now,
                        )
                    )
                    activity.log_activity(
                        prototype_db, user.id, "user", activity.INSIGHT_SET_ASIDE,
                        f"You set aside an insight: \u201c{activity.excerpt(connection.insight_text)}\u201d",
                        connection_id=connection.id, research_question_id=connection.research_question_id, **refs,
                    )
                connection.status = "rejected"
                continue

            if decision.action == "edit":
                prototype_db.add(
                    ReflectionConnectionRevision(
                        connection_id=connection.id,
                        insight_text=connection.insight_text,
                        evidence_snapshot=json.dumps(
                            [
                                {
                                    "highlight_id": e.highlight_id,
                                    "snippet_id": e.snippet_id,
                                    "start_index": e.start_index,
                                    "end_index": e.end_index,
                                    "source": e.source,
                                }
                                for e in connection.evidence
                            ]
                        ),
                        changed_at=now,
                    )
                )
                if connection.source == "ai":
                    connection.source = "user"
                    connection.edited_from_ai = True
                if decision.insight_text is not None:
                    connection.insight_text = decision.insight_text
                if decision.research_question_id is not None:
                    connection.research_question_id = decision.research_question_id
                if decision.evidence is not None:
                    for e in list(connection.evidence):
                        prototype_db.delete(e)
                    for ev in decision.evidence:
                        prototype_db.add(evidence_row(connection.id, ev, spans, vm, prototype_db))
                connection.status = "edited"
                activity.log_activity(
                    prototype_db, user.id, "user", activity.INSIGHT_EDITED,
                    f"You edited an insight and filed it: \u201c{activity.excerpt(connection.insight_text)}\u201d",
                    connection_id=connection.id, research_question_id=connection.research_question_id, **refs,
                )
            else:  # accept
                if connection.status != "accepted":
                    activity.log_activity(
                        prototype_db, user.id, "user", activity.INSIGHT_KEPT,
                        f"You kept an insight: \u201c{activity.excerpt(connection.insight_text)}\u201d",
                        connection_id=connection.id, research_question_id=connection.research_question_id, **refs,
                    )
                connection.status = "accepted"
        elif decision.action != "reject":
            # A connection the AI missed, added by the user during review.
            if not decision.insight_text:
                raise HTTPException(400, "insight_text is required for a new connection")
            connection = ReflectionConnection(
                voice_memo_id=voice_memo_id,
                research_question_id=decision.research_question_id,
                insight_text=decision.insight_text,
                source="user",
                status="accepted",
                created_at=now,
            )
            prototype_db.add(connection)
            prototype_db.flush()
            for ev in decision.evidence or []:
                prototype_db.add(evidence_row(connection.id, ev, spans, vm, prototype_db))
            activity.log_activity(
                prototype_db, user.id, "user", activity.INSIGHT_ADDED,
                f"You added an insight of your own: \u201c{activity.excerpt(connection.insight_text)}\u201d",
                connection_id=connection.id, research_question_id=connection.research_question_id, **refs,
            )

    draft = prototype_db.get(ReviewDraft, voice_memo_id)
    if draft is not None:
        prototype_db.delete(draft)
    vm.status = "filed"
    prototype_db.commit()

    connections = (
        prototype_db.query(ReflectionConnection)
        .filter(ReflectionConnection.voice_memo_id == voice_memo_id)
        .all()
    )
    text_by_span = quoted_texts(cortico_db, _evidence_spans(connections, []))
    return {"connections": _serialize_connections(cortico_db, vm, connections, text_by_span)}


# --- review draft: the client's undecided state, kept per voice memo ---------------------

REVIEW_DRAFT_MAX_BYTES = 200_000


def _serialize_draft(voice_memo_id: int, draft: ReviewDraft | None) -> dict:
    return {
        "voice_memo_id": voice_memo_id,
        "payload": json.loads(draft.payload_json) if draft else None,
        "updated_at": iso_utc(draft.updated_at) if draft else None,
    }


@router.get("/voice_memos/{voice_memo_id}/review_draft")
def get_review_draft(
    voice_memo_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    owned_voice_memo(prototype_db, voice_memo_id, user)
    return _serialize_draft(voice_memo_id, prototype_db.get(ReviewDraft, voice_memo_id))


@router.put("/voice_memos/{voice_memo_id}/review_draft")
def save_review_draft(
    voice_memo_id: int, body: ReviewDraftSave, prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    owned_voice_memo(prototype_db, voice_memo_id, user)
    payload_json = json.dumps(body.payload)
    if len(payload_json.encode()) > REVIEW_DRAFT_MAX_BYTES:
        raise HTTPException(413, f"review draft is larger than {REVIEW_DRAFT_MAX_BYTES} bytes")
    draft = prototype_db.get(ReviewDraft, voice_memo_id)
    if draft is None:
        draft = ReviewDraft(voice_memo_id=voice_memo_id, user_id=user.id)
        prototype_db.add(draft)
    draft.payload_json = payload_json
    draft.updated_at = datetime.now(timezone.utc)
    prototype_db.commit()
    return _serialize_draft(voice_memo_id, draft)


@router.delete("/voice_memos/{voice_memo_id}/review_draft")
def delete_review_draft(
    voice_memo_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    owned_voice_memo(prototype_db, voice_memo_id, user)
    draft = prototype_db.get(ReviewDraft, voice_memo_id)
    if draft is not None:
        prototype_db.delete(draft)
        prototype_db.commit()
    return {"deleted": draft is not None}
