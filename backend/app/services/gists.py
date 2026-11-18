"""Conversation summaries ("gists") made through the app.

`scripts/backfill_gists.py` fills `conversation_gist` from the local snapshot. This does the
same for whatever the request's data source can read, a few conversations per call, so the
conversations of a hosted workspace get gists too. A gist is per conversation, not per user.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..config import Config
from ..cortico.source import ConversationRef, CorticoSource, SnippetRef
from ..llm import AIProviderError, structured_json
from ..models_prototype import ConversationGist
from . import activity

GIST_PROMPT = """You are generating a short reference summary of a community conversation \
transcript, to help an AI later recommend which conversations are worth listening to next for \
a given research interest.

Based on the conversation's title, its project/forum/community context, and a sample of \
substantive snippets (mechanical lines and pure facilitator prompts have already been excluded), \
write:
- summary: 2-3 sentences describing what this conversation was actually about and what came up.
- keywords: 5-10 concrete topics/themes actually present in the sample.

Only describe what the sample actually supports. Don't invent topics, people, or claims that \
aren't reflected in it."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "keywords"],
    "additionalProperties": False,
}


def sample(snippets: list[SnippetRef], limit: int) -> list[SnippetRef]:
    """Snippets spread across the whole conversation, leaving out the facilitator unless
    nobody else spoke."""
    spoken = [s for s in snippets if s.text and not s.is_facilitator] or [s for s in snippets if s.text]
    if len(spoken) <= limit:
        return spoken
    step = len(spoken) / limit
    return [spoken[int(i * step)] for i in range(limit)]


def status(db: Session, conversations: list[ConversationRef]) -> dict:
    ids = {c.id for c in conversations}
    have = (
        {r.conversation_id for r in db.query(ConversationGist.conversation_id).filter(ConversationGist.conversation_id.in_(ids))}
        if ids
        else set()
    )
    return {"total": len(ids), "with_gist": len(have), "remaining": len(ids - have)}


def generate_missing(
    db: Session, source: CorticoSource, conversations: list[ConversationRef], user_id: int, config: Config
) -> dict:
    """Summarises up to `config.gists.max_per_request` conversations that have no gist yet,
    one model call each, committing after each so a failure keeps the ones before it. A
    conversation with no transcript text is skipped and counted, never retried in this call."""
    cfg = config.gists
    ids = [c.id for c in conversations]
    have = (
        {r.conversation_id for r in db.query(ConversationGist.conversation_id).filter(ConversationGist.conversation_id.in_(ids))}
        if ids
        else set()
    )
    todo = [c for c in sorted(conversations, key=lambda c: c.id) if c.id not in have and c.project_id is not None]
    names = source.project_names({c.project_id for c in todo[: cfg.max_per_request]})
    generated: list[int] = []
    empty: list[int] = []
    failed: list[dict] = []
    for conversation in todo[: cfg.max_per_request]:
        snippets = sample(source.snippets(conversation.id), cfg.sample_snippets)
        if not snippets:
            # An empty gist row, so the conversation is not fetched again on every call.
            db.add(ConversationGist(conversation_id=conversation.id, project_id=conversation.project_id,
                                    forum_id=conversation.forum_id, summary_text="", keywords_json="[]"))
            db.commit()
            empty.append(conversation.id)
            continue
        payload = {
            "title": conversation.title,
            "project": names.get(conversation.project_id),
            "snippets": [s.text for s in snippets],
        }
        try:
            answer = structured_json(
                system=GIST_PROMPT, user=json.dumps(payload), schema=OUTPUT_SCHEMA, name="conversation_gist",
                model=cfg.model, effort=cfg.reasoning_effort,
            )
        except AIProviderError as exc:
            failed.append({"conversation_id": conversation.id, "detail": exc.detail})
            break  # the next call would most likely fail the same way
        keywords = [str(k) for k in answer.data.get("keywords") or [] if isinstance(k, (str, int, float))]
        db.add(ConversationGist(
            conversation_id=conversation.id, project_id=conversation.project_id, forum_id=conversation.forum_id,
            summary_text=str(answer.data.get("summary") or ""), keywords_json=json.dumps(keywords),
        ))
        db.commit()
        generated.append(conversation.id)
    if generated:
        activity.log_activity(
            db, user_id, "ai", activity.GISTS_GENERATED,
            f"The AI summarised {activity.plural(len(generated), 'conversation')} so it can recommend what to read next.",
            count=len(generated),
        )
        db.commit()
    return {**status(db, conversations), "generated": generated, "without_transcript": empty, "failed": failed}
