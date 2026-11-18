"""What am I missing: the AI looks at what the user has gathered under one research question,
and at the conversations they have not opened, and says what is not covered yet. Each
observation may name unopened conversations, which the user can add to Listen next.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import as_source
from ..evidence_text import quoted_texts
from ..llm import structured_json
from ..models_prototype import (
    HighlightQuestion,
    NextStepSuggestion,
    QuestionGap,
    ReflectionConnection,
    ResearchQuestion,
    RLHighlight,
    VoiceMemo,
)
from . import activity
from .code_families import FILED_STATUSES, group_for
from .next_steps import gist_dict, gists_by_conversation
from .questions import opened_conversation_ids, scope_conversations, visible_conversations
from .rq_merges import resolve_rq_id

GAP_KINDS = ("perspective", "counter_view", "thin_spot")

WHATS_MISSING_PROMPT = """You help a sensemaker see what one of their research questions does \
not cover yet.

You are given the research question, the insights they have filed under it, quotes from the \
highlights they filed under it, the conversations they have already heard, and the \
conversations in their projects they have NOT opened (a title, and a short summary and \
keywords when one exists).

Return short observations, each one of:
- "perspective": a perspective or group of people not heard from yet on this question.
- "counter_view": a view that runs against what the insights say so far.
- "thin_spot": a part of the question that rests on very little.

Rules:
- Ground every observation in the inputs: what the insights and quotes say, which \
conversations were heard, and what the unopened conversations' titles and summaries say. Never \
invent a topic, a group or a claim that the inputs do not reflect.
- conversation_ids may name unopened conversations worth listening to for that observation. \
Use only ids from unopened_conversations, and only when its title or summary supports it. \
Leave the list empty otherwise.
- An observation is one or two plain sentences addressed to the user.
- Do not repeat an entry of earlier_observations.
- Return at most max_observations observations, and an empty list rather than a weak one.
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(GAP_KINDS)},
                    "observation": {"type": "string"},
                    "conversation_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["kind", "observation", "conversation_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["observations"],
    "additionalProperties": False,
}


def _apply_gaps(
    prototype_db: Session,
    *,
    user_id: int,
    research_question_id: int,
    raw_observations: list[dict],
    valid_conversation_ids: set[int],
    max_observations: int,
    ai_metadata: dict,
) -> list[QuestionGap]:
    """Persists the model's observations as suggested gaps. An observation with no text is
    dropped, an unknown kind becomes "thin_spot", conversation ids outside
    `valid_conversation_ids` (the unopened conversations the model was shown) are removed,
    and the cap is enforced here."""
    created: list[QuestionGap] = []
    for raw in raw_observations:
        if len(created) >= max_observations:
            break
        text = raw.get("observation")
        if not isinstance(text, str) or not text.strip():
            continue
        conversation_ids = list(
            dict.fromkeys(cid for cid in raw.get("conversation_ids") or [] if cid in valid_conversation_ids)
        )
        gap = QuestionGap(
            user_id=user_id,
            research_question_id=research_question_id,
            gap_kind=raw.get("kind") if raw.get("kind") in GAP_KINDS else "thin_spot",
            observation=text.strip(),
            conversation_ids_json=json.dumps(conversation_ids),
            status="suggested",
            ai_metadata=json.dumps(ai_metadata),
        )
        prototype_db.add(gap)
        created.append(gap)
    prototype_db.commit()
    return created


def _filed_highlight_quotes(prototype_db: Session, source, user_id: int, group: list[int]) -> list[dict]:
    highlights = (
        prototype_db.query(RLHighlight)
        .join(HighlightQuestion, HighlightQuestion.highlight_id == RLHighlight.id)
        .filter(RLHighlight.user_id == user_id, HighlightQuestion.research_question_id.in_(group))
        .all()
    )
    highlights = list({h.id: h for h in highlights}.values())
    text_by_span = quoted_texts(
        source, [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    )
    quotes = []
    for h in highlights:
        text = " ".join(
            t for t in (text_by_span.get((s.snippet_id, s.start_index, s.end_index), "") for s in h.snippets) if t
        )
        if text:
            quotes.append({"conversation_id": h.conversation_id, "text": text, "note": h.notes})
    return quotes


def generate_gaps(
    *,
    prototype_db: Session,
    cortico_db,
    config: Config,
    user_id: int,
    research_question_id: int,
    project_ids: list[int],
) -> list[QuestionGap]:
    source = as_source(cortico_db)
    limits = config.whats_missing
    target_id = resolve_rq_id(prototype_db, research_question_id)  # new rows go to the target
    rq = prototype_db.get(ResearchQuestion, target_id)
    group = group_for(prototype_db, target_id)

    insights = (
        prototype_db.query(ReflectionConnection.insight_text)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(
            VoiceMemo.user_id == user_id,
            ReflectionConnection.status.in_(FILED_STATUSES),
            ReflectionConnection.research_question_id.in_(group),
        )
        .order_by(ReflectionConnection.id)
    )
    opened = opened_conversation_ids(prototype_db, user_id)
    heard = visible_conversations(source, project_ids, opened)
    unopened = [c for c in scope_conversations(source, project_ids) if c.id not in opened]
    gists = gists_by_conversation(prototype_db, {c.id for c in unopened})
    # Conversations with a summary tell the model more, so they are kept when the list is cut.
    unopened.sort(key=lambda c: c.id not in gists)
    unopened = unopened[: limits.max_unopened_conversations]
    earlier = (
        prototype_db.query(QuestionGap.observation)
        .filter(QuestionGap.user_id == user_id, QuestionGap.research_question_id.in_(group))
        .order_by(QuestionGap.id)
    )

    payload = {
        "research_question": {"name": rq.name, "description": rq.description},
        "insights": [row.insight_text for row in insights],
        "highlight_quotes": _filed_highlight_quotes(prototype_db, source, user_id, group),
        "conversations_heard": [
            {"conversation_id": c.id, "title": c.title, "project_id": c.project_id} for c in heard.values()
        ],
        "unopened_conversations": [
            {**gist_dict(gists[c.id], c.title), "project_id": c.project_id}
            if c.id in gists
            else {"conversation_id": c.id, "title": c.title, "project_id": c.project_id}
            for c in unopened
        ],
        "earlier_observations": [row.observation for row in earlier],
        "max_observations": limits.max_observations,
    }
    answer = structured_json(
        system=WHATS_MISSING_PROMPT, user=json.dumps(payload), schema=_OUTPUT_SCHEMA, name="question_gaps"
    )
    created = _apply_gaps(
        prototype_db,
        user_id=user_id,
        research_question_id=target_id,
        raw_observations=answer.data.get("observations", []),
        valid_conversation_ids={c.id for c in unopened},
        max_observations=limits.max_observations,
        ai_metadata={
            "model": answer.model,
            "reasoning_effort": answer.reasoning_effort,
            "prompt": WHATS_MISSING_PROMPT,
        },
    )
    activity.log_activity(
        prototype_db, user_id, "ai", activity.QUESTION_GAPS_SUGGESTED,
        f"The AI pointed out {activity.plural(len(created), 'thing')} “{activity.excerpt(rq.name, 60)}” "
        "doesn't cover yet."
        if created
        else f"The AI found nothing missing from “{activity.excerpt(rq.name, 60)}”.",
        research_question_id=target_id, count=len(created),
    )
    prototype_db.commit()
    return created


def add_to_listen_next(
    prototype_db: Session, *, user_id: int, gap: QuestionGap, conversation_ids: list[int], question_name: str | None
) -> list[tuple[NextStepSuggestion, bool]]:
    """One next-step row per conversation, as (row, created). The rows are `suggested`, the
    status the Listen next list shows, and carry the gap's ai_metadata: they are still the
    AI's suggestion, which the user accepts or dismisses there like any other. A conversation
    that already has a next step the user has not dismissed gets no second row. The caller
    commits."""
    existing: dict[int, NextStepSuggestion] = {}
    if conversation_ids:
        rows = (
            prototype_db.query(NextStepSuggestion)
            .filter(
                NextStepSuggestion.user_id == user_id,
                NextStepSuggestion.status != "dismissed",
                NextStepSuggestion.recommended_conversation_id.in_(conversation_ids),
            )
            .order_by(NextStepSuggestion.id)
        )
        for row in rows:
            existing.setdefault(row.recommended_conversation_id, row)

    metadata = json.loads(gap.ai_metadata) if gap.ai_metadata else {}
    metadata.update({"origin": "question_gap", "question_gap_id": gap.id})
    rq = prototype_db.get(ResearchQuestion, resolve_rq_id(prototype_db, gap.research_question_id))
    out: list[tuple[NextStepSuggestion, bool]] = []
    for conversation_id in dict.fromkeys(conversation_ids):
        if conversation_id in existing:
            out.append((existing[conversation_id], False))
            continue
        row = NextStepSuggestion(
            user_id=user_id,
            research_question_id=rq.id if rq is not None and not rq.archived else None,
            suggested_direction=gap.observation,
            recommended_conversation_id=conversation_id,
            rationale=f"From “What am I missing?” on “{question_name}”."
            if question_name
            else "From “What am I missing?”.",
            status="suggested",
            ai_metadata=json.dumps(metadata),
        )
        prototype_db.add(row)
        existing[conversation_id] = row
        out.append((row, True))
    prototype_db.flush()
    return out
