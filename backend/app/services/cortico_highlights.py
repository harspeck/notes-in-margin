"""Highlights that already exist in Cortico, placed on the transcript. Read-only: nothing
here is stored, and nothing is written to Cortico."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..cortico.source import CorticoSource, SnippetRef
from ..models_prototype import RLCorticoSync, RLHighlight

# Word times and highlight offsets are both rounded upstream, so a word that starts a hair
# before the highlight still belongs to it.
TOLERANCE_SECONDS = 0.05


def spans_for(start: float, end: float, snippets: list[SnippetRef]) -> list[dict]:
    """Cortico returns a highlight as an audio range, not as words. This maps the range to
    half-open word ranges `[start_index, end_index)`, one per speaker turn it touches."""
    spans = []
    for s in snippets:
        if not s.words or s.start_offset is None:
            continue
        if s.start_offset >= end or (s.end_offset is not None and s.end_offset <= start):
            continue
        inside = [
            i
            for i, w in enumerate(s.words)
            if isinstance(w.get("start"), (int, float)) and start - TOLERANCE_SECONDS <= w["start"] < end
        ]
        if inside:
            spans.append({"snippet_id": s.id, "start_index": inside[0], "end_index": inside[-1] + 1})
    return spans


def _own_copy_ids(db: Session, user_id: int) -> set[int]:
    """Cortico ids of the highlights this user made here: those already show as their own."""
    rows = (
        db.query(RLCorticoSync.cortico_id)
        .join(RLHighlight, RLHighlight.id == RLCorticoSync.local_id)
        .filter(
            RLCorticoSync.local_kind == "highlight",
            RLCorticoSync.cortico_id.isnot(None),
            RLHighlight.user_id == user_id,
        )
        .all()
    )
    return {r[0] for r in rows}


def list_for_conversation(db: Session, source: CorticoSource, user_id: int, conversation_id: int) -> list[dict]:
    raw = source.cortico_highlights(conversation_id)
    if not raw:
        return []
    own = _own_copy_ids(db, user_id)
    snippets = source.snippets(conversation_id)
    out = []
    for h in raw:
        start, end = h.get("audio_start_offset"), h.get("audio_end_offset")
        if h.get("id") in own or h.get("is_active") is False or start is None or end is None:
            continue
        spans = spans_for(start, end, snippets)
        if not spans:
            continue
        out.append(
            {
                "cortico_id": h["id"],
                "title": h.get("title"),
                "description": h.get("description"),
                "author_name": h.get("user_name"),
                "mine": h.get("user_id") == user_id,
                "tool_id": h.get("tool_id"),
                "created_at": h.get("created_at"),
                "start_offset": start,
                "end_offset": end,
                "snippets": spans,
            }
        )
    out.sort(key=lambda h: h["start_offset"])
    return out
