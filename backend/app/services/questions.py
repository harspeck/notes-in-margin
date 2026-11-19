"""What gathers under a research question besides insights: the highlights the user filed
under it, how widely it has come up (breadth), and starting questions from the conversation
guide. Everything here reads a question's merged group (services/rq_merges.py).
"""
from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import get_config
from ..cortico.source import ConversationRef, as_source
from ..evidence_text import quoted_texts
from ..models_prototype import (
    ConnectionEvidence,
    HighlightQuestion,
    ReadingState,
    ReflectionConnection,
    ResearchQuestion,
    RLHighlight,
    VoiceMemo,
)
from ..timefmt import iso_utc
from .code_families import FILED_STATUSES, group_for
from .rq_merges import active_merge_edges, resolve_rq_id, rq_group_ids

GUIDE_IDEAS_LIMIT = 12
# Workspace projects whose guide is not cached yet cost one Cortico call each.
GUIDE_IDEAS_MAX_PROJECT_FETCHES = 5


# --- creating a question ----------------------------------------------------------------


def create_question(
    prototype_db: Session, user_id: int, workspace_project_ids: list[int], name: str, description: str | None
) -> ResearchQuestion:
    """Adds a research question for the user and flushes it. The one place a question row is
    made (POST /research_questions and adopting a teammate's question), so project id and
    display order are always set the same way. The caller logs activity and commits."""
    rq = ResearchQuestion(
        user_id=user_id,
        # A research question spans the workspace; project_id records where it started.
        project_id=workspace_project_ids[0] if workspace_project_ids else get_config().default_project_id,
        name=name,
        description=description,
        display_order=prototype_db.query(ResearchQuestion).filter(ResearchQuestion.user_id == user_id).count(),
    )
    prototype_db.add(rq)
    prototype_db.flush()
    return rq


# --- scope ------------------------------------------------------------------------------


def visible_conversations(source, project_ids: list[int], conversation_ids: set[int]) -> dict[int, ConversationRef]:
    """The conversations among `conversation_ids` the user's data source shows them: the
    active workspace's projects, or in snapshot mode with no workspace, the whole corpus
    (the same scope as GET /conversations and GET /desk)."""
    source = as_source(source)
    if source.kind == "cortico_api" and not project_ids:
        return {}
    known = source.conversations_by_ids(conversation_ids)
    if project_ids:
        allowed = set(project_ids)
        known = {cid: c for cid, c in known.items() if c.project_id in allowed}
    return known


def scope_conversations(source, project_ids: list[int]) -> list[ConversationRef]:
    """Every conversation in the user's scope. In cortico_api mode this is the sidecar's
    cached listing of the workspace's projects (see ApiSource.scope_conversations)."""
    source = as_source(source)
    if source.kind == "cortico_api" and not project_ids:
        return []
    return source.scope_conversations(project_ids or None)


def opened_conversation_ids(prototype_db: Session, user_id: int) -> set[int]:
    """Conversations the user has opened: a reading-state row, a highlight or a voice memo."""
    ids = {
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


# --- highlights filed under questions ---------------------------------------------------


def checked_question_ids(prototype_db: Session, user_id: int, rq_ids: list[int]) -> list[int]:
    """Each id resolved through merges to the question that holds it now, de-duplicated.
    404 for a question that isn't the user's; 409 for an archived one that isn't merged."""
    edges = active_merge_edges(prototype_db)
    out: list[int] = []
    for rq_id in dict.fromkeys(rq_ids):
        rq = prototype_db.get(ResearchQuestion, rq_id)
        if rq is None or rq.user_id != user_id:
            raise HTTPException(404, "research question not found")
        target_id = resolve_rq_id(prototype_db, rq_id, edges)
        target = rq if target_id == rq_id else prototype_db.get(ResearchQuestion, target_id)
        if target is None or target.archived:
            raise HTTPException(409, f"research question {rq_id} is archived")
        out.append(target_id)
    return list(dict.fromkeys(out))


def question_ids_by_highlight(prototype_db: Session, highlight_ids: list[int]) -> dict[int, list[int]]:
    """highlight id -> the questions it is filed under, each resolved through merges."""
    if not highlight_ids:
        return {}
    edges = active_merge_edges(prototype_db)
    out: dict[int, list[int]] = {}
    rows = (
        prototype_db.query(HighlightQuestion)
        .filter(HighlightQuestion.highlight_id.in_(highlight_ids))
        .order_by(HighlightQuestion.id)
    )
    for link in rows:
        rq_id = resolve_rq_id(prototype_db, link.research_question_id, edges)
        ids = out.setdefault(link.highlight_id, [])
        if rq_id not in ids:
            ids.append(rq_id)
    return out


def set_highlight_questions(prototype_db: Session, highlight: RLHighlight, rq_ids: list[int]) -> bool:
    """Replaces the highlight's questions with `rq_ids` (already checked). Returns whether
    anything changed. The caller commits."""
    links = prototype_db.query(HighlightQuestion).filter(HighlightQuestion.highlight_id == highlight.id).all()
    wanted = set(rq_ids)
    changed = False
    have: set[int] = set()
    for link in links:
        if link.research_question_id in wanted and link.research_question_id not in have:
            have.add(link.research_question_id)
        else:
            prototype_db.delete(link)
            changed = True
    for rq_id in rq_ids:
        if rq_id not in have:
            prototype_db.add(
                HighlightQuestion(highlight_id=highlight.id, research_question_id=rq_id, user_id=highlight.user_id)
            )
            have.add(rq_id)
            changed = True
    return changed


def add_highlight_question(prototype_db: Session, highlight: RLHighlight, rq_id: int) -> None:
    exists = (
        prototype_db.query(HighlightQuestion)
        .filter(HighlightQuestion.highlight_id == highlight.id, HighlightQuestion.research_question_id == rq_id)
        .first()
    )
    if exists is None:
        prototype_db.add(
            HighlightQuestion(highlight_id=highlight.id, research_question_id=rq_id, user_id=highlight.user_id)
        )


def delete_links_for_highlight(prototype_db: Session, highlight_id: int) -> None:
    prototype_db.query(HighlightQuestion).filter(HighlightQuestion.highlight_id == highlight_id).delete()


def _cited_highlight_ids(prototype_db: Session, user_id: int, highlight_ids: set[int]) -> set[int]:
    """Those of `highlight_ids` cited as evidence by any filed insight of the user's."""
    if not highlight_ids:
        return set()
    rows = (
        prototype_db.query(ConnectionEvidence.highlight_id)
        .join(ReflectionConnection, ReflectionConnection.id == ConnectionEvidence.connection_id)
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(
            VoiceMemo.user_id == user_id,
            ReflectionConnection.status.in_(FILED_STATUSES),
            ConnectionEvidence.highlight_id.in_(highlight_ids),
        )
    )
    return {row.highlight_id for row in rows}


def uncited_highlights(prototype_db: Session, source, user_id: int, rq_id: int) -> list[RLHighlight]:
    """The user's highlights filed under rq_id's merged group that no filed insight cites."""
    group = group_for(prototype_db, rq_id)
    highlights = (
        prototype_db.query(RLHighlight)
        .join(HighlightQuestion, HighlightQuestion.highlight_id == RLHighlight.id)
        .filter(RLHighlight.user_id == user_id, HighlightQuestion.research_question_id.in_(group))
        .order_by(RLHighlight.created_at, RLHighlight.id)
        .all()
    )
    highlights = list({h.id: h for h in highlights}.values())  # filed under two questions of one group
    cited = _cited_highlight_ids(prototype_db, user_id, {h.id for h in highlights})
    return [h for h in highlights if h.id not in cited]


def serialize_question_highlights(prototype_db: Session, source, highlights: list[RLHighlight]) -> list[dict]:
    source = as_source(source)
    text_by_span = quoted_texts(
        source, [(s.snippet_id, s.start_index, s.end_index) for h in highlights for s in h.snippets]
    )
    conversations = source.conversations_by_ids({h.conversation_id for h in highlights})
    rq_ids = question_ids_by_highlight(prototype_db, [h.id for h in highlights])

    def one(h: RLHighlight) -> dict:
        conv = conversations.get(h.conversation_id)
        snippets = [
            {
                "snippet_id": s.snippet_id,
                "start_index": s.start_index,
                "end_index": s.end_index,
                "text": text_by_span.get((s.snippet_id, s.start_index, s.end_index), ""),
            }
            for s in h.snippets
        ]
        return {
            "highlight_id": h.id,
            "conversation_id": h.conversation_id,
            "conversation_title": conv.title if conv else None,
            "title": h.title,
            "notes": h.notes,
            "starred": bool(h.starred),
            "created_at": iso_utc(h.created_at),
            "research_question_ids": rq_ids.get(h.id, []),
            "snippets": snippets,
            "text": " ".join(s["text"] for s in snippets if s["text"]),
        }

    return [one(h) for h in highlights]


# --- breadth ----------------------------------------------------------------------------


def breadth(prototype_db: Session, source, user_id: int, project_ids: list[int]) -> dict:
    """How widely each active question has come up, in a fixed number of queries whatever
    the number of questions: questions, merges, filed insights, their evidence, highlight
    links, the three "opened" sources, and two batched Cortico reads."""
    source = as_source(source)
    edges = active_merge_edges(prototype_db)
    questions = [
        rq
        for rq in prototype_db.query(ResearchQuestion)
        .filter(ResearchQuestion.user_id == user_id, ResearchQuestion.archived.is_(False))
        .order_by(ResearchQuestion.display_order, ResearchQuestion.id)
        if rq.id not in edges
    ]
    target_of = {
        member: rq.id for rq in questions for member in rq_group_ids(prototype_db, rq.id, edges)
    }

    filed = (
        prototype_db.query(
            ReflectionConnection.id, ReflectionConnection.research_question_id, VoiceMemo.conversation_id
        )
        .join(VoiceMemo, VoiceMemo.id == ReflectionConnection.voice_memo_id)
        .filter(VoiceMemo.user_id == user_id, ReflectionConnection.status.in_(FILED_STATUSES))
        .all()
    )
    connection_ids = [row.id for row in filed]
    evidence = (
        prototype_db.query(ConnectionEvidence.connection_id, ConnectionEvidence.snippet_id)
        .filter(ConnectionEvidence.connection_id.in_(connection_ids))
        .all()
        if connection_ids
        else []
    )
    # After an insight merge, an insight's evidence can come from other conversations.
    snippets = source.snippets_by_ids({row.snippet_id for row in evidence})
    conversations_of: dict[int, set[int]] = {row.id: {row.conversation_id} for row in filed}
    for row in evidence:
        snippet = snippets.get(row.snippet_id)
        if snippet is not None:
            conversations_of[row.connection_id].add(snippet.conversation_id)

    links = (
        prototype_db.query(HighlightQuestion.research_question_id, RLHighlight.id, RLHighlight.conversation_id)
        .join(RLHighlight, RLHighlight.id == HighlightQuestion.highlight_id)
        .filter(RLHighlight.user_id == user_id)
        .all()
    )

    opened = opened_conversation_ids(prototype_db, user_id)
    referenced = set(opened) | {cid for ids in conversations_of.values() for cid in ids}
    visible = visible_conversations(source, project_ids, referenced)

    if project_ids:
        in_scope = sorted(set(project_ids))
    elif source.kind == "snapshot":
        in_scope = sorted({c.project_id for c in source.scope_conversations(None) if c.project_id is not None})
    else:
        in_scope = []

    insight_convs: dict[int | None, set[int]] = {}
    insight_count: Counter = Counter()
    for row in filed:
        key = target_of.get(row.research_question_id)  # None: no question, or an archived one
        insight_count[key] += 1
        insight_convs.setdefault(key, set()).update(c for c in conversations_of[row.id] if c in visible)
    highlight_convs: dict[int, set[int]] = {}
    highlight_ids: dict[int, set[int]] = {}
    for rq_id, highlight_id, conversation_id in links:
        key = target_of.get(rq_id)
        if key is None:
            continue
        highlight_ids.setdefault(key, set()).add(highlight_id)
        if conversation_id in visible:
            highlight_convs.setdefault(key, set()).add(conversation_id)

    def projects(conversation_ids: set[int]) -> list[int]:
        return sorted({visible[c].project_id for c in conversation_ids if visible[c].project_id is not None})

    def one(rq: ResearchQuestion) -> dict:
        from_insights = insight_convs.get(rq.id, set())
        from_highlights = highlight_convs.get(rq.id, set())
        heard = projects(from_insights | from_highlights)
        return {
            "research_question_id": rq.id,
            "name": rq.name,
            "conversation_ids": sorted(from_insights | from_highlights),
            "conversation_count": len(from_insights | from_highlights),
            "insight_conversation_ids": sorted(from_insights),
            "highlight_only_conversation_ids": sorted(from_highlights - from_insights),
            "project_ids_heard": heard,
            "projects_heard_count": len(heard),
            "insight_count": insight_count.get(rq.id, 0),
            "highlight_count": len(highlight_ids.get(rq.id, set())),
        }

    unfiled = insight_convs.get(None, set())
    return {
        "opened_conversation_ids": sorted(c for c in opened if c in visible),
        "opened_conversation_count": sum(1 for c in opened if c in visible),
        "projects_in_scope": in_scope,
        "projects_in_scope_count": len(in_scope),
        "questions": [one(rq) for rq in questions],
        # Filed insights under no active question ("doesn't fit a question yet").
        "unfiled": {
            "insight_count": insight_count.get(None, 0),
            "conversation_ids": sorted(unfiled),
            "conversation_count": len(unfiled),
            "project_ids_heard": projects(unfiled),
            "projects_heard_count": len(projects(unfiled)),
        },
    }


# --- starting questions from the conversation guide ---------------------------------------


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def guide_ideas(prototype_db: Session, source, user_id: int, project_ids: list[int]) -> dict:
    """Distinct guide questions from the projects in scope that aren't already one of the
    user's research questions (compared case-insensitively, archived and merged included)."""
    source = as_source(source)
    conversations = scope_conversations(source, project_ids)
    count_by_project = Counter(c.project_id for c in conversations if c.project_id is not None)
    ordered = sorted(count_by_project, key=lambda pid: (-count_by_project[pid], pid))
    guides, not_loaded = source.guide_questions_for_projects(ordered, max_fetches=GUIDE_IDEAS_MAX_PROJECT_FETCHES)

    taken = {
        _norm(row.name)
        for row in prototype_db.query(ResearchQuestion.name).filter(ResearchQuestion.user_id == user_id)
    }
    ideas: dict[str, dict] = {}
    for project_id in ordered:
        for question in guides.get(project_id, []):
            key = _norm(question.text)
            if not key or key in taken:
                continue
            idea = ideas.setdefault(
                key,
                {
                    "text": " ".join(question.text.split()),
                    "shorthand_code": question.shorthand_code,
                    "conversation_count": 0,
                    "project_ids": [],
                },
            )
            if project_id not in idea["project_ids"]:
                idea["project_ids"].append(project_id)
                idea["conversation_count"] += count_by_project[project_id]
    ranked = sorted(ideas.values(), key=lambda i: -i["conversation_count"])  # stable: guide order within a tie
    return {"ideas": ranked[:GUIDE_IDEAS_LIMIT], "projects_not_loaded": not_loaded}
