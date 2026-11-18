"""The activity log (`rl_activity`): what the AI suggested and what the user decided.

Every row is written by `log_activity`, and every kind string is listed in `KINDS` below.

To add a kind:
1. Add a constant and its `KINDS` entry (the value is the `GET /activity` filter bucket it
   belongs to: `CHANGED`, `SET_ASIDE` or `OTHER`). Rows whose actor is "ai" always match
   the `ai` filter, whatever the bucket.
2. Call `log_activity(db, user_id, actor, KIND, "One plain sentence.", **ids)` next to the
   write it describes, before that write's `commit()`.
3. Add the kind to the table in `docs/architecture/routers.md`.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from ..models_prototype import Activity

logger = logging.getLogger(__name__)

ACTORS = ("ai", "user")

# Filter buckets for GET /activity.
CHANGED = "changed"  # the user edited, reworded or merged something
SET_ASIDE = "set_aside"  # a rejection, retraction or dismissal
OTHER = "other"  # shown under "all" (and under "ai" when the actor is the AI)

INSIGHTS_SUGGESTED = "insights_suggested"
NEXT_STEPS_SUGGESTED = "next_steps_suggested"
CODE_FAMILIES_SUGGESTED = "code_families_suggested"
REFLECTION_TYPED = "reflection_typed"
REFLECTION_RECORDED = "reflection_recorded"
INSIGHT_KEPT = "insight_kept"
INSIGHT_ADDED = "insight_added"
INSIGHT_EDITED = "insight_edited"
INSIGHT_SET_ASIDE = "insight_set_aside"
INSIGHT_RETURNED_TO_REVIEW = "insight_returned_to_review"
MEMO_ENTRY_EDITED = "memo_entry_edited"
MEMO_ENTRY_RETRACTED = "memo_entry_retracted"
MEMO_ENTRY_RESTORED = "memo_entry_restored"
RQ_CREATED = "rq_created"
RQ_REWORDED = "rq_reworded"
RQ_ARCHIVED = "rq_archived"
RQ_RESTORED = "rq_restored"
RQ_MERGED = "rq_merged"
RQ_MERGE_UNDONE = "rq_merge_undone"
NEXT_STEP_ACCEPTED = "next_step_accepted"
NEXT_STEP_DISMISSED = "next_step_dismissed"
CODE_FAMILY_ACCEPTED = "code_family_accepted"
CODE_FAMILY_DISMISSED = "code_family_dismissed"
CODE_FAMILY_STARRED = "code_family_starred"
MERGE_NUDGES_SUGGESTED = "merge_nudges_suggested"
PASSAGES_SUGGESTED = "passages_suggested"
QUESTION_GAPS_SUGGESTED = "question_gaps_suggested"
HIGHLIGHT_FILED = "highlight_filed"
MEMO_ENTRY_MOVED = "memo_entry_moved"
INSIGHTS_MERGED = "insights_merged"
INSIGHT_MERGE_UNDONE = "insight_merge_undone"
MERGE_NUDGE_DISMISSED = "merge_nudge_dismissed"
PASSAGE_ACCEPTED = "passage_accepted"
PASSAGE_DISMISSED = "passage_dismissed"
QUESTION_GAP_DISMISSED = "question_gap_dismissed"
QUESTION_GAP_LISTEN_NEXT = "question_gap_listen_next"
LISTENING_LINES_SUGGESTED = "listening_lines_suggested"
LISTENING_LINE_ACCEPTED = "listening_line_accepted"
LISTENING_LINE_DISMISSED = "listening_line_dismissed"
GISTS_GENERATED = "gists_generated"

KINDS: dict[str, str] = {
    # actor "ai"
    INSIGHTS_SUGGESTED: OTHER,
    NEXT_STEPS_SUGGESTED: OTHER,
    CODE_FAMILIES_SUGGESTED: OTHER,
    MERGE_NUDGES_SUGGESTED: OTHER,
    PASSAGES_SUGGESTED: OTHER,
    QUESTION_GAPS_SUGGESTED: OTHER,
    LISTENING_LINES_SUGGESTED: OTHER,
    GISTS_GENERATED: OTHER,
    # actor "user"
    REFLECTION_TYPED: OTHER,
    REFLECTION_RECORDED: OTHER,
    INSIGHT_KEPT: OTHER,
    INSIGHT_ADDED: OTHER,
    INSIGHT_EDITED: CHANGED,
    INSIGHT_SET_ASIDE: SET_ASIDE,
    INSIGHT_RETURNED_TO_REVIEW: OTHER,
    MEMO_ENTRY_EDITED: CHANGED,
    MEMO_ENTRY_RETRACTED: SET_ASIDE,
    MEMO_ENTRY_RESTORED: OTHER,
    RQ_CREATED: OTHER,
    RQ_REWORDED: CHANGED,
    RQ_ARCHIVED: CHANGED,
    RQ_RESTORED: CHANGED,
    RQ_MERGED: CHANGED,
    RQ_MERGE_UNDONE: CHANGED,
    NEXT_STEP_ACCEPTED: OTHER,
    NEXT_STEP_DISMISSED: SET_ASIDE,
    LISTENING_LINE_ACCEPTED: OTHER,
    LISTENING_LINE_DISMISSED: SET_ASIDE,
    CODE_FAMILY_ACCEPTED: OTHER,
    CODE_FAMILY_DISMISSED: SET_ASIDE,
    CODE_FAMILY_STARRED: OTHER,
    HIGHLIGHT_FILED: OTHER,
    MEMO_ENTRY_MOVED: CHANGED,
    INSIGHTS_MERGED: CHANGED,
    INSIGHT_MERGE_UNDONE: CHANGED,
    MERGE_NUDGE_DISMISSED: SET_ASIDE,
    PASSAGE_ACCEPTED: OTHER,
    PASSAGE_DISMISSED: SET_ASIDE,
    QUESTION_GAP_DISMISSED: SET_ASIDE,
    QUESTION_GAP_LISTEN_NEXT: OTHER,
}


def kinds_in(bucket: str) -> list[str]:
    return [kind for kind, b in KINDS.items() if b == bucket]


def excerpt(text: str | None, limit: int = 90) -> str:
    """A one-line quote of `text` for a summary sentence."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def plural(count: int, noun: str, plural_noun: str | None = None) -> str:
    return f"{count} {noun if count == 1 else (plural_noun or noun + 's')}"


def log_activity(db: Session, user_id: int, actor: str, kind: str, summary: str, **refs) -> None:
    """Adds one activity row to the caller's session, so it is saved by the caller's own
    commit and rolled back with it. Never raises: a log line must not fail the write it
    describes. Refs whose value is None are dropped."""
    try:
        if actor not in ACTORS:
            raise ValueError(f"unknown actor {actor!r}")
        if kind not in KINDS:
            # Still logged, so a missing registration costs a filter bucket, not the row.
            logger.warning("activity kind %r is not listed in KINDS", kind)
        db.add(
            Activity(
                user_id=user_id,
                actor=actor,
                kind=kind,
                summary=summary,
                refs_json=json.dumps({k: v for k, v in refs.items() if v is not None}, default=str),
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not log activity %r", kind)
