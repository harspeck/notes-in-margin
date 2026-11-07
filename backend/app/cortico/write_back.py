"""Mirrors local objects into Cortico. Today: highlights.

The local write always lands first and is never rolled back by a Cortico failure. Each
mirrored object gets one `rl_cortico_sync` row, so retries are idempotent (an object that
already has a Cortico id is never pushed twice) and failures are visible to the UI.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models_prototype import RLCorticoSync, RLHighlight
from .errors import CorticoError, CorticoNotFound
from .source import ApiSource, ConversationRef, SnippetRef


class NotRepresentable(ValueError):
    """The highlight can't be expressed as one Cortico highlight."""


def build_highlight_snippets(spans: list[tuple[int, int, int]], snippets: list[SnippetRef]) -> list[dict]:
    """spans: (snippet_id, start, end) with half-open word ranges [start, end), as stored in
    highlight_snippet. Cortico's word_indices are inclusive on both ends, and a highlight
    must be one continuous passage: every snippet after the first starts at word 0, every
    snippet before the last ends at its last word, and no non-empty snippet is skipped."""
    order = {s.id: i for i, s in enumerate(snippets)}
    by_id = {s.id: s for s in snippets}
    if not spans:
        raise NotRepresentable("highlight has no passages")
    missing = [sid for sid, _, _ in spans if sid not in by_id]
    if missing:
        raise NotRepresentable(f"snippets {missing} aren't in this conversation's transcript")
    spans = sorted(spans, key=lambda s: order[s[0]])

    for (prev, _, _), (cur, _, _) in zip(spans, spans[1:]):
        if order[cur] == order[prev]:
            raise NotRepresentable("two passages share a snippet")
        between = snippets[order[prev] + 1 : order[cur]]
        if any(s.words for s in between):
            raise NotRepresentable("passages aren't contiguous")

    out = []
    for i, (sid, start, end) in enumerate(spans):
        snip = by_id[sid]
        n = len(snip.words)
        if not snip.uuid or n == 0:
            raise NotRepresentable(f"snippet {sid} has no uuid or words")
        if not (0 <= start < end <= n):
            raise NotRepresentable(f"word range [{start}, {end}) is outside snippet {sid}")
        if i > 0 and start != 0:
            raise NotRepresentable("a continuing passage must start at the snippet's first word")
        if i < len(spans) - 1 and end != n:
            raise NotRepresentable("a passage that continues must run to the snippet's last word")
        out.append({"uuid": snip.uuid, "word_indices": {"start_index": start, "end_index": end - 1}})
    return out


def _sync_row(db: Session, highlight_id: int) -> RLCorticoSync:
    row = (
        db.query(RLCorticoSync)
        .filter(RLCorticoSync.local_kind == "highlight", RLCorticoSync.local_id == highlight_id)
        .one_or_none()
    )
    if row is None:
        row = RLCorticoSync(local_kind="highlight", local_id=highlight_id, cortico_kind="highlight", status="pending")
        db.add(row)
    return row


def push_highlight(db: Session, source: ApiSource, highlight: RLHighlight, conversation: ConversationRef,
                   *, tool_id: str) -> RLCorticoSync:
    row = _sync_row(db, highlight.id)
    if row.cortico_id is not None and row.status == "synced":
        return row
    row.attempts = (row.attempts or 0) + 1
    try:
        snippets = source.snippets(conversation.id)
        spans = [(s.snippet_id, s.start_index, s.end_index) for s in highlight.snippets]
        payload_snippets = build_highlight_snippets(spans, snippets)
        first_words = " ".join(
            w["word"] for w in next(s for s in snippets if s.id == spans[0][0]).words[spans[0][1] : spans[0][2]]
        )
        payload = {
            "conversation_id": conversation.id,
            "project_id": conversation.project_id,
            "forum_id": conversation.forum_id,
            "title": highlight.title or (first_words[:80] or f"Highlight {highlight.id}"),
            "notes": highlight.notes,
            "snippets": payload_snippets,
            "tool_id": tool_id,
            "is_auto_highlight": False,
            "metadata": {"source": tool_id, "local_highlight_id": highlight.id},
        }
        created = source.client.create_highlight(payload)
        row.cortico_id = created.get("id")
        row.status = "synced"
        row.error = None
    except NotRepresentable as exc:
        row.status = "skipped"
        row.error = str(exc)
    except CorticoError as exc:
        row.status = "failed"
        row.error = exc.detail
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return row


def delete_highlight_copy(db: Session, source: ApiSource, highlight_id: int) -> RLCorticoSync | None:
    """Deletes the Cortico copy this app created. Never touches highlights it didn't create."""
    row = (
        db.query(RLCorticoSync)
        .filter(RLCorticoSync.local_kind == "highlight", RLCorticoSync.local_id == highlight_id)
        .one_or_none()
    )
    if row is None or row.cortico_id is None or row.status == "deleted":
        return row
    try:
        source.client.delete_highlight(row.cortico_id)
        row.status = "deleted"
        row.error = None
    except CorticoNotFound:
        row.status = "deleted"
    except CorticoError as exc:
        row.status = "failed"
        row.error = f"delete failed: {exc.detail}"
    db.commit()
    return row


def sync_status(db: Session, highlight_ids: list[int]) -> dict[int, dict]:
    if not highlight_ids:
        return {}
    rows = (
        db.query(RLCorticoSync)
        .filter(RLCorticoSync.local_kind == "highlight", RLCorticoSync.local_id.in_(highlight_ids))
        .all()
    )
    return {r.local_id: {"status": r.status, "cortico_id": r.cortico_id, "error": r.error} for r in rows}
