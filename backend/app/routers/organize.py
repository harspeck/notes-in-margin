from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource, as_source
from ..db import get_prototype_db
from ..evidence_text import quoted_texts
from ..models_prototype import CodeFamily, ReflectionConnection, ResearchQuestion, VoiceMemo
from ..services.code_families import FILED_STATUSES, starred_family_ids
from ..services.rq_merges import active_merge_edges, rq_group_ids
from .code_families import cap_dict, live_families_for_group, serialize_families
from .memo import evidence_serializer, evidence_texts, filed_connections, starred_highlights, starred_unassigned_groups
from .research_questions import serialize_rqs

router = APIRouter(tags=["organize"])
config = get_config()

FOCUS_RANGE = (5, 7)


def _target_rqs(prototype_db: Session, edges: dict[int, int], user_id: int) -> list[ResearchQuestion]:
    """The user's unarchived RQs that aren't merged into another; merged RQs are archived
    and fold into their target's group."""
    rqs = (
        prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user_id, ResearchQuestion.archived.is_(False))
        .order_by(ResearchQuestion.display_order, ResearchQuestion.id)
        .all()
    )
    return [rq for rq in rqs if rq.id not in edges]


def _filed_connections(prototype_db: Session, group: list[int]) -> list[ReflectionConnection]:
    connections = (
        prototype_db.query(ReflectionConnection)
        .filter(
            ReflectionConnection.research_question_id.in_(group),
            ReflectionConnection.status.in_(FILED_STATUSES),
        )
        .all()
    )
    return sorted(connections, key=lambda c: (c.memo_order if c.memo_order is not None else 1 << 30, c.created_at))


def _conversation_ids(prototype_db: Session, connections: list[ReflectionConnection]) -> dict[int, int]:
    """connection id -> Cortico conversation id, via the voice memo it was filed from."""
    vm_ids = {c.voice_memo_id for c in connections}
    if not vm_ids:
        return {}
    conv_by_vm = {
        vm.id: vm.conversation_id for vm in prototype_db.query(VoiceMemo).filter(VoiceMemo.id.in_(vm_ids))
    }
    return {c.id: conv_by_vm.get(c.voice_memo_id) for c in connections}


def _focus_warning(count: int) -> str | None:
    low, high = FOCUS_RANGE
    if low <= count <= high:
        return None
    return f"{count} research questions are focused; aim for {low}-{high}."


@router.get("/organize")
def get_organize(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    edges = active_merge_edges(prototype_db)
    rqs = _target_rqs(prototype_db, edges, user.id)
    serialized = serialize_rqs(prototype_db, rqs)

    loaded = []
    for rq in rqs:
        group = rq_group_ids(prototype_db, rq.id, edges)
        loaded.append((_filed_connections(prototype_db, group), live_families_for_group(prototype_db, group)))

    group_connections = [c for conns, _ in loaded for c in conns]
    conv_by_connection = _conversation_ids(prototype_db, group_connections)
    conv_ids = {cid for cid in conv_by_connection.values() if cid is not None}
    titles = (
        {cid: c.title for cid, c in as_source(cortico_db).conversations_by_ids(conv_ids).items()}
        if conv_ids
        else {}
    )

    highlights = starred_highlights(prototype_db, user.id)
    text_by_span = evidence_texts(cortico_db, group_connections, highlights)
    serialize_evidence = evidence_serializer(prototype_db, group_connections, text_by_span)
    # Computed against every filed connection, like GET /memo, then narrowed per group.
    unassigned = starred_unassigned_groups(cortico_db, filed_connections(prototype_db, user.id), highlights, text_by_span)

    groups = []
    for rq_dict, (connections, families) in zip(serialized, loaded):
        group_conv_ids = {conv_by_connection.get(c.id) for c in connections}
        family_ids_by_connection: dict[int, list[int]] = {}
        for f in families:
            for m in f.members:
                family_ids_by_connection.setdefault(m.connection_id, []).append(f.id)
        groups.append(
            {
                "rq": rq_dict,
                "merged_sources": rq_dict["merged_sources"],
                "entry_count": len(connections),
                "entries": [
                    {
                        "connection_id": c.id,
                        "insight_text": c.insight_text,
                        "research_question_id": c.research_question_id,
                        "conversation_id": conv_by_connection.get(c.id),
                        "conversation_title": titles.get(conv_by_connection.get(c.id)),
                        "voice_memo_id": c.voice_memo_id,
                        "family_ids": family_ids_by_connection.get(c.id, []),
                        "evidence": serialize_evidence(c),
                    }
                    for c in connections
                ],
                "families": serialize_families(prototype_db, families),
                "cap": cap_dict(len(families)),
                # Highlights carry no RQ, so a group shows those from conversations its entries came from.
                "starred_unassigned": [u for u in unassigned if u["conversation_id"] in group_conv_ids],
            }
        )

    focused = [g for g in groups if g["rq"]["focused"]]
    return {
        "focused": focused,
        "other": [g for g in groups if not g["rq"]["focused"]],
        "focus_count": len(focused),
        "focus_range": list(FOCUS_RANGE),
    }


@router.get("/codebook_prep")
def get_codebook_prep(
    cortico_db: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """A draft codebook for the focused RQs: their starred, live code families with each
    member entry and its quoted evidence. Read-only; the browser saves the file."""
    edges = active_merge_edges(prototype_db)
    focused = [r for r in serialize_rqs(prototype_db, _target_rqs(prototype_db, edges, user.id)) if r["focused"]]

    warnings: list[str] = []
    focus_warning = _focus_warning(len(focused))
    if focus_warning:
        warnings.append(focus_warning)

    plan = []
    unreviewed = 0
    for rq in focused:
        group = rq_group_ids(prototype_db, rq["id"], edges)
        families = live_families_for_group(prototype_db, group)
        unreviewed += sum(1 for f in families if f.status == "suggested")
        starred = starred_family_ids(prototype_db, [f.id for f in families])
        codes = [f for f in families if f.id in starred]
        if not codes:
            warnings.append(f'"{rq["name"]}" has no starred code families.')
        plan.append((rq, group, codes))
    if unreviewed:
        warnings.append(f"{unreviewed} suggested code families are still unreviewed in focused research questions.")

    member_ids = {m.connection_id for _, _, codes in plan for f in codes for m in f.members}
    connections = (
        {
            c.id: c
            for c in prototype_db.query(ReflectionConnection).filter(
                ReflectionConnection.id.in_(member_ids), ReflectionConnection.status.in_(FILED_STATUSES)
            )
        }
        if member_ids
        else {}
    )
    conv_by_connection = _conversation_ids(prototype_db, list(connections.values()))
    text_by_span = quoted_texts(
        cortico_db, [(e.snippet_id, e.start_index, e.end_index) for c in connections.values() for e in c.evidence]
    )

    def code(f: CodeFamily) -> dict:
        return {
            "family_id": f.id,
            "name": f.name,
            "definition": f.definition,
            "status": f.status,
            "entries": [
                {
                    "connection_id": c.id,
                    "insight_text": c.insight_text,
                    "conversation_id": conv_by_connection.get(c.id),
                    "evidence": [
                        {
                            "snippet_id": e.snippet_id,
                            "start_index": e.start_index,
                            "end_index": e.end_index,
                            "text": text_by_span.get((e.snippet_id, e.start_index, e.end_index), ""),
                            "source": e.source,
                        }
                        for e in c.evidence
                    ],
                }
                for c in (connections.get(m.connection_id) for m in f.members)
                if c is not None  # a retracted entry is left out
            ],
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user.id,
        "research_questions": [
            {
                "id": rq["id"],
                "name": rq["name"],
                "description": rq["description"],
                "merged_from": group[1:],
                "codes": [code(f) for f in codes],
            }
            for rq, group, codes in plan
        ],
        "warnings": warnings,
    }
