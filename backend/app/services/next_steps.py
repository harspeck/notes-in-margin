"""Turns captured-but-unused signal (`voice_memo.extracted_interests`) into AI-proposed
next-step suggestions, grounded in cached corpus gists (see scripts/backfill_gists.py) —
same human-in-the-loop posture as ai_review.py's RQ connections: the model proposes,
nothing is "decided" until the user accepts or dismisses.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import as_source
from ..llm import AIProviderError, structured_json
from . import activity, listening_interests
from ..models_prototype import ConversationGist, NextStepSuggestion, ReadingState, RecommendationRun, ResearchQuestion, RLHighlight, TranscriptState, VoiceMemo

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
    """The user's active listening interests (rl_listening_interest): the lines extracted
    from all of their reflections, less the ones they removed, plus the ones they added.
    A user who has never opened the list gets their reflections' interests imported first.
    Empty when there are none."""
    listening_interests.ensure_imported(prototype_db, user_id)
    return [i.text for i in listening_interests.active_interests(prototype_db, user_id)]


def _engaged_conversation_ids(prototype_db: Session, user_id: int) -> set[int]:
    ids: set[int] = set()
    ids |= {
        row.conversation_id
        for row in prototype_db.query(TranscriptState.conversation_id).filter(TranscriptState.user_id == user_id)
    }
    # Opening a conversation counts, even with nothing highlighted or reflected on.
    ids |= {
        row.conversation_id
        for row in prototype_db.query(ReadingState.conversation_id).filter(ReadingState.user_id == user_id)
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


def _gist_context(prototype_db: Session, cortico_db: Session, scope_ids: set[int] | None = None) -> list[dict]:
    """`scope_ids` are the conversations of the user's workspace. Gists and the Cortico
    conversation cache are shared by every user, so in `cortico_api` mode a caller must pass
    it, or one user's summaries reach another user's prompt."""
    source = as_source(cortico_db)
    # An empty summary marks a conversation that had no transcript to summarise.
    query = prototype_db.query(ConversationGist).filter(ConversationGist.summary_text != "")
    if scope_ids is not None:
        if not scope_ids:
            return []
        query = query.filter(ConversationGist.conversation_id.in_(scope_ids))
    gists = query.all()
    known = source.conversations_by_ids({g.conversation_id for g in gists})
    if source.kind != "snapshot":
        # Only conversations this user's Cortico access has listed; never other users' corpora.
        gists = [g for g in gists if g.conversation_id in known]
    titles = {cid: c.title for cid, c in known.items()}
    return [gist_dict(g, titles.get(g.conversation_id)) for g in gists]


def gist_dict(gist: ConversationGist, title: str | None) -> dict:
    """How a cached gist is shown to the model, here and in services/question_gaps.py."""
    return {
        "conversation_id": gist.conversation_id,
        "title": title,
        "summary": gist.summary_text,
        "keywords": json.loads(gist.keywords_json) if gist.keywords_json else [],
    }


def gists_by_conversation(prototype_db: Session, conversation_ids: set[int]) -> dict[int, ConversationGist]:
    """Cached gists for these conversations. Empty when none were backfilled."""
    if not conversation_ids:
        return {}
    rows = prototype_db.query(ConversationGist).filter(
        ConversationGist.conversation_id.in_(conversation_ids), ConversationGist.summary_text != ""
    )
    return {g.conversation_id: g for g in rows}


def pending_recommendation(
    prototype_db: Session, user_id: int, scope_ids: set[int] | None = None
) -> NextStepSuggestion | None:
    """The newest undecided suggestion that names a conversation the user has not opened
    and, when `scope_ids` is given, can still read."""
    engaged = _engaged_conversation_ids(prototype_db, user_id)
    rows = (
        prototype_db.query(NextStepSuggestion)
        .filter(
            NextStepSuggestion.user_id == user_id,
            NextStepSuggestion.status == "suggested",
            NextStepSuggestion.recommended_conversation_id.isnot(None),
        )
        .order_by(NextStepSuggestion.created_at.desc(), NextStepSuggestion.id.desc())
    )
    return next(
        (
            r
            for r in rows
            if r.recommended_conversation_id not in engaged
            and (scope_ids is None or r.recommended_conversation_id in scope_ids)
        ),
        None,
    )


RUN_FAILED = "failed"


def recommend_next(
    *, prototype_db: Session, cortico_db: Session, config: Config, user_id: int, scope_ids: set[int] | None = None
) -> dict:
    """The conversation to read next, for the moment a session ends. Answers from what is
    already suggested when it can. It calls the model only when nothing is waiting, there
    are summaries of unopened conversations to choose from, the last run is not recent, and
    what the model would be shown has changed since that run. `reason` says why no call
    was made."""
    waiting = pending_recommendation(prototype_db, user_id, scope_ids)
    if waiting is not None:
        return {"recommendation": waiting, "generated": False, "reason": None}
    engaged = _engaged_conversation_ids(prototype_db, user_id)
    candidates = sorted(
        g["conversation_id"] for g in _gist_context(prototype_db, cortico_db, scope_ids) if g["conversation_id"] not in engaged
    )
    if not candidates:
        return {"recommendation": None, "generated": False, "reason": "no_summaries"}
    key = hashlib.sha256(
        json.dumps([candidates, _all_extracted_interests(prototype_db, user_id)]).encode()
    ).hexdigest()
    last = (
        prototype_db.query(RecommendationRun)
        .filter(RecommendationRun.user_id == user_id)
        .order_by(RecommendationRun.id.desc())
        .first()
    )
    if last is not None:
        made = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - made < timedelta(minutes=config.next_steps.min_minutes_between_auto_runs):
            return {"recommendation": None, "generated": False, "reason": "recent"}
        if last.fingerprint == key:
            # The model saw exactly this and named nothing the user can open.
            return {"recommendation": None, "generated": False, "reason": "unchanged"}
    # Saved before the model is asked, so a second request arriving meanwhile does not ask too.
    run = RecommendationRun(user_id=user_id, fingerprint=key)
    prototype_db.add(run)
    prototype_db.commit()
    try:
        generate_next_step_suggestions(
            prototype_db=prototype_db, cortico_db=cortico_db, config=config, user_id=user_id, scope_ids=scope_ids
        )
    except AIProviderError:
        run.fingerprint = RUN_FAILED
        prototype_db.commit()
        raise
    return {
        "recommendation": pending_recommendation(prototype_db, user_id, scope_ids),
        "generated": True,
        "reason": None,
    }


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
    scope_ids: set[int] | None = None,
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
        "conversation_gists": _gist_context(prototype_db, cortico_db, scope_ids),
    }

    answer = structured_json(
        system=NEXT_STEPS_PROMPT, user=json.dumps(payload), schema=_OUTPUT_SCHEMA, name="next_step_suggestions"
    )
    result = answer.data
    raw_suggestions = result.get("suggestions", [])

    candidate_conversation_ids = {
        s["recommended_conversation_id"] for s in raw_suggestions if s.get("recommended_conversation_id") is not None
    }
    valid_conversation_ids = _valid_conversation_ids(cortico_db, candidate_conversation_ids)
    if scope_ids is not None:
        valid_conversation_ids &= scope_ids  # the conversation cache is shared by every user

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
                    "model": answer.model,
                    "reasoning_effort": answer.reasoning_effort,
                    "prompt": NEXT_STEPS_PROMPT,
                }
            ),
        )
        prototype_db.add(suggestion)
        created.append(suggestion)

    activity.log_activity(
        prototype_db, user_id, "ai", activity.NEXT_STEPS_SUGGESTED,
        f"The AI suggested {activity.plural(len(created), 'next step')}."
        if created
        else "The AI had no next steps to suggest.",
        count=len(created),
    )
    prototype_db.commit()
    return created
