"""Merging one filed insight into another, by hand or from an accepted merge nudge.

The target gains the source's evidence, the source's status becomes "merged" (it leaves the
memo but is kept), and the merge is logged in rl_insight_merge so it can be undone.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..evidence_provenance import resolve_evidence, user_highlight_spans
from ..models_prototype import (
    ConnectionEvidence,
    InsightMerge,
    ReflectionConnection,
    ReflectionConnectionRevision,
    VoiceMemo,
)
from ..timefmt import iso_utc
from . import activity
from .code_families import FILED_STATUSES

MERGED_STATUS = "merged"


def write_connection_revision(prototype_db: Session, connection: ReflectionConnection) -> None:
    """Snapshot the insight's text and evidence before a change to either."""
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


def _refs(prototype_db: Session, merge: InsightMerge, target: ReflectionConnection) -> dict:
    vm = prototype_db.get(VoiceMemo, target.voice_memo_id)
    return {
        "connection_id": target.id,
        "source_connection_id": merge.source_connection_id,
        "insight_merge_id": merge.id,
        "research_question_id": target.research_question_id,
        "conversation_id": vm.conversation_id if vm else None,
        "voice_memo_id": target.voice_memo_id,
    }


def merge_insights(
    prototype_db: Session, *, user_id: int, source: ReflectionConnection, target: ReflectionConnection
) -> InsightMerge:
    """Folds `source` into `target` and commits. The caller has checked both belong to the
    user. Raises ValueError (the routers' 409) unless both are filed and distinct."""
    if source.id == target.id:
        raise ValueError("an insight can't be merged into itself")
    if source.status not in FILED_STATUSES:
        raise ValueError(f"the insight to merge is {source.status}, not in the memo")
    if target.status not in FILED_STATUSES:
        raise ValueError(f"the insight to merge into is {target.status}, not in the memo")

    write_connection_revision(prototype_db, target)
    # The source's evidence may sit in another conversation than the target's reflection,
    # so candidate highlights are looked up by snippet, not by conversation.
    spans = user_highlight_spans(prototype_db, user_id, {e.snippet_id for e in source.evidence})
    have = {(e.snippet_id, e.start_index, e.end_index) for e in target.evidence}
    added: list[ConnectionEvidence] = []
    for e in source.evidence:
        key = (e.snippet_id, e.start_index, e.end_index)
        if key in have:
            continue
        have.add(key)
        highlight_id, evidence_source = resolve_evidence(
            spans,
            snippet_id=e.snippet_id,
            start_index=e.start_index,
            end_index=e.end_index,
            source=e.source,
            highlight_id=e.highlight_id,
        )
        row = ConnectionEvidence(
            connection_id=target.id,
            highlight_id=highlight_id,
            snippet_id=e.snippet_id,
            start_index=e.start_index,
            end_index=e.end_index,
            source=evidence_source,
        )
        prototype_db.add(row)
        added.append(row)
    prototype_db.flush()

    merge = InsightMerge(
        user_id=user_id,
        source_connection_id=source.id,
        target_connection_id=target.id,
        source_previous_status=source.status,
        added_evidence_json=json.dumps(
            [
                {"id": r.id, "snippet_id": r.snippet_id, "start_index": r.start_index, "end_index": r.end_index}
                for r in added
            ]
        ),
        status="active",
    )
    prototype_db.add(merge)
    source.status = MERGED_STATUS
    prototype_db.flush()
    activity.log_activity(
        prototype_db, user_id, "user", activity.INSIGHTS_MERGED,
        f"You merged “{activity.excerpt(source.insight_text, 60)}” into "
        f"“{activity.excerpt(target.insight_text, 60)}”. The earlier wording of each is kept.",
        **_refs(prototype_db, merge, target),
    )
    prototype_db.commit()
    prototype_db.refresh(merge)
    return merge


def undo_insight_merge(prototype_db: Session, merge: InsightMerge) -> None:
    """Takes back exactly the evidence the merge added and returns the source to the status
    it had. Raises ValueError (409) if the merge is already undone or stale: the source is
    no longer "merged", or the target has since left the memo."""
    if merge.status != "active":
        raise ValueError(f"this merge is already {merge.status}")
    source = prototype_db.get(ReflectionConnection, merge.source_connection_id)
    target = prototype_db.get(ReflectionConnection, merge.target_connection_id)
    if source is None or target is None or source.status != MERGED_STATUS:
        raise ValueError("this merge can no longer be undone: the merged insight has changed since")
    if target.status not in FILED_STATUSES:
        raise ValueError(f"this merge can no longer be undone: the insight it went into is {target.status}")

    write_connection_revision(prototype_db, target)
    added = {(a["snippet_id"], a["start_index"], a["end_index"]) for a in json.loads(merge.added_evidence_json)}
    # A span another insight still merged into the target also cites stays, and that merge
    # takes it over, so undoing it later removes the span then.
    others = (
        prototype_db.query(InsightMerge)
        .filter(
            InsightMerge.target_connection_id == target.id,
            InsightMerge.status == "active",
            InsightMerge.id != merge.id,
        )
        .order_by(InsightMerge.id)
        .all()
    )
    for other in others:
        other_source = prototype_db.get(ReflectionConnection, other.source_connection_id)
        shared = added & {(e.snippet_id, e.start_index, e.end_index) for e in (other_source.evidence if other_source else [])}
        if shared:
            kept = json.loads(other.added_evidence_json)
            kept += [{"snippet_id": s, "start_index": a, "end_index": b} for s, a, b in sorted(shared)]
            other.added_evidence_json = json.dumps(kept)
            added -= shared
    for e in list(target.evidence):
        if (e.snippet_id, e.start_index, e.end_index) in added:
            prototype_db.delete(e)
    source.status = merge.source_previous_status
    merge.status = "undone"
    merge.undone_at = datetime.now(timezone.utc)
    activity.log_activity(
        prototype_db, merge.user_id, "user", activity.INSIGHT_MERGE_UNDONE,
        f"You undid the merge of “{activity.excerpt(source.insight_text, 60)}”.",
        **_refs(prototype_db, merge, target),
    )
    prototype_db.commit()
    prototype_db.refresh(merge)


def serialize_merge(merge: InsightMerge) -> dict:
    return {
        "id": merge.id,
        "source_connection_id": merge.source_connection_id,
        "target_connection_id": merge.target_connection_id,
        "status": merge.status,
        "added_evidence_count": len(json.loads(merge.added_evidence_json)),
        "source_previous_status": merge.source_previous_status,
        "created_at": iso_utc(merge.created_at),
        "undone_at": iso_utc(merge.undone_at),
    }


def active_merges_by_target(prototype_db: Session, target_ids: list[int]) -> dict[int, list[InsightMerge]]:
    if not target_ids:
        return {}
    out: dict[int, list[InsightMerge]] = {}
    rows = (
        prototype_db.query(InsightMerge)
        .filter(InsightMerge.target_connection_id.in_(target_ids), InsightMerge.status == "active")
        .order_by(InsightMerge.id)
    )
    for merge in rows:
        out.setdefault(merge.target_connection_id, []).append(merge)
    return out
