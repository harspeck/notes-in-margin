"""Merge nudges: AI suggestions that two questions look alike or two insights repeat each
other. See services/merge_nudges.py."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, current_user, get_source
from ..config import get_config
from ..cortico.source import CorticoSource
from ..db import get_prototype_db
from ..evidence_text import evidence_conversations
from ..models_prototype import (
    HighlightQuestion,
    MergeNudge,
    ReflectionConnection,
    ResearchQuestion,
    RLHighlight,
    VoiceMemo,
)
from ..ownership import owned_connection
from ..schemas import MergeNudgeAccept
from ..services import activity
from ..services.code_families import FILED_STATUSES
from ..services.insight_merges import merge_insights, serialize_merge
from ..services.merge_nudges import (
    INSIGHTS,
    QUESTIONS,
    active_questions,
    filed_insights_by_question,
    generate_merge_nudges,
)
from ..services.rq_merges import active_merge_edges, merge_rq, resolve_rq_id, rq_group_ids
from ..timefmt import iso_utc
from .research_questions import _serialize_merge as serialize_rq_merge

router = APIRouter(prefix="/merge_nudges", tags=["merge_nudges"])
config = get_config()


def _owned_nudge(prototype_db: Session, nudge_id: int, user: CurrentUser) -> MergeNudge:
    nudge = prototype_db.get(MergeNudge, nudge_id)
    if nudge is None or nudge.user_id != user.id:
        raise HTTPException(404, "merge nudge not found")
    return nudge


def _serialize(prototype_db: Session, source, user: CurrentUser, nudges: list[MergeNudge], *, live_only: bool) -> list[dict]:
    """Each nudge with both of its items. With live_only, a nudge whose question was since
    archived or merged, or whose insight has left the memo, is left out (the row is kept)."""
    questions = {rq.id: rq for rq in active_questions(prototype_db, user.id)}
    insights_by_question = filed_insights_by_question(prototype_db, user.id, list(questions.values()))
    insights = {c.id: c for conns in insights_by_question.values() for c in conns}
    rq_names = {
        rq.id: rq.name for rq in prototype_db.query(ResearchQuestion).filter(ResearchQuestion.user_id == user.id)
    }

    edges = active_merge_edges(prototype_db)
    highlight_counts: dict[int, int] = {}
    if any(n.kind == QUESTIONS for n in nudges):
        target_of = {m: rq_id for rq_id in questions for m in rq_group_ids(prototype_db, rq_id, edges)}
        seen: set[tuple[int, int]] = set()
        links = (
            prototype_db.query(HighlightQuestion.research_question_id, HighlightQuestion.highlight_id)
            .join(RLHighlight, RLHighlight.id == HighlightQuestion.highlight_id)
            .filter(RLHighlight.user_id == user.id)
        )
        for rq_id, highlight_id in links:
            key = (target_of.get(rq_id), highlight_id)
            if key[0] is not None and key not in seen:
                seen.add(key)
                highlight_counts[key[0]] = highlight_counts.get(key[0], 0) + 1

    insight_ids = {i for n in nudges if n.kind == INSIGHTS for i in (n.item_a_id, n.item_b_id) if i in insights}
    vm_conversation = (
        {
            vm.id: vm.conversation_id
            for vm in prototype_db.query(VoiceMemo).filter(
                VoiceMemo.id.in_({insights[i].voice_memo_id for i in insight_ids})
            )
        }
        if insight_ids
        else {}
    )
    by_snippet = evidence_conversations(source, {e.snippet_id for i in insight_ids for e in insights[i].evidence})
    titles = {
        cid: c.title for cid, c in source.conversations_by_ids(set(vm_conversation.values())).items()
    } if vm_conversation else {}

    def question_item(rq_id: int) -> dict | None:
        rq = questions.get(rq_id)
        if rq is None:
            return None
        return {
            "id": rq.id,
            "name": rq.name,
            "description": rq.description,
            "insight_count": len(insights_by_question.get(rq.id, [])),
            "highlight_count": highlight_counts.get(rq.id, 0),
        }

    def insight_conversations(c: ReflectionConnection) -> set[int]:
        own = vm_conversation.get(c.voice_memo_id)
        ids = {by_snippet[e.snippet_id][0] for e in c.evidence if e.snippet_id in by_snippet}
        return ids | ({own} if own is not None else set())

    def insight_item(connection_id: int) -> dict | None:
        c = insights.get(connection_id)
        if c is None:
            return None
        conversation_id = vm_conversation.get(c.voice_memo_id)
        return {
            "id": c.id,
            "insight_text": c.insight_text,
            "conversation_id": conversation_id,
            "conversation_title": titles.get(conversation_id),
            "evidence_count": len(c.evidence),
            "conversation_count": len(insight_conversations(c)),
        }

    out = []
    for n in nudges:
        item = question_item if n.kind == QUESTIONS else insight_item
        items = [item(n.item_a_id), item(n.item_b_id)]
        if any(i is None for i in items):
            if live_only:
                continue
            items = [i for i in items if i is not None]
        rq_id = resolve_rq_id(prototype_db, n.research_question_id, edges) if n.research_question_id else None
        preview = None
        if n.kind == INSIGHTS and len(items) == 2:
            a, b = insights[n.item_a_id], insights[n.item_b_id]
            spans = {(e.snippet_id, e.start_index, e.end_index) for c in (a, b) for e in c.evidence}
            preview = {
                "evidence_count": len(spans),
                "conversation_count": len(insight_conversations(a) | insight_conversations(b)),
            }
        out.append(
            {
                "id": n.id,
                "kind": n.kind,
                "status": n.status,
                "reason": n.reason,
                "suggested_wording": n.suggested_wording,
                "default_target_id": n.default_target_id,
                "research_question_id": rq_id,
                "research_question_name": rq_names.get(rq_id),
                "items": items,
                # Insights only: what the merged insight would hold.
                "merged_preview": preview,
                "result_merge_id": n.result_merge_id,
                "ai_metadata": json.loads(n.ai_metadata) if n.ai_metadata else None,
                "created_at": iso_utc(n.created_at),
            }
        )
    return out


@router.post("/generate")
def generate(
    source: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Paid. Returns the nudges this run created (possibly none)."""
    created = generate_merge_nudges(prototype_db=prototype_db, config=config, user_id=user.id)
    return _serialize(prototype_db, source, user, created, live_only=False)


@router.get("")
def list_nudges(
    source: CorticoSource = Depends(get_source), prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    nudges = (
        prototype_db.query(MergeNudge)
        .filter(MergeNudge.user_id == user.id, MergeNudge.status == "suggested")
        .order_by(MergeNudge.id)
        .all()
    )
    return _serialize(prototype_db, source, user, nudges, live_only=True)


@router.post("/{nudge_id}/accept")
def accept(
    nudge_id: int,
    body: MergeNudgeAccept | None = None,
    source: CorticoSource = Depends(get_source),
    prototype_db: Session = Depends(get_prototype_db),
    user: CurrentUser = Depends(current_user),
):
    """Performs the merge through the same functions as merging by hand. 409 if the nudge
    was already decided or either side is no longer live."""
    nudge = _owned_nudge(prototype_db, nudge_id, user)
    if nudge.status != "suggested":
        raise HTTPException(409, f"this suggestion is already {nudge.status}")
    body = body or MergeNudgeAccept()
    target_id = body.target_id if body.target_id is not None else nudge.default_target_id
    if target_id not in (nudge.item_a_id, nudge.item_b_id):
        raise HTTPException(422, "target_id must be one of the two items in this suggestion")
    source_id = nudge.item_b_id if target_id == nudge.item_a_id else nudge.item_a_id

    try:
        if nudge.kind == QUESTIONS:
            source_rq = prototype_db.get(ResearchQuestion, source_id)
            target_rq = prototype_db.get(ResearchQuestion, target_id)
            if any(rq is None or rq.user_id != user.id for rq in (source_rq, target_rq)):
                raise ValueError("one of these research questions no longer exists")
            merge = merge_rq(prototype_db, user_id=user.id, source=source_rq, target=target_rq)
            if body.apply_suggested_wording and nudge.suggested_wording and nudge.suggested_wording != target_rq.name:
                previous = target_rq.name
                target_rq.name = nudge.suggested_wording
                activity.log_activity(
                    prototype_db, user.id, "user", activity.RQ_REWORDED,
                    f"You reworded a research question: “{activity.excerpt(target_rq.name)}”",
                    research_question_id=target_rq.id, previous_name=previous,
                )
            merged = {"kind": QUESTIONS, **serialize_rq_merge(merge)}
        else:
            source_c = owned_connection(prototype_db, source_id, user)
            target_c = owned_connection(prototype_db, target_id, user)
            if source_c is None or target_c is None:
                raise ValueError("one of these insights no longer exists")
            if any(c.status not in FILED_STATUSES for c in (source_c, target_c)):
                raise ValueError("one of these insights is no longer in the memo")
            merge = merge_insights(prototype_db, user_id=user.id, source=source_c, target=target_c)
            merged = {"kind": INSIGHTS, **serialize_merge(merge)}
    except ValueError as e:
        raise HTTPException(409, str(e))

    nudge.status = "accepted"
    nudge.result_merge_id = merge.id
    prototype_db.commit()
    return {
        "id": nudge.id,
        "status": nudge.status,
        "kind": nudge.kind,
        "source_id": source_id,
        "target_id": target_id,
        # Undo with POST /rq_merges/{id}/undo (questions) or POST /insight_merges/{id}/undo.
        "merge": merged,
    }


@router.post("/{nudge_id}/dismiss")
def dismiss(
    nudge_id: int, prototype_db: Session = Depends(get_prototype_db), user: CurrentUser = Depends(current_user)
):
    """Keeps the two separate. The pair is never suggested again."""
    nudge = _owned_nudge(prototype_db, nudge_id, user)
    if nudge.status == "accepted":
        raise HTTPException(409, "this suggestion was already accepted; undo the merge instead")
    if nudge.status != "dismissed":
        if nudge.kind == QUESTIONS:
            names = [
                rq.name
                for rq in (prototype_db.get(ResearchQuestion, i) for i in (nudge.item_a_id, nudge.item_b_id))
                if rq is not None
            ]
            summary = "You kept " + " and ".join(f"“{activity.excerpt(n, 60)}”" for n in names) + " as separate questions."
        else:
            summary = "You kept 2 similar insights separate."
        activity.log_activity(
            prototype_db, user.id, "user", activity.MERGE_NUDGE_DISMISSED, summary,
            merge_nudge_id=nudge.id, nudge_kind=nudge.kind, item_ids=[nudge.item_a_id, nudge.item_b_id],
            research_question_id=nudge.research_question_id,
        )
    nudge.status = "dismissed"
    prototype_db.commit()
    return {"id": nudge.id, "status": nudge.status}
