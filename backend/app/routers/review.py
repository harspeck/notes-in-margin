from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..evidence_provenance import assigned_highlight_ids, evidence_row, highlight_spans
from ..evidence_text import quoted_texts
from ..models_prototype import (
    ReflectionConnection,
    ReflectionConnectionRevision,
    RLHighlight,
    VoiceMemo,
)
from ..ownership import owned_voice_memo, owns_rq
from ..schemas import FileToMemoRequest
from ..services import ai_review

router = APIRouter(tags=["review"])
config = get_config()


def _evidence_spans(connections: list[ReflectionConnection], highlights: list[RLHighlight]) -> list[tuple[int, int, int]]:
    spans = [(e.snippet_id, e.start_index, e.end_index) for c in connections for e in c.evidence]
    spans += [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    return spans


def _serialize_connection(c: ReflectionConnection, text_by_span: dict[tuple[int, int, int], str]) -> dict:
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


@router.post("/voice_memos/{voice_memo_id}/review")
def run_review(
    voice_memo_id: int,
    cortico_db: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    connections = ai_review.generate_connections(
        prototype_db=prototype_db, cortico_db=cortico_db, voice_memo=vm, config=config
    )
    starred = (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == vm.user_id, RLHighlight.conversation_id == vm.conversation_id, RLHighlight.starred.is_(True))
        .all()
    )
    text_by_span = quoted_texts(cortico_db, _evidence_spans(connections, starred))
    return {
        "connections": [_serialize_connection(c, text_by_span) for c in connections],
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
        "connections": [_serialize_connection(c, text_by_span) for c in connections],
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
    deleted, so the review stays auditable."""
    vm = owned_voice_memo(prototype_db, voice_memo_id, user)
    for decision in body.decisions:
        rq_id = decision.research_question_id
        if rq_id is not None and not owns_rq(prototype_db, rq_id, user):
            raise HTTPException(400, f"research question {rq_id} not found")

    now = datetime.now(timezone.utc)
    spans = highlight_spans(prototype_db, vm.user_id, vm.conversation_id)

    for decision in body.decisions:
        if decision.connection_id is not None:
            connection = prototype_db.get(ReflectionConnection, decision.connection_id)
            if connection is None or connection.voice_memo_id != voice_memo_id:
                raise HTTPException(400, f"connection {decision.connection_id} not found on this memo")

            if decision.action == "reject":
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
            else:  # accept
                connection.status = "accepted"
        else:
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

    vm.status = "filed"
    prototype_db.commit()

    connections = (
        prototype_db.query(ReflectionConnection)
        .filter(ReflectionConnection.voice_memo_id == voice_memo_id)
        .all()
    )
    text_by_span = quoted_texts(cortico_db, _evidence_spans(connections, []))
    return {"connections": [_serialize_connection(c, text_by_span) for c in connections]}
