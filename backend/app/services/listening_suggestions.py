"""Lines the AI offers for "what I want to hear next", from the user's questions and their
newest highlights.

The Desk asks for them on its own, so the call is cheap (`config.listening_suggestions.model`)
and cached: `refresh` only calls the model when every earlier offer has been decided, the
questions or highlights have changed since the last run, and the last run is not too recent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import CorticoSource
from ..llm import structured_json
from ..models_prototype import (
    ListeningInterest, ListeningSuggestion, ListeningSuggestionRun, ResearchQuestion, RLHighlight,
)
from ..schemas import LISTENING_INTEREST_MAX_CHARS
from . import activity, listening_interests

PROMPT = """You help a researcher who is reading transcripts of community conversations decide \
what to listen for next.

You are given the questions they have jotted down, passages they highlighted recently (with \
any note they wrote), the lines already on their "what I want to hear next" list, and lines \
they turned down before.

Offer a few new lines for that list. Each line:
- is one short plain sentence in the researcher's own voice, starting with what they want to \
hear, for example "Hear from renters about meeting times";
- follows from a question or a highlight you were given, never from general knowledge;
- says something the existing lines do not, and is not a reworded line they turned down.

Give a one-sentence reason for each that names the question or highlight it came from. Offer \
fewer lines, or none, when the material does not support more."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["text", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def pending(db: Session, user_id: int) -> list[ListeningSuggestion]:
    return (
        db.query(ListeningSuggestion)
        .filter(ListeningSuggestion.user_id == user_id, ListeningSuggestion.status == "suggested")
        .order_by(ListeningSuggestion.id)
        .all()
    )


def _questions(db: Session, user_id: int) -> list[ResearchQuestion]:
    return (
        db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user_id, ResearchQuestion.archived.is_(False))
        .order_by(ResearchQuestion.display_order, ResearchQuestion.id)
        .all()
    )


def _recent_highlights(db: Session, user_id: int, limit: int) -> list[RLHighlight]:
    return (
        db.query(RLHighlight)
        .filter(RLHighlight.user_id == user_id)
        .order_by(RLHighlight.created_at.desc(), RLHighlight.id.desc())
        .limit(limit)
        .all()
    )


def fingerprint(questions: list[ResearchQuestion], highlights: list[RLHighlight]) -> str:
    """Changes when a question is added, reworded or archived, or a highlight is made, removed
    or annotated."""
    material = json.dumps(
        {
            "questions": [[q.id, q.name, q.description] for q in questions],
            "highlights": sorted([h.id, h.notes or ""] for h in highlights),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _highlight_payload(source: CorticoSource, highlights: list[RLHighlight]) -> list[dict]:
    snippets = source.snippets_by_ids({s.snippet_id for h in highlights for s in h.snippets})
    out = []
    for h in highlights:
        parts = []
        for span in h.snippets:
            snippet = snippets.get(span.snippet_id)
            if snippet is not None:
                parts.append(" ".join(w["word"] for w in snippet.words[span.start_index : span.end_index]))
        text = " ".join(p for p in parts if p)
        if text:
            out.append({"text": text, "note": h.notes or None})
    return out


def apply_suggestions(
    db: Session, user_id: int, run: ListeningSuggestionRun, raw: list, *, limit: int, ai_metadata: dict
) -> list[ListeningSuggestion]:
    """Saves what the model offered, less anything the user already has, was already offered
    or turned down. Adds to the session without committing."""
    taken = {_norm(i.text) for i in db.query(ListeningInterest).filter(ListeningInterest.user_id == user_id)}
    taken |= {_norm(s.text) for s in db.query(ListeningSuggestion).filter(ListeningSuggestion.user_id == user_id)}
    created: list[ListeningSuggestion] = []
    for item in raw if isinstance(raw, list) else []:
        if len(created) >= limit or not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())[:LISTENING_INTEREST_MAX_CHARS]
        if not text or _norm(text) in taken:
            continue
        taken.add(_norm(text))
        row = ListeningSuggestion(
            user_id=user_id, run_id=run.id, text=text, reason=" ".join(str(item.get("reason") or "").split()) or None,
            status="suggested", ai_metadata=json.dumps(ai_metadata),
        )
        db.add(row)
        created.append(row)
    run.suggestion_count = len(created)
    return created


def refresh(db: Session, source: CorticoSource, user_id: int, config: Config) -> dict:
    """Returns the undecided suggestions, calling the model first only when the cache rules
    above allow it. `reason` says why no call was made."""
    cfg = config.listening_suggestions
    waiting = pending(db, user_id)
    if waiting:
        return {"suggestions": waiting, "generated": False, "reason": "undecided"}
    questions = _questions(db, user_id)
    highlights = _recent_highlights(db, user_id, cfg.recent_highlights)
    if not questions and not highlights:
        return {"suggestions": [], "generated": False, "reason": "nothing_to_go_on"}
    key = fingerprint(questions, highlights)
    last = (
        db.query(ListeningSuggestionRun)
        .filter(ListeningSuggestionRun.user_id == user_id)
        .order_by(ListeningSuggestionRun.id.desc())
        .first()
    )
    if last is not None:
        if last.fingerprint == key:
            return {"suggestions": [], "generated": False, "reason": "unchanged"}
        made = last.created_at if last.created_at.tzinfo else last.created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - made < timedelta(minutes=cfg.min_minutes_between_runs):
            return {"suggestions": [], "generated": False, "reason": "recent"}

    listening_interests.ensure_imported(db, user_id)
    payload = {
        "questions": [{"name": q.name, "description": q.description} for q in questions],
        "recent_highlights": _highlight_payload(source, highlights),
        "lines_already_on_the_list": [i.text for i in listening_interests.active_interests(db, user_id)],
        "lines_turned_down": [
            s.text
            for s in db.query(ListeningSuggestion).filter(
                ListeningSuggestion.user_id == user_id, ListeningSuggestion.status == "dismissed"
            )
        ],
        "offer_at_most": cfg.max_suggestions,
    }
    answer = structured_json(
        system=PROMPT, user=json.dumps(payload), schema=OUTPUT_SCHEMA, name="listening_suggestions",
        model=cfg.model, effort=cfg.reasoning_effort,
    )
    run = ListeningSuggestionRun(user_id=user_id, fingerprint=key)
    db.add(run)
    db.flush()
    created = apply_suggestions(
        db, user_id, run, answer.data.get("suggestions"), limit=cfg.max_suggestions,
        ai_metadata={"model": answer.model, "reasoning_effort": answer.reasoning_effort, "prompt": PROMPT},
    )
    activity.log_activity(
        db, user_id, "ai", activity.LISTENING_LINES_SUGGESTED,
        f"The AI offered {activity.plural(len(created), 'line')} for what you want to hear next."
        if created
        else "The AI had no new lines to offer for what you want to hear next.",
        count=len(created),
    )
    db.commit()
    return {"suggestions": created, "generated": True, "reason": None}
