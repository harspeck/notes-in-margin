from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource, as_source
from ..db import get_prototype_db
from ..evidence_provenance import assigned_highlight_ids, evidence_row, highlight_spans
from ..evidence_text import quoted_texts
from ..models_prototype import (
    ReflectionConnection,
    ReflectionConnectionRevision,
    ResearchQuestion,
    RLHighlight,
    VoiceMemo,
)
from ..schemas import MemoEntryUpdate, MemoReorderRequest
from ..services.rq_merges import active_merge_edges, resolve_rq_id, rq_group_ids

router = APIRouter(prefix="/memo", tags=["memo"])
config = get_config()

FILED_STATUSES = ("accepted", "edited")


def filed_connections(prototype_db: Session, user_id: int) -> list[ReflectionConnection]:
    return (
        prototype_db.query(ReflectionConnection)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(ReflectionConnection.status.in_(FILED_STATUSES), VoiceMemo.user_id == user_id)
        .all()
    )


def starred_highlights(prototype_db: Session, user_id: int) -> list[RLHighlight]:
    return (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == user_id, RLHighlight.starred.is_(True))
        .all()
    )


def evidence_texts(
    cortico_db: Session, connections: list[ReflectionConnection], highlights: list[RLHighlight]
) -> dict[tuple[int, int, int], str]:
    """Quoted text for every evidence span of `connections` and every snippet span of `highlights`."""
    spans = [(e.snippet_id, e.start_index, e.end_index) for c in connections for e in c.evidence]
    spans += [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    return quoted_texts(cortico_db, spans)


def evidence_serializer(prototype_db: Session, connections: list[ReflectionConnection], text_by_span: dict):
    """Returns a function that serializes one connection's evidence list, with each row's
    highlight star and quoted text."""
    cited = [e.highlight_id for c in connections for e in c.evidence if e.highlight_id is not None]
    starred_by_highlight = (
        {h.id: h.starred for h in prototype_db.query(RLHighlight).filter(RLHighlight.id.in_(cited))}
        if cited
        else {}
    )

    def serialize(c: ReflectionConnection) -> list[dict]:
        return [
            {
                "highlight_id": e.highlight_id,
                "snippet_id": e.snippet_id,
                "start_index": e.start_index,
                "end_index": e.end_index,
                "source": e.source,
                "starred": starred_by_highlight.get(e.highlight_id, False),
                "text": text_by_span.get((e.snippet_id, e.start_index, e.end_index), ""),
            }
            for e in c.evidence
        ]

    return serialize


def starred_unassigned_groups(
    cortico_db: Session,
    connections: list[ReflectionConnection],
    highlights: list[RLHighlight],
    text_by_span: dict,
) -> list[dict]:
    """Starred highlights not cited by any of `connections` (pass every filed connection),
    grouped by conversation."""
    cited = assigned_highlight_ids(connections)
    groups: dict[int, dict] = {}
    for h in highlights:
        if h.id in cited:
            continue
        conv = as_source(cortico_db).conversations_by_ids({h.conversation_id}).get(h.conversation_id)
        group = groups.setdefault(
            h.conversation_id,
            {"conversation_id": h.conversation_id, "conversation_title": conv.title if conv else None, "highlights": []},
        )
        group["highlights"].append(
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
        )
    return list(groups.values())


@router.get("")
def get_memo(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    connections = filed_connections(prototype_db, user.id)

    voice_memo_ids = {c.voice_memo_id for c in connections}
    voice_memos = {
        vm.id: vm for vm in prototype_db.query(VoiceMemo).filter(VoiceMemo.id.in_(voice_memo_ids))
    } if voice_memo_ids else {}

    conv_ids = {vm.conversation_id for vm in voice_memos.values()}
    conversations = (
        as_source(cortico_db).conversations_by_ids(conv_ids)
        if conv_ids
        else {}
    )

    research_questions = (
        prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user.id)
        .order_by(ResearchQuestion.display_order)
        .all()
    )

    all_starred_highlights = starred_highlights(prototype_db, user.id)
    text_by_span = evidence_texts(cortico_db, connections, all_starred_highlights)
    serialize_evidence = evidence_serializer(prototype_db, connections, text_by_span)

    # A merged RQ has no section of its own; its entries read under its final target.
    edges = active_merge_edges(prototype_db)
    research_questions = [rq for rq in research_questions if rq.id not in edges]
    by_rq: dict[int | None, list[ReflectionConnection]] = {rq.id: [] for rq in research_questions}
    unmapped: list[ReflectionConnection] = []
    for c in connections:
        rq_id = (
            resolve_rq_id(prototype_db, c.research_question_id, edges)
            if c.research_question_id is not None
            else None
        )
        if rq_id is not None and rq_id in by_rq:
            by_rq[rq_id].append(c)
        else:
            unmapped.append(c)

    def entry(c: ReflectionConnection) -> dict:
        vm = voice_memos.get(c.voice_memo_id)
        conv = conversations.get(vm.conversation_id) if vm else None
        ai_metadata = json.loads(c.ai_metadata) if c.ai_metadata else None
        return {
            "connection_id": c.id,
            "insight_text": c.insight_text,
            "source": c.source,
            "edited_from_ai": c.edited_from_ai,
            "conversation_id": conv.id if conv else None,
            "conversation_title": conv.title if conv else None,
            "voice_memo_id": c.voice_memo_id,
            "voice_memo_audio_path": vm.audio_path if vm else None,
            # Where in the voice memo's own recording this insight was drawn from, if the
            # AI located it — lets the memo page link straight to that moment, not just
            # the start of the file. None for manually-added connections or older ones
            # created before this field existed.
            "reflection_timestamp": ai_metadata.get("reflection_timestamp") if ai_metadata else None,
            "filed_at": c.updated_at.isoformat(),
            "evidence": serialize_evidence(c),
        }

    def ordered(conns: list[ReflectionConnection]) -> list[ReflectionConnection]:
        return sorted(conns, key=lambda c: (c.memo_order if c.memo_order is not None else 1 << 30, c.created_at))

    sections = [
        {
            "research_question_id": rq.id,
            "name": rq.name,
            "archived": rq.archived,
            "merged_from": rq_group_ids(prototype_db, rq.id, edges)[1:],
            "entries": [entry(c) for c in ordered(by_rq[rq.id])],
        }
        for rq in research_questions
    ]

    return {
        "sections": sections,
        "unmapped": [entry(c) for c in ordered(unmapped)],
        "starred_unassigned": starred_unassigned_groups(cortico_db, connections, all_starred_highlights, text_by_span),
    }


@router.patch("/connections/{connection_id}")
def edit_memo_entry(
    connection_id: int, body: MemoEntryUpdate, prototype_db: Session = Depends(get_prototype_db)
):
    connection = prototype_db.get(ReflectionConnection, connection_id)
    if connection is None or connection.status not in FILED_STATUSES:
        raise HTTPException(404, "filed memo entry not found")

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
        )
    )

    if connection.source == "ai":
        connection.source = "user"
        connection.edited_from_ai = True
    if body.insight_text is not None:
        connection.insight_text = body.insight_text
    if body.evidence is not None:
        for e in list(connection.evidence):
            prototype_db.delete(e)
        vm = prototype_db.get(VoiceMemo, connection.voice_memo_id)
        spans = highlight_spans(prototype_db, vm.user_id, vm.conversation_id)
        for ev in body.evidence:
            prototype_db.add(evidence_row(connection.id, ev, spans, vm, prototype_db))
    connection.status = "edited"
    prototype_db.commit()
    return {"id": connection.id, "status": connection.status}


@router.post("/connections/{connection_id}/retract")
def retract_memo_entry(connection_id: int, prototype_db: Session = Depends(get_prototype_db)):
    connection = prototype_db.get(ReflectionConnection, connection_id)
    if connection is None or connection.status not in FILED_STATUSES:
        raise HTTPException(404, "filed memo entry not found")
    connection.status = "retracted"
    prototype_db.commit()
    return {"id": connection.id, "status": connection.status}


@router.put("/reorder")
def reorder_memo_entries(body: MemoReorderRequest, prototype_db: Session = Depends(get_prototype_db)):
    group = set(rq_group_ids(prototype_db, body.research_question_id))
    for order, connection_id in enumerate(body.ordered_connection_ids):
        c = prototype_db.get(ReflectionConnection, connection_id)
        if c is not None and c.research_question_id in group:
            c.memo_order = order
    prototype_db.commit()
    return {"reordered": True}
