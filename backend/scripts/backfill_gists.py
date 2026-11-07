"""One-time, resumable backfill of `conversation_gist` rows for every Cortico conversation.

Not an API endpoint or something the UI triggers (see docs/prd-phase-2.md). Skips any
conversation that already has a gist row, so it's safe to re-run or resume after an
interruption. Pinned to reasoning effort "low" regardless of config.ai_review's own
setting, per the PRD — this backfill is ~200 cheap calls and should stay cheap even if
the interactive review call is later turned up.

Run from backend/: `python scripts/backfill_gists.py` (add --dry-run to preview without
calling the API, or --limit N to do a partial run).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config  # noqa: E402
from app.db import CorticoSession, PrototypeBase, PrototypeSession, prototype_engine  # noqa: E402
from app.llm import structured_json  # noqa: E402
from app.models_cortico import Community, Conversation, Forum, Project, Snippet  # noqa: E402
from app.models_prototype import ConversationGist  # noqa: E402

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

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "keywords"],
    "additionalProperties": False,
}

SNIPPET_SAMPLE_SIZE = 20


def _sampled_substantive_snippets(cortico_db, conversation_id: int, limit: int = SNIPPET_SAMPLE_SIZE):
    """A handful of snippets spread across the whole conversation, not just its opening —
    skips mechanical/facilitator-prompt lines, falling back to the full snippet list for the
    rare conversation that's nothing but those."""
    snippets = (
        cortico_db.query(Snippet)
        .filter(
            Snippet.conversation_id == conversation_id,
            Snippet.is_mechanical.is_(False),
            Snippet.is_facilitator_prompt.is_(False),
        )
        .order_by(Snippet.order_index)
        .all()
    )
    if not snippets:
        snippets = (
            cortico_db.query(Snippet)
            .filter(Snippet.conversation_id == conversation_id)
            .order_by(Snippet.order_index)
            .all()
        )
    if len(snippets) <= limit:
        return snippets
    step = len(snippets) / limit
    return [snippets[int(i * step)] for i in range(limit)]


def _context_names(cortico_db, conversation: Conversation) -> tuple[str | None, str | None, str | None]:
    project = cortico_db.get(Project, conversation.project_id) if conversation.project_id else None
    forum = cortico_db.get(Forum, conversation.forum_id) if conversation.forum_id else None
    community = cortico_db.get(Community, forum.community_id) if forum else None
    return (
        project.name if project else None,
        forum.name if forum else None,
        community.name if community else None,
    )


def backfill(*, limit: int | None = None, dry_run: bool = False) -> None:
    config = get_config()
    PrototypeBase.metadata.create_all(bind=prototype_engine)

    cortico_db = CorticoSession()
    prototype_db = PrototypeSession()

    try:
        existing_ids = {row.conversation_id for row in prototype_db.query(ConversationGist.conversation_id)}
        conversations = cortico_db.query(Conversation).order_by(Conversation.id).all()
        todo = [c for c in conversations if c.id not in existing_ids]
        if limit is not None:
            todo = todo[:limit]

        print(f"{len(existing_ids)} gists already exist; {len(todo)} to generate this run.")
        if not todo:
            return

        start = time.time()
        total_input_tokens = 0
        total_output_tokens = 0

        for i, conversation in enumerate(todo, start=1):
            snippets = _sampled_substantive_snippets(cortico_db, conversation.id)
            project_name, forum_name, community_name = _context_names(cortico_db, conversation)
            payload = {
                "title": conversation.title,
                "project": project_name,
                "forum": forum_name,
                "community": community_name,
                "snippets": [s.text for s in snippets],
            }

            if dry_run:
                print(f"[{i}/{len(todo)}] (dry-run) conversation {conversation.id} {conversation.title!r} "
                      f"— {len(snippets)} sample snippets")
                continue

            answer = structured_json(
                system=GIST_PROMPT, user=json.dumps(payload), schema=_OUTPUT_SCHEMA,
                name="conversation_gist", effort="low",
            )
            result = answer.data
            total_input_tokens += answer.input_tokens
            total_output_tokens += answer.output_tokens

            prototype_db.add(
                ConversationGist(
                    conversation_id=conversation.id,
                    project_id=conversation.project_id,
                    forum_id=conversation.forum_id,
                    summary_text=result["summary"],
                    keywords_json=json.dumps(result["keywords"]),
                )
            )
            # Commit per-conversation, not batched at the end, so an interruption loses at
            # most one call's worth of work and a re-run picks up where this left off.
            prototype_db.commit()
            print(f"[{i}/{len(todo)}] conversation {conversation.id} {conversation.title!r}: "
                  f"{result['summary'][:100]}")

        if dry_run:
            return

        elapsed = time.time() - start
        print(f"\nDone: {len(todo)} gists generated in {elapsed:.1f}s "
              f"({elapsed / len(todo):.2f}s/call avg).")
        print(f"Token usage: {total_input_tokens} input + {total_output_tokens} output tokens "
              f"across {len(todo)} calls on {config.ai_review.model}.")
    finally:
        cortico_db.close()
        prototype_db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only process the first N missing conversations")
    parser.add_argument("--dry-run", action="store_true", help="preview what would be summarized without calling the API")
    args = parser.parse_args()
    backfill(limit=args.limit, dry_run=args.dry_run)
