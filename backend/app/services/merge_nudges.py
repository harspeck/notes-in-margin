"""Merge nudges: the AI points at two research questions that look alike, or two insights
under one question that say nearly the same thing. It only suggests. Accepting runs the same
merge the user can do by hand (services/rq_merges.merge_rq, services/insight_merges.merge_insights);
dismissing means "kept separate", and a pair that was ever suggested, kept separate or merged
is never suggested again.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..config import Config, MergeNudgesConfig
from ..llm import structured_json
from ..models_prototype import (
    InsightMerge,
    MergeNudge,
    ReflectionConnection,
    ResearchQuestion,
    RQMerge,
    VoiceMemo,
)
from . import activity
from .code_families import FILED_STATUSES
from .rq_merges import active_merge_edges, rq_group_ids

QUESTIONS = "questions"
INSIGHTS = "insights"

MERGE_NUDGE_PROMPT = """You help a sensemaker keep their research questions and insights tidy. \
You are given their active research questions, each with its name, description and the insights \
they have filed under it.

Point out, sparingly:
- question_pairs: two questions that look alike, meaning they are gathering the same kind of \
passage and would read better as one question. Give a one-sentence reason grounded in the \
names, descriptions and insights you were given, and a suggested_wording for the merged question.
- insight_pairs: two insights under the SAME question that say nearly the same thing. Give a \
one-sentence reason.

Rules:
- Use only ids you were given. Never invent a question id or a connection id.
- Never pair a question with itself, an insight with itself, or two insights from different \
questions.
- Never repeat a pair listed in already_decided_question_pairs or already_decided_insight_pairs: \
the user has already merged those or chosen to keep them separate.
- Related is not the same as alike. Two questions about the same broad topic that ask different \
things stay separate. Two insights that agree but add different detail stay separate.
- Return at most max_question_pairs question pairs and max_insight_pairs insight pairs, and \
empty lists when nothing genuinely looks alike.
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question_pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_a_id": {"type": "integer"},
                    "question_b_id": {"type": "integer"},
                    "reason": {"type": "string"},
                    "suggested_wording": {"type": "string"},
                },
                "required": ["question_a_id", "question_b_id", "reason", "suggested_wording"],
                "additionalProperties": False,
            },
        },
        "insight_pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "connection_a_id": {"type": "integer"},
                    "connection_b_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["connection_a_id", "connection_b_id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["question_pairs", "insight_pairs"],
    "additionalProperties": False,
}


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def active_questions(prototype_db: Session, user_id: int) -> list[ResearchQuestion]:
    """The user's questions that are neither archived nor merged into another."""
    edges = active_merge_edges(prototype_db)
    return [
        rq
        for rq in prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user_id, ResearchQuestion.archived.is_(False))
        .order_by(ResearchQuestion.display_order, ResearchQuestion.id)
        if rq.id not in edges
    ]


def filed_insights_by_question(
    prototype_db: Session, user_id: int, questions: list[ResearchQuestion]
) -> dict[int, list[ReflectionConnection]]:
    """question id -> the user's filed insights in its merged group."""
    edges = active_merge_edges(prototype_db)
    target_of = {member: rq.id for rq in questions for member in rq_group_ids(prototype_db, rq.id, edges)}
    out: dict[int, list[ReflectionConnection]] = {rq.id: [] for rq in questions}
    if not target_of:
        return out
    rows = (
        prototype_db.query(ReflectionConnection)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(
            VoiceMemo.user_id == user_id,
            ReflectionConnection.status.in_(FILED_STATUSES),
            ReflectionConnection.research_question_id.in_(list(target_of)),
        )
        .order_by(ReflectionConnection.id)
    )
    for c in rows:
        out[target_of[c.research_question_id]].append(c)
    return out


def decided_pairs(prototype_db: Session, user_id: int) -> dict[str, set[tuple[int, int]]]:
    """Pairs that must not be suggested: any earlier nudge (pending, accepted or kept
    separate), and any pair the user merged by hand, undone or not."""
    out: dict[str, set[tuple[int, int]]] = {QUESTIONS: set(), INSIGHTS: set()}
    for n in prototype_db.query(MergeNudge).filter(MergeNudge.user_id == user_id):
        out.setdefault(n.kind, set()).add(_pair(n.item_a_id, n.item_b_id))
    for m in prototype_db.query(RQMerge).filter(RQMerge.user_id == user_id):
        out[QUESTIONS].add(_pair(m.source_rq_id, m.target_rq_id))
    for m in prototype_db.query(InsightMerge).filter(InsightMerge.user_id == user_id):
        out[INSIGHTS].add(_pair(m.source_connection_id, m.target_connection_id))
    return out


def _apply_nudges(
    prototype_db: Session,
    *,
    user_id: int,
    raw_question_pairs: list[dict],
    raw_insight_pairs: list[dict],
    valid_question_ids: set[int],
    question_of_insight: dict[int, int],
    question_weight: dict[int, int],
    insight_weight: dict[int, int],
    limits: MergeNudgesConfig,
    ai_metadata: dict,
) -> list[MergeNudge]:
    """Persists the model's pairs as suggested nudges, trusting none of its ids.

    A question pair needs two different ids from `valid_question_ids`. An insight pair needs
    two different keys of `question_of_insight` (filed insight id -> its question) under the
    same question. Pairs already decided, or repeated in this output, are dropped, and the
    per-run caps are enforced here whatever the model returned. The default target is the
    heavier side (`*_weight`: insights under a question, evidence under an insight); a tie
    goes to the one the model named first."""
    decided = decided_pairs(prototype_db, user_id)
    created: list[MergeNudge] = []

    def add(kind: str, a, b, weight: dict[int, int], *, reason, wording=None, rq_id=None) -> None:
        if not isinstance(reason, str) or not reason.strip():
            return
        pair = _pair(a, b)
        if pair in decided[kind]:
            return
        decided[kind].add(pair)
        nudge = MergeNudge(
            user_id=user_id,
            kind=kind,
            item_a_id=a,
            item_b_id=b,
            default_target_id=b if weight.get(b, 0) > weight.get(a, 0) else a,
            research_question_id=rq_id,
            reason=reason.strip(),
            suggested_wording=(wording or "").strip() or None,
            status="suggested",
            ai_metadata=json.dumps(ai_metadata),
        )
        prototype_db.add(nudge)
        created.append(nudge)

    question_count = 0
    for raw in raw_question_pairs:
        a, b = raw.get("question_a_id"), raw.get("question_b_id")
        if a == b or a not in valid_question_ids or b not in valid_question_ids:
            continue
        if question_count >= limits.max_question_pairs_per_run:
            break
        before = len(created)
        add(QUESTIONS, a, b, question_weight, reason=raw.get("reason"), wording=raw.get("suggested_wording"))
        question_count += len(created) - before

    insight_count = 0
    for raw in raw_insight_pairs:
        a, b = raw.get("connection_a_id"), raw.get("connection_b_id")
        if a == b or a not in question_of_insight or b not in question_of_insight:
            continue
        if question_of_insight[a] != question_of_insight[b]:
            continue
        if insight_count >= limits.max_insight_pairs_per_run:
            break
        before = len(created)
        add(INSIGHTS, a, b, insight_weight, reason=raw.get("reason"), rq_id=question_of_insight[a])
        insight_count += len(created) - before

    prototype_db.commit()
    return created


def generate_merge_nudges(*, prototype_db: Session, config: Config, user_id: int) -> list[MergeNudge]:
    questions = active_questions(prototype_db, user_id)
    insights = filed_insights_by_question(prototype_db, user_id, questions)
    if len(questions) < 2 and not any(len(v) >= 2 for v in insights.values()):
        return []  # nothing could be paired, so no call is spent

    decided = decided_pairs(prototype_db, user_id)
    limits = config.merge_nudges
    payload = {
        "research_questions": [
            {
                "id": rq.id,
                "name": rq.name,
                "description": rq.description,
                "insights": [{"connection_id": c.id, "insight_text": c.insight_text} for c in insights[rq.id]],
            }
            for rq in questions
        ],
        "already_decided_question_pairs": [list(p) for p in sorted(decided[QUESTIONS])],
        "already_decided_insight_pairs": [list(p) for p in sorted(decided[INSIGHTS])],
        "max_question_pairs": limits.max_question_pairs_per_run,
        "max_insight_pairs": limits.max_insight_pairs_per_run,
    }
    answer = structured_json(
        system=MERGE_NUDGE_PROMPT, user=json.dumps(payload), schema=_OUTPUT_SCHEMA, name="merge_nudges"
    )
    created = _apply_nudges(
        prototype_db,
        user_id=user_id,
        raw_question_pairs=answer.data.get("question_pairs", []),
        raw_insight_pairs=answer.data.get("insight_pairs", []),
        valid_question_ids={rq.id for rq in questions},
        question_of_insight={c.id: rq_id for rq_id, conns in insights.items() for c in conns},
        question_weight={rq_id: len(conns) for rq_id, conns in insights.items()},
        insight_weight={c.id: len(c.evidence) for conns in insights.values() for c in conns},
        limits=limits,
        ai_metadata={
            "model": answer.model,
            "reasoning_effort": answer.reasoning_effort,
            "prompt": MERGE_NUDGE_PROMPT,
        },
    )
    question_nudges = sum(1 for n in created if n.kind == QUESTIONS)
    activity.log_activity(
        prototype_db, user_id, "ai", activity.MERGE_NUDGES_SUGGESTED,
        f"The AI pointed out {activity.plural(len(created), 'pair')} that could be merged."
        if created
        else "The AI found nothing that looks alike enough to merge.",
        count=len(created), question_pair_count=question_nudges, insight_pair_count=len(created) - question_nudges,
    )
    prototype_db.commit()
    return created
