"""Links evidence passages back to the user's own highlights.

`connection_evidence.highlight_id` is what makes a passage "yours": it drives the
`yours`/`AI-suggested` badge in review, the "starred" flag on the memo page, and whether a
starred highlight counts as assigned. Every path that writes evidence (AI review, a
connection added in review, a memo-page evidence edit) runs its spans through
`resolve_evidence` so the link is set the same way everywhere.

Matching is by overlap, not exact span equality: the model (or the user) rarely cites
exactly the words that were highlighted, and a passage that shares any word with a
highlight on the same snippet is the user's highlight being cited. Spans are half-open
word ranges [start_index, end_index), the same convention as highlight_snippet.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models_prototype import ConnectionEvidence, ReflectionConnection, RLHighlight, VoiceMemo
from .schemas import EvidenceIn


@dataclass(frozen=True)
class HighlightSpan:
    highlight_id: int
    starred: bool
    snippet_id: int
    start_index: int
    end_index: int


def highlight_spans(prototype_db: Session, user_id: int, conversation_id: int) -> list[HighlightSpan]:
    highlights = (
        prototype_db.query(RLHighlight)
        .filter(RLHighlight.user_id == user_id, RLHighlight.conversation_id == conversation_id)
        .all()
    )
    return [
        HighlightSpan(h.id, bool(h.starred), s.snippet_id, s.start_index, s.end_index)
        for h in highlights
        for s in h.snippets  # type: ignore[attr-defined]
    ]


def user_highlight_spans(prototype_db: Session, user_id: int, snippet_ids: set[int]) -> list[HighlightSpan]:
    """The user's highlight spans on these snippets, whatever conversation they are in.
    Snippet ids are unique across conversations, so this is the right candidate set for an
    insight whose evidence spans several conversations (after an insight merge)."""
    from .models_prototype import HighlightSnippet

    if not snippet_ids:
        return []
    rows = (
        prototype_db.query(HighlightSnippet, RLHighlight)
        .join(RLHighlight, RLHighlight.id == HighlightSnippet.highlight_id)
        .filter(RLHighlight.user_id == user_id, HighlightSnippet.snippet_id.in_(snippet_ids))
        .all()
    )
    return [HighlightSpan(h.id, bool(h.starred), s.snippet_id, s.start_index, s.end_index) for s, h in rows]


def match_highlight(
    spans: list[HighlightSpan], snippet_id: int, start_index: int, end_index: int
) -> HighlightSpan | None:
    """The highlight this passage cites, if any. When several overlap, a starred one wins,
    then the one sharing the most words, then the oldest (lowest id) for stability."""
    best: tuple[tuple[bool, int, int], HighlightSpan] | None = None
    for s in spans:
        if s.snippet_id != snippet_id:
            continue
        overlap = min(end_index, s.end_index) - max(start_index, s.start_index)
        if overlap <= 0:
            continue
        key = (s.starred, overlap, -s.highlight_id)
        if best is None or key > best[0]:
            best = (key, s)
    return best[1] if best else None


def resolve_evidence(
    spans: list[HighlightSpan],
    *,
    snippet_id: int,
    start_index: int,
    end_index: int,
    source: str,
    highlight_id: int | None = None,
) -> tuple[int | None, str]:
    """Returns (highlight_id, source) for an evidence row about to be written.

    An explicit highlight_id from the caller is kept (the user picked that highlight).
    Otherwise the span is matched against the user's highlights; a match makes it the
    user's evidence. Unmatched spans keep the caller's source.
    """
    if highlight_id is not None:
        return highlight_id, "user"
    match = match_highlight(spans, snippet_id, start_index, end_index)
    if match is not None:
        return match.highlight_id, "user"
    return None, source


def assigned_highlight_ids(connections: list[ReflectionConnection]) -> set[int]:
    """Highlights cited as evidence by a connection that is on an RQ and not rejected or
    retracted. A starred highlight outside this set is "starred, unassigned"."""
    return {
        e.highlight_id
        for c in connections
        if c.research_question_id is not None and c.status not in ("rejected", "retracted")
        for e in c.evidence
        if e.highlight_id is not None
    }


def evidence_row(
    connection_id: int, ev: EvidenceIn, spans: list[HighlightSpan], vm: VoiceMemo, prototype_db: Session
) -> ConnectionEvidence:
    """Builds a user-side evidence row (review-step add/edit, memo-page edit) so
    highlight_id is set, or kept, the same way the AI review sets it. An explicit
    highlight_id must belong to the voice memo's user, and either sit on the voice memo's
    conversation or cover the evidence's snippet (an insight that gained evidence from
    another conversation through a merge cites that conversation's highlights)."""
    if ev.highlight_id is not None:
        h = prototype_db.get(RLHighlight, ev.highlight_id)
        if h is None or h.user_id != vm.user_id or not (
            h.conversation_id == vm.conversation_id
            or any(s.snippet_id == ev.snippet_id for s in h.snippets)  # type: ignore[attr-defined]
        ):
            raise HTTPException(400, f"highlight {ev.highlight_id} not found on this conversation")
    highlight_id, source = resolve_evidence(
        spans,
        snippet_id=ev.snippet_id,
        start_index=ev.start_index,
        end_index=ev.end_index,
        source=ev.source,
        highlight_id=ev.highlight_id,
    )
    return ConnectionEvidence(
        connection_id=connection_id,
        highlight_id=highlight_id,
        snippet_id=ev.snippet_id,
        start_index=ev.start_index,
        end_index=ev.end_index,
        source=source,
    )
