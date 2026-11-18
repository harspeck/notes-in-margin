"""Resolves evidence word-index spans back to actual quoted transcript text, so the
review and memo screens can show what the passage actually says instead of a bare
snippet id. Read-only against the Cortico snippets — see docs/prototype-log.md."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .cortico.source import as_source


def quoted_texts(
    cortico_db: Session, spans: list[tuple[int, int, int]]
) -> dict[tuple[int, int, int], str]:
    """spans: list of (snippet_id, start_index, end_index). Returns a dict keyed by the
    same tuples, batched into a single query regardless of how many spans are requested."""
    snippet_ids = {sid for sid, _, _ in spans}
    if not snippet_ids:
        return {}
    snippets = as_source(cortico_db).snippets_by_ids(snippet_ids)

    out: dict[tuple[int, int, int], str] = {}
    for sid, start, end in spans:
        snippet = snippets.get(sid)
        if snippet is None or not snippet.words:
            out[(sid, start, end)] = ""
            continue
        out[(sid, start, end)] = " ".join(w["word"] for w in snippet.words[start:end])
    return out


def evidence_conversations(cortico_db, snippet_ids: set[int]) -> dict[int, tuple[int, str | None]]:
    """snippet id -> (conversation id, conversation title). An insight's evidence can come
    from several conversations (after an insight merge), so serializers label each evidence
    row with its own conversation, not the reflection's. Unknown snippets are left out."""
    snippet_ids = set(snippet_ids)
    if not snippet_ids:
        return {}
    source = as_source(cortico_db)
    conversation_of = {sid: s.conversation_id for sid, s in source.snippets_by_ids(snippet_ids).items()}
    conversations = source.conversations_by_ids(set(conversation_of.values()))
    return {
        sid: (cid, conversations[cid].title if cid in conversations else None)
        for sid, cid in conversation_of.items()
    }
