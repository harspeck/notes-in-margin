"""Turns captured-but-unused signal (`voice_memo.extracted_interests`) into AI-proposed
next-step suggestions, grounded in cached corpus gists (see scripts/backfill_gists.py) —
same human-in-the-loop posture as ai_review.py's RQ connections: the model proposes,
nothing is "decided" until the user accepts or dismisses.
"""
from __future__ import annotations

import json

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import as_source
from ..models_prototype import ConversationGist, NextStepSuggestion, ResearchQuestion, RLHighlight, TranscriptState, VoiceMemo

NEXT_STEPS_PROMPT = """You help a sensemaker decide what to listen to next in a large interview \
corpus, based on things they've said they want to hear more about.

Rules:
- Only propose directions or conversations grounded in the inputs you're given: the user's own \
"what I'd like to hear next" statements (extracted_interests), their active research questions, \
and the cached conversation summaries ("gists") provided. Never invent a topic, conversation, or \
claim not reflected in these inputs.
- Prefer recommending conversations the user hasn't engaged with yet (anything not listed in \
already_engaged_conversation_ids) over ones they have already heard, but recommending an \
already-engaged one is acceptable if nothing unvisited genuinely fits.
- Don't repeat an existing_suggestions entry that already covers essentially the same direction \
or conversation — propose something new, or something more specific.
- A suggestion can be a direction with no specific conversation, a conversation with no stated \
direction, or both together — only fill in what the inputs actually support.
- rationale must explain, in terms of the actual extracted_interests or gist content you were \
given, why this suggestion makes sense.
- Return an empty suggestions list rather than forcing weak ones if nothing genuinely stands out.
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "research_question_id": {"type": ["integer", "null"]},
                    "suggested_direction": {"type": ["string", "null"]},
                    "recommended_conversation_id": {"type": ["integer", "null"]},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "research_question_id",
                    "suggested_direction",
                    "recommended_conversation_id",
                    "rationale",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def _all_extracted_interests(prototype_db: Session, user_id: int) -> list[str]:
    """Across *all* of the user's voice memos, not just the most recent — interests
    accumulate across sessions."""
    memos = prototype_db.query(VoiceMemo).filter(VoiceMemo.user_id == user_id).all()
    interests: list[str] = []
    for vm in memos:
        if vm.extracted_interests:
            interests.extend(json.loads(vm.extracted_interests))
    return interests


def _engaged_conversation_ids(prototype_db: Session, user_id: int) -> set[int]:
    ids: set[int] = set()
    ids |= {
        row.conversation_id
        for row in prototype_db.query(TranscriptState.conversation_id).filter(TranscriptState.user_id == user_id)
    }
    ids |= {
        row.conversation_id
        for row in prototype_db.query(RLHighlight.conversation_id).filter(RLHighlight.user_id == user_id)
    }
    ids |= {
        row.conversation_id
        for row in prototype_db.query(VoiceMemo.conversation_id).filter(VoiceMemo.user_id == user_id)
    }
    return ids


def _existing_suggestions_context(prototype_db: Session, user_id: int) -> list[dict]:
    """Non-dismissed suggestions already on the books, so the model doesn't repeat itself."""
    rows = (
        prototype_db.query(NextStepSuggestion)
        .filter(NextStepSuggestion.user_id == user_id, NextStepSuggestion.status != "dismissed")
        .all()
    )
    return [
        {
            "suggested_direction": r.suggested_direction,
            "recommended_conversation_id": r.recommended_conversation_id,
            "research_question_id": r.research_question_id,
        }
        for r in rows
    ]


def _gist_context(prototype_db: Session, cortico_db: Session) -> list[dict]:
    source = as_source(cortico_db)
    gists = prototype_db.query(ConversationGist).all()
    known = source.conversations_by_ids({g.conversation_id for g in gists})
    if source.kind != "snapshot":
        # Only conversations this user's Cortico access has listed; never other users' corpora.
        gists = [g for g in gists if g.conversation_id in known]
    titles = {cid: c.title for cid, c in known.items()}
    return [
        {
            "conversation_id": g.conversation_id,
            "title": titles.get(g.conversation_id),
            "summary": g.summary_text,
            "keywords": json.loads(g.keywords_json) if g.keywords_json else [],
        }
        for g in gists
    ]


def _valid_conversation_ids(cortico_db: Session, candidate_ids: set[int]) -> set[int]:
    """Structural validation mirroring ai_review._validate_evidence_in_range: every
    recommended_conversation_id must resolve to a real Cortico conversation before it's
    ever persisted — don't trust the model's ids blindly."""
    if not candidate_ids:
        return set()
    return set(as_source(cortico_db).conversations_by_ids(candidate_ids))


def generate_next_step_suggestions(
    *,
    prototype_db: Session,
    cortico_db: Session,
    config: Config,
    user_id: int,
) -> list[NextStepSuggestion]:
    research_questions = (
        prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user_id, ResearchQuestion.archived.is_(False))
        .order_by(ResearchQuestion.display_order)
        .all()
    )
    valid_rq_ids = {rq.id for rq in research_questions}

    payload = {
        "extracted_interests": _all_extracted_interests(prototype_db, user_id),
        "research_questions": [{"id": rq.id, "name": rq.name, "description": rq.description} for rq in research_questions],
        "already_engaged_conversation_ids": sorted(_engaged_conversation_ids(prototype_db, user_id)),
        "existing_suggestions": _existing_suggestions_context(prototype_db, user_id),
        "conversation_gists": _gist_context(prototype_db, cortico_db),
    }

    client = OpenAI()
    response = client.responses.create(
        model=config.ai_review.model,
        reasoning={"effort": config.ai_review.reasoning_effort},
        input=[
            {"role": "system", "content": NEXT_STEPS_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "next_step_suggestions",
                "schema": _OUTPUT_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    raw_suggestions = result.get("suggestions", [])

    candidate_conversation_ids = {
        s["recommended_conversation_id"] for s in raw_suggestions if s.get("recommended_conversation_id") is not None
    }
    valid_conversation_ids = _valid_conversation_ids(cortico_db, candidate_conversation_ids)

    created: list[NextStepSuggestion] = []
    for s in raw_suggestions:
        direction = s.get("suggested_direction")
        conv_id = s.get("recommended_conversation_id")
        if conv_id is not None and conv_id not in valid_conversation_ids:
            conv_id = None  # hallucinated/out-of-range id — dropped, not trusted
        if not direction and conv_id is None:
            continue  # nothing real left to persist for this candidate

        rq_id = s.get("research_question_id")
        if rq_id is not None and rq_id not in valid_rq_ids:
            rq_id = None

        suggestion = NextStepSuggestion(
            user_id=user_id,
            research_question_id=rq_id,
            suggested_direction=direction,
            recommended_conversation_id=conv_id,
            rationale=s.get("rationale", ""),
            status="suggested",
            ai_metadata=json.dumps(
                {
                    "model": config.ai_review.model,
                    "reasoning_effort": config.ai_review.reasoning_effort,
                    "prompt": NEXT_STEPS_PROMPT,
                }
            ),
        )
        prototype_db.add(suggestion)
        created.append(suggestion)

    prototype_db.commit()
    return created
