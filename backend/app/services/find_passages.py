"""Find more passages: the AI reads one conversation's transcript for passages that speak to
a research question and suggests them. Nothing becomes a highlight until the user accepts a
suggestion, which then goes through the same code as a highlight made in the reader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import ConversationRef, SnippetRef, as_source
from ..evidence_provenance import HighlightSpan, highlight_spans, match_highlight
from ..llm import AIProviderError, structured_json
from ..models_prototype import PassageSuggestion, ReadingState, ReflectionConnection, ResearchQuestion, VoiceMemo
from . import activity
from .code_families import FILED_STATUSES, group_for
from .questions import visible_conversations
from .rq_merges import resolve_rq_id

FIND_PASSAGES_PROMPT = """You help a sensemaker find passages in one conversation transcript \
that speak to one of their research questions.

You are given the research question (its name, description and a few insights the user has \
already filed under it), the transcript as turns with snippet ids and word counts, and the \
passages the user has already highlighted in this conversation.

Rules:
- Return only passages that are actually in the transcript you were given. Point to each one \
as a snippet_id plus a word range inside that snippet: start_index is the first word (counting \
from 0) and end_index is one past the last word, so the range is [start_index, end_index) and \
end_index is never larger than the snippet's word_count.
- A passage stays inside one snippet. Prefer a participant's own words over a facilitator's \
prompt.
- Do not return a passage that overlaps one in already_highlighted: the user has it.
- reason is one short line saying what the passage offers the question, in terms of what is \
said in it. Never claim more than the words support.
- Return at most max_passages passages, the strongest first, and an empty list if nothing in \
this conversation genuinely speaks to the question.
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "passages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "snippet_id": {"type": "integer"},
                    "start_index": {"type": "integer"},
                    "end_index": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["snippet_id", "start_index", "end_index", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["passages"],
    "additionalProperties": False,
}


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return min(a_end, b_end) - max(a_start, b_start) > 0


def _apply_passages(
    prototype_db: Session,
    *,
    user_id: int,
    research_question_id: int,
    conversation_id: int,
    raw_passages: list[dict],
    snippets: dict[int, SnippetRef],
    user_highlight_spans: list[HighlightSpan],
    known_spans: list[tuple[int, int, int]],
    max_passages: int,
    ai_metadata: dict,
) -> list[PassageSuggestion]:
    """Persists the model's passages as suggestions, after checking each against the real
    transcript (`snippets`: this conversation's snippets by id). Dropped: an unknown snippet,
    a range that is not a half-open word range inside the snippet, a passage overlapping one
    of the user's highlights, one overlapping an earlier suggestion for this question
    (`known_spans`, whatever the user decided about it) or an earlier passage of this same
    output, and anything past `max_passages`."""
    taken = list(known_spans)
    created: list[PassageSuggestion] = []
    for raw in raw_passages:
        if len(created) >= max_passages:
            break
        snippet_id, start, end = raw.get("snippet_id"), raw.get("start_index"), raw.get("end_index")
        snippet = snippets.get(snippet_id) if isinstance(snippet_id, int) else None
        if snippet is None or snippet.conversation_id != conversation_id:
            continue
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(snippet.words)):
            continue
        if match_highlight(user_highlight_spans, snippet_id, start, end) is not None:
            continue
        if any(sid == snippet_id and _overlaps(start, end, s, e) for sid, s, e in taken):
            continue
        taken.append((snippet_id, start, end))
        suggestion = PassageSuggestion(
            user_id=user_id,
            research_question_id=research_question_id,
            conversation_id=conversation_id,
            snippet_id=snippet_id,
            start_index=start,
            end_index=end,
            reason=(raw.get("reason") or "").strip(),
            status="suggested",
            ai_metadata=json.dumps(ai_metadata),
        )
        prototype_db.add(suggestion)
        created.append(suggestion)
    prototype_db.commit()
    return created


def _question_context(prototype_db: Session, rq: ResearchQuestion, user_id: int, max_insights: int) -> dict:
    insights = (
        prototype_db.query(ReflectionConnection.insight_text)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(
            VoiceMemo.user_id == user_id,
            ReflectionConnection.status.in_(FILED_STATUSES),
            ReflectionConnection.research_question_id.in_(group_for(prototype_db, rq.id)),
        )
        .order_by(ReflectionConnection.updated_at.desc(), ReflectionConnection.id.desc())
        .limit(max_insights)
    )
    return {"name": rq.name, "description": rq.description, "insights": [row.insight_text for row in insights]}


def recently_opened(prototype_db: Session, source, user_id: int, project_ids: list[int]) -> list[ConversationRef]:
    """The conversations the user has opened (rl_reading_state) that their data source still
    shows them, most recently opened first."""
    states = (
        prototype_db.query(ReadingState)
        .filter(ReadingState.user_id == user_id)
        .order_by(ReadingState.last_opened_at.desc(), ReadingState.conversation_id.desc())
        .all()
    )
    visible = visible_conversations(source, project_ids, {s.conversation_id for s in states})
    return [visible[s.conversation_id] for s in states if s.conversation_id in visible]


@dataclass
class FindPassagesResult:
    research_question_id: int
    searched_conversation_ids: list[int] = field(default_factory=list)
    suggestions: list[PassageSuggestion] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # [{conversation_id, detail}]


def find_passages(
    *,
    prototype_db: Session,
    cortico_db,
    config: Config,
    user_id: int,
    research_question_id: int,
    conversations: list[ConversationRef],
) -> FindPassagesResult:
    """One model call per conversation, in the order given, up to
    `config.find_passages.max_conversations_per_run` conversations that have a transcript.
    Each conversation's suggestions are saved before the next call. If a call fails after
    another succeeded, the run stops and reports the failure in `failed`; if the first
    call fails, the AIProviderError is raised."""
    source = as_source(cortico_db)
    limits = config.find_passages
    target_id = resolve_rq_id(prototype_db, research_question_id)  # new rows go to the target
    rq = prototype_db.get(ResearchQuestion, target_id)
    group = group_for(prototype_db, target_id)
    question = _question_context(prototype_db, rq, user_id, limits.max_insights_in_prompt)
    result = FindPassagesResult(research_question_id=target_id)

    for conversation in conversations:
        if len(result.searched_conversation_ids) >= limits.max_conversations_per_run:
            break
        snippets = source.snippets(conversation.id)
        if not any(s.words for s in snippets):
            continue  # no transcript to search, so no call is spent on it
        spans = highlight_spans(prototype_db, user_id, conversation.id)
        payload = {
            "research_question": question,
            "conversation": {"id": conversation.id, "title": conversation.title},
            "transcript_snippets": [
                {
                    "snippet_id": s.id,
                    "speaker": s.speaker_name,
                    "is_facilitator": s.is_facilitator,
                    "text": s.text,
                    "word_count": len(s.words),
                }
                for s in snippets
            ],
            "already_highlighted": [
                {"snippet_id": s.snippet_id, "start_index": s.start_index, "end_index": s.end_index} for s in spans
            ],
            "max_passages": limits.max_passages_per_conversation,
        }
        try:
            answer = structured_json(
                system=FIND_PASSAGES_PROMPT, user=json.dumps(payload), schema=_OUTPUT_SCHEMA, name="find_passages"
            )
        except AIProviderError as exc:
            if not result.searched_conversation_ids:
                raise
            result.failed.append({"conversation_id": conversation.id, "detail": exc.detail})
            break
        known = [
            (p.snippet_id, p.start_index, p.end_index)
            for p in prototype_db.query(PassageSuggestion).filter(
                PassageSuggestion.user_id == user_id,
                PassageSuggestion.conversation_id == conversation.id,
                PassageSuggestion.research_question_id.in_(group),
            )
        ]
        result.suggestions += _apply_passages(
            prototype_db,
            user_id=user_id,
            research_question_id=target_id,
            conversation_id=conversation.id,
            raw_passages=answer.data.get("passages", []),
            snippets={s.id: s for s in snippets},
            user_highlight_spans=spans,
            known_spans=known,
            max_passages=limits.max_passages_per_conversation,
            ai_metadata={
                "model": answer.model,
                "reasoning_effort": answer.reasoning_effort,
                "prompt": FIND_PASSAGES_PROMPT,
            },
        )
        result.searched_conversation_ids.append(conversation.id)

    if result.searched_conversation_ids:
        count = len(result.suggestions)
        activity.log_activity(
            prototype_db, user_id, "ai", activity.PASSAGES_SUGGESTED,
            f"The AI found {activity.plural(count, 'passage')} for “{activity.excerpt(rq.name, 60)}” "
            f"in {activity.plural(len(result.searched_conversation_ids), 'conversation')}.",
            research_question_id=target_id, count=count, conversation_ids=result.searched_conversation_ids,
        )
        prototype_db.commit()
    return result
