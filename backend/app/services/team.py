"""The Team Page and the team codebook: what teammates made on the conversations the viewer
can read, shown read-only, plus the shared question groups and themes built on top.

THE ACCESS RULE LIVES HERE AND NOWHERE ELSE. Every team route builds a `TeamView` with
`load_view` and reads only through it.

- The viewer's scope (`team_scope`) is the conversations of their active workspace in
  `cortico_api` mode, limited to the projects their Cortico account can reach now
  (`cortico_access.accessible_project_ids`, re-checked per session every few minutes), and
  every conversation in `snapshot` mode. With no workspace nothing is in scope.
- A teammate's highlight or reflection is visible when its conversation is in scope. An
  insight is visible when its reflection's conversation is in scope and it is filed
  (`FILED_STATUSES`); suggested, rejected, retracted and merged insights never are.
- A teammate's question is visible only when something visible is filed under it (through
  question merges). The viewer's own open questions are always visible to the viewer.
- Evidence and highlight text is returned only for snippets whose own conversation is in
  scope, because the snippet and conversation caches are shared by every user and an
  insight merge can copy evidence across conversations.
- Conversation titles come from the scope, never from the shared cache.
- Anything else answers 404, like a row that does not exist. Ids sent by the client
  (questions, conversations, insights, groups, themes, users) are checked against the view.

Groups and themes belong to nobody, and their free text must never describe what a viewer
cannot see:

- A group is visible to whoever can see one of its questions, and they see only those
  questions and the insights under them. When any of its questions is hidden from the
  viewer, the name shown is the first visible question's and the rationale is null
  (`_group_dict`). A group's own name is stored only when someone typed one.
- A theme is visible only when every insight it holds, or ever held, is on a conversation
  in the viewer's scope (`_themes_in_scope`); one that never had a member is visible with
  its group.
- `created_by`, `updated_by` and `edited_by` name a user only when that user has a row the
  viewer can see (`TeamView.known_author`).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, active_workspace, workspace_project_ids
from ..config import Config, get_config
from ..cortico.source import ConversationRef, CorticoSource, SnippetRef, as_source
from ..llm import structured_json
from ..models_prototype import (
    Adoption,
    HighlightQuestion,
    ReadingState,
    ReflectionConnection,
    ResearchQuestion,
    RLCorticoSync,
    RLHighlight,
    RLSession,
    TeamAIRun,
    TeamQuestionGroup,
    TeamQuestionMember,
    TeamTheme,
    TeamThemeMember,
    TeamThemeRevision,
    UserName,
    VoiceMemo,
)
from ..timefmt import iso_utc
from . import activity
from .code_families import FILED_STATUSES
from .cortico_access import accessible_project_ids
from .questions import create_question
from .rq_merges import active_merge_edges, resolve_rq_id

# Reflections a teammate has finished writing or recording. One still transcribing, or
# whose transcription failed, has no text to show.
SHOWN_REFLECTION_STATUSES = ("ready_for_review", "filed")
VISIBLE_GROUP_STATUSES = ("active", "suggested")
LIVE_THEME_STATUSES = ("suggested", "accepted")

NO_QUESTION = "That question isn't on your team page."
NO_CONVERSATION = "That conversation isn't in your workspace."
NO_PERSON = "That person has nothing on your team page."
NO_GROUP = "That group of questions isn't on your team page."
NO_THEME = "That theme isn't on your team page."
NO_INSIGHT = "One of those insights isn't filed under this group's questions."

QUESTION_GROUP_PROMPT = """You help a research team notice when different people are asking \
the same research question in different words.

You are given the team's research questions that are not yet grouped: an id, the question, an \
optional description, and who asked it.

Rules:
- Propose a group only when its questions ask the same thing, so that insights filed under one \
would belong under the others. Questions that merely share a topic are not the same question.
- A group has two or more question ids. Every id must be one you were given; never invent one.
- A question may appear in at most one group.
- Give each group a short name worded as a question, and a one-sentence rationale saying what \
the questions have in common.
- It is fine, and common, to return an empty list.
"""

_GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "rationale": {"type": "string"},
                    "question_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "rationale", "question_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}

TEAM_THEME_PROMPT = """You help a research team build a shared codebook from insights that \
several people filed under the same research question.

You are given the question group, every filed insight under it (an id, the insight, who wrote \
it and which conversation it came from), the themes the team already has, and how many new \
themes you may propose.

Rules:
- A theme is a broad pattern that more than one insight supports, ideally from more than one \
person or conversation. Name it in a few plain words and define it in one or two sentences \
grounded in the insights you were given.
- Every value in connection_ids must be an insight id you were given; never invent one.
- Do not propose a theme the team already has, under the same or a near-identical name.
- Propose at most max_new_themes themes. It is fine to return fewer, or none.
"""

_THEME_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "connection_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "definition", "connection_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}


# --- scope ------------------------------------------------------------------------------


@dataclass
class TeamScope:
    viewer_id: int
    kind: str  # the data source: "snapshot" or "cortico_api"
    conversations: dict[int, ConversationRef]
    # False for a signed-in user with no workspace: nothing is visible, their own questions included.
    has_workspace: bool = True
    workspace_project_ids: list[int] = field(default_factory=list)

    def title(self, conversation_id: int) -> str | None:
        conversation = self.conversations.get(conversation_id)
        return conversation.title if conversation else None


def team_scope(prototype_db: Session, source: CorticoSource, user: CurrentUser) -> TeamScope:
    """The conversations the viewer may see teammates' work on. `scope_conversations` reads
    the cached workspace listing, so this never calls Cortico once per conversation."""
    source = as_source(source)
    workspace_projects = workspace_project_ids(active_workspace(prototype_db, user))
    if source.kind != "cortico_api":
        return TeamScope(
            viewer_id=user.id, kind=source.kind, workspace_project_ids=workspace_projects,
            conversations={c.id: c for c in source.scope_conversations(None)},
        )
    conversations: list[ConversationRef] = []
    if workspace_projects:
        # The workspace was checked against the user's Cortico roles when it was saved, and
        # roles change. A Cortico failure here fails the request; there is no unchecked fallback.
        reachable = accessible_project_ids(
            source.client,
            user.session.token_hash if user.session is not None else f"user:{user.id}",
            get_config().team.access_recheck_minutes,
        )
        allowed = [pid for pid in workspace_projects if pid in reachable]
        conversations = source.scope_conversations(allowed) if allowed else []
    return TeamScope(
        viewer_id=user.id, kind=source.kind, workspace_project_ids=workspace_projects,
        conversations={c.id: c for c in conversations}, has_workspace=bool(workspace_projects),
    )


def _where_in(query, column, ids, size: int = 500) -> list:
    """`query` filtered to `column IN ids`, in batches: a workspace can hold thousands of
    conversations, more than one statement's parameters allow."""
    ids = list(ids)
    rows: list = []
    for start in range(0, len(ids), size):
        rows.extend(query.filter(column.in_(ids[start:start + size])).all())
    return rows


def _in_scope(scope: TeamScope, query, conversation_column) -> list:
    """The rows of `query` made on a conversation in the viewer's scope. The scope filter
    for every row a team route returns."""
    return _where_in(query, conversation_column, scope.conversations)


def _active_group_of(prototype_db: Session, edges: dict[int, int]) -> dict[int, int]:
    """question id (resolved through merges) -> the active group that holds it. A question
    merge can leave one question in two active groups; the lowest group id wins everywhere."""
    out: dict[int, int] = {}
    members = (
        prototype_db.query(TeamQuestionMember.research_question_id, TeamQuestionMember.group_id)
        .join(TeamQuestionGroup, TeamQuestionGroup.id == TeamQuestionMember.group_id)
        .filter(TeamQuestionGroup.status == "active")
    )
    for rq_id, group_id in members:
        target = resolve_rq_id(prototype_db, rq_id, edges)
        out[target] = min(group_id, out.get(target, group_id))
    return out


# --- the view ---------------------------------------------------------------------------


@dataclass
class TeamView:
    """Everything a viewer may see of the team's work, loaded once per request."""

    db: Session
    source: CorticoSource
    scope: TeamScope
    edges: dict[int, int]
    memos: dict[int, VoiceMemo]  # every reflection in scope, whatever its status
    insights: list[ReflectionConnection]  # filed, in scope
    highlights: list[RLHighlight]
    questions: dict[int, ResearchQuestion]
    insights_by_question: dict[int, list[ReflectionConnection]]
    highlights_by_question: dict[int, list[RLHighlight]]
    question_ids_by_highlight: dict[int, list[int]]
    active_group_of: dict[int, int]
    adopted: set[int]  # questions the viewer already adopted
    people_ids: set[int]  # everyone with a row the viewer can see, and the viewer
    names: dict[int, str] = field(default_factory=dict)

    @property
    def viewer_id(self) -> int:
        return self.scope.viewer_id

    def resolve(self, rq_id: int) -> int:
        return resolve_rq_id(self.db, rq_id, self.edges)

    def author_of_insight(self, connection: ReflectionConnection) -> int:
        return self.memos[connection.voice_memo_id].user_id

    def prime_names(self, user_ids) -> None:
        wanted = {uid for uid in user_ids if uid is not None and uid not in self.names}
        if not wanted:
            return
        found: dict[int, str] = {}
        # Open sessions first, then the name kept from the last sign-in, which wins.
        sessions = _where_in(
            self.db.query(RLSession.user_id, RLSession.display_name, RLSession.created_at), RLSession.user_id, wanted
        )
        for row in sorted(sessions, key=lambda r: r.created_at):
            if row.display_name:
                found[row.user_id] = row.display_name
        for row in _where_in(self.db.query(UserName), UserName.user_id, wanted):
            found[row.user_id] = row.display_name
        for uid in wanted:
            self.names[uid] = found.get(uid) or f"Teammate {uid}"

    def name(self, user_id: int) -> str:
        """The user's name as anyone would read it: what the model is sent, so a payload
        does not change with who is asking."""
        self.prime_names([user_id])
        return self.names[user_id]

    def author(self, user_id: int) -> dict:
        """Who made a row. Never carries an email."""
        is_you = user_id == self.viewer_id
        # Snapshot mode has no sign-in, so the one implicit user has no name.
        name = "You" if is_you and self.scope.kind == "snapshot" else self.name(user_id)
        return {"user_id": user_id, "name": name, "is_you": is_you}

    def known_author(self, user_id: int | None) -> dict | None:
        """Who created or edited a group or theme, only when the viewer can see a row of
        theirs: otherwise the name would tell the viewer who works outside their scope."""
        return self.author(user_id) if user_id in self.people_ids else None


def load_view(prototype_db: Session, source: CorticoSource, user: CurrentUser) -> TeamView:
    source = as_source(source)
    scope = team_scope(prototype_db, source, user)
    edges = active_merge_edges(prototype_db)

    memos = {m.id: m for m in _in_scope(scope, prototype_db.query(VoiceMemo), VoiceMemo.conversation_id)}
    insights = _where_in(
        prototype_db.query(ReflectionConnection).filter(ReflectionConnection.status.in_(FILED_STATUSES)),
        ReflectionConnection.voice_memo_id, memos,
    )
    insights.sort(key=lambda c: (c.created_at, c.id))
    highlights = _in_scope(scope, prototype_db.query(RLHighlight), RLHighlight.conversation_id)
    highlights.sort(key=lambda h: (h.created_at, h.id))
    highlight_by_id = {h.id: h for h in highlights}
    links = _where_in(prototype_db.query(HighlightQuestion), HighlightQuestion.highlight_id, highlight_by_id)
    links.sort(key=lambda link: link.id)

    def resolve(rq_id: int) -> int:
        return resolve_rq_id(prototype_db, rq_id, edges)

    referenced = {resolve(c.research_question_id) for c in insights if c.research_question_id is not None}
    referenced |= {resolve(link.research_question_id) for link in links}
    open_questions = prototype_db.query(ResearchQuestion).filter(ResearchQuestion.archived.is_(False))
    candidates = {q.id: q for q in _where_in(open_questions, ResearchQuestion.id, referenced)}
    if scope.has_workspace:
        candidates.update((q.id, q) for q in open_questions.filter(ResearchQuestion.user_id == user.id))
    candidates = {qid: q for qid, q in candidates.items() if qid not in edges}  # a merged one reads under its target

    # An insight or highlight counts under a question only when the same person owns both.
    insights_by_question: dict[int, list[ReflectionConnection]] = {}
    for c in insights:
        if c.research_question_id is None:
            continue
        question = candidates.get(resolve(c.research_question_id))
        if question is not None and question.user_id == memos[c.voice_memo_id].user_id:
            insights_by_question.setdefault(question.id, []).append(c)
    highlights_by_question: dict[int, list[RLHighlight]] = {}
    question_ids_by_highlight: dict[int, list[int]] = {}
    for link in links:
        highlight = highlight_by_id[link.highlight_id]
        question = candidates.get(resolve(link.research_question_id))
        if question is None or question.user_id != highlight.user_id:
            continue
        if question.id not in question_ids_by_highlight.setdefault(highlight.id, []):
            question_ids_by_highlight[highlight.id].append(question.id)
            highlights_by_question.setdefault(question.id, []).append(highlight)

    # A teammate's question needs something visible filed under it. The viewer's own do not.
    questions = {
        qid: q
        for qid, q in sorted(candidates.items())
        if qid in insights_by_question or qid in highlights_by_question or q.user_id == user.id
    }
    adopted = {
        row.source_id
        for row in prototype_db.query(Adoption.source_id).filter(
            Adoption.user_id == user.id, Adoption.kind == "question"
        )
    }
    people_ids = {user.id} | {h.user_id for h in highlights} | {memos[c.voice_memo_id].user_id for c in insights}
    people_ids |= {m.user_id for m in memos.values() if m.status in SHOWN_REFLECTION_STATUSES}

    view = TeamView(
        db=prototype_db, source=source, scope=scope, edges=edges, memos=memos, insights=insights,
        highlights=highlights, questions=questions, insights_by_question=insights_by_question,
        highlights_by_question=highlights_by_question, question_ids_by_highlight=question_ids_by_highlight,
        active_group_of=_active_group_of(prototype_db, edges), adopted=adopted, people_ids=people_ids,
    )
    view.prime_names(people_ids)
    return view


# --- what is visible ----------------------------------------------------------------------


def visible_question(view: TeamView, rq_id: int) -> ResearchQuestion:
    question = view.questions.get(rq_id)
    if question is None:
        raise HTTPException(404, NO_QUESTION)
    return question


def visible_conversation(view: TeamView, conversation_id: int) -> ConversationRef:
    conversation = view.scope.conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(404, NO_CONVERSATION)
    return conversation


def visible_reflections(view: TeamView) -> list[VoiceMemo]:
    return [m for m in view.memos.values() if m.status in SHOWN_REFLECTION_STATUSES]


def _scoped_snippets(view: TeamView, snippet_ids) -> dict[int, SnippetRef]:
    """Only the snippets whose own conversation is in scope. `snippets_by_ids` reads a cache
    shared by every user and is not an access check."""
    return {
        sid: snippet
        for sid, snippet in view.source.snippets_by_ids(set(snippet_ids)).items()
        if snippet.conversation_id in view.scope.conversations
    }


def _span_text(snippet: SnippetRef, start: int, end: int) -> str:
    return " ".join(w["word"] for w in snippet.words[start:end])


def _all_member_ids(view: TeamView, group: TeamQuestionGroup) -> list[int]:
    """Every question of the group, seen by the viewer or not, each resolved through
    question merges. An active group lists a question only while it is the one that holds it."""
    ids = dict.fromkeys(view.resolve(m.research_question_id) for m in sorted(group.members, key=lambda m: m.id))
    if group.status == "active":
        return [qid for qid in ids if view.active_group_of.get(qid) == group.id]
    return list(ids)


def _group_member_ids(view: TeamView, group: TeamQuestionGroup) -> list[int]:
    """The group's questions the viewer can see."""
    return [qid for qid in _all_member_ids(view, group) if qid in view.questions]


def visible_groups(view: TeamView, statuses=VISIBLE_GROUP_STATUSES) -> list[TeamQuestionGroup]:
    groups = (
        view.db.query(TeamQuestionGroup)
        .filter(TeamQuestionGroup.status.in_(statuses))
        .order_by(TeamQuestionGroup.id)
        .all()
    )
    return [g for g in groups if _group_member_ids(view, g)]


def visible_group(view: TeamView, group_id: int, statuses=VISIBLE_GROUP_STATUSES) -> TeamQuestionGroup:
    group = view.db.get(TeamQuestionGroup, group_id)
    if group is None or group.status not in statuses or not _group_member_ids(view, group):
        raise HTTPException(404, NO_GROUP)
    return group


def group_insights(view: TeamView, group: TeamQuestionGroup) -> list[ReflectionConnection]:
    """Every visible filed insight under the group's visible questions."""
    out = [c for qid in _group_member_ids(view, group) for c in view.insights_by_question.get(qid, [])]
    return sorted(out, key=lambda c: (c.created_at, c.id))


def _connections_in_scope(view: TeamView, connection_ids) -> set[int]:
    """Those insights, whatever their status, whose reflection is on a conversation in the
    viewer's scope."""
    rows = _where_in(
        view.db.query(ReflectionConnection.id, ReflectionConnection.voice_memo_id),
        ReflectionConnection.id, set(connection_ids),
    )
    return {row.id for row in rows if row.voice_memo_id in view.memos}


def _themes_in_scope(view: TeamView, themes: list[TeamTheme]) -> list[TeamTheme]:
    """The themes whose every member, now and at every earlier revision, is in the viewer's
    scope. A theme's name and definition describe its members, and keep describing one after
    it is taken out, so a theme that ever held a hidden insight stays hidden from that viewer.
    A member that is in scope but no longer filed does not hide it: it is only left out of
    the member list."""
    ever: dict[int, set[int]] = {t.id: {m.connection_id for m in t.members} for t in themes}
    revisions = _where_in(
        view.db.query(TeamThemeRevision.theme_id, TeamThemeRevision.member_ids_json), TeamThemeRevision.theme_id, ever
    )
    for theme_id, member_ids_json in revisions:
        ever[theme_id].update(json.loads(member_ids_json))
    in_scope = _connections_in_scope(view, {cid for ids in ever.values() for cid in ids})
    return [t for t in themes if ever[t.id] <= in_scope]


def visible_themes(
    view: TeamView, group: TeamQuestionGroup, statuses=LIVE_THEME_STATUSES
) -> list[TeamTheme]:
    themes = (
        view.db.query(TeamTheme)
        .filter(TeamTheme.group_id == group.id, TeamTheme.status.in_(statuses))
        .order_by(TeamTheme.id)
        .all()
    )
    return _themes_in_scope(view, themes)


def visible_theme(view: TeamView, theme_id: int) -> tuple[TeamTheme, TeamQuestionGroup, set[int]]:
    """The theme, its group and the ids of the group's insights the viewer can see."""
    theme = view.db.get(TeamTheme, theme_id)
    if theme is None:
        raise HTTPException(404, NO_THEME)
    group = view.db.get(TeamQuestionGroup, theme.group_id)
    if group is None or group.status not in VISIBLE_GROUP_STATUSES or not _group_member_ids(view, group):
        raise HTTPException(404, NO_THEME)
    if not _themes_in_scope(view, [theme]):
        raise HTTPException(404, NO_THEME)
    return theme, group, {c.id for c in group_insights(view, group)}


# --- serializers --------------------------------------------------------------------------


def _question_dict(view: TeamView, question: ResearchQuestion) -> dict:
    insights = view.insights_by_question.get(question.id, [])
    highlights = view.highlights_by_question.get(question.id, [])
    conversations = {view.memos[c.voice_memo_id].conversation_id for c in insights}
    conversations |= {h.conversation_id for h in highlights}
    return {
        "research_question_id": question.id,
        "name": question.name,
        "description": question.description,
        "author": view.author(question.user_id),
        "insight_count": len(insights),
        "highlight_count": len(highlights),
        "conversation_count": len(conversations),
        "group_id": view.active_group_of.get(question.id),
        "adopted_by_you": question.id in view.adopted,
    }


def _insight_dicts(view: TeamView, insights: list[ReflectionConnection]) -> list[dict]:
    snippets = _scoped_snippets(view, {e.snippet_id for c in insights for e in c.evidence})

    def one(c: ReflectionConnection) -> dict:
        memo = view.memos[c.voice_memo_id]
        return {
            "connection_id": c.id,
            "insight_text": c.insight_text,
            "author": view.author(memo.user_id),
            "conversation_id": memo.conversation_id,
            "conversation_title": view.scope.title(memo.conversation_id),
            "voice_memo_id": memo.id,
            # reflection_connection has no column for when it was filed; this is its last change.
            "filed_at": iso_utc(c.updated_at),
            "evidence": [
                {
                    "snippet_id": e.snippet_id,
                    "conversation_id": snippets[e.snippet_id].conversation_id,
                    "conversation_title": view.scope.title(snippets[e.snippet_id].conversation_id),
                    "text": _span_text(snippets[e.snippet_id], e.start_index, e.end_index),
                    "start_index": e.start_index,
                    "end_index": e.end_index,
                }
                for e in sorted(c.evidence, key=lambda e: e.id)
                if e.snippet_id in snippets
            ],
        }

    return [one(c) for c in insights]


def _question_highlight_dicts(view: TeamView, highlights: list[RLHighlight]) -> list[dict]:
    snippets = _scoped_snippets(view, {s.snippet_id for h in highlights for s in h.snippets})

    def one(h: RLHighlight) -> dict:
        texts = [
            _span_text(snippets[s.snippet_id], s.start_index, s.end_index)
            for s in h.snippets
            if s.snippet_id in snippets
        ]
        return {
            "highlight_id": h.id,
            "author": view.author(h.user_id),
            "conversation_id": h.conversation_id,
            "conversation_title": view.scope.title(h.conversation_id),
            "text": " ".join(t for t in texts if t),
            "notes": h.notes,
            "starred": bool(h.starred),
        }

    return [one(h) for h in highlights]


def _reflection_text(memo: VoiceMemo) -> str:
    if memo.transcription_edited:
        return memo.transcription_edited
    try:
        return " ".join(w["word"] for w in json.loads(memo.transcription_raw or "[]"))
    except (ValueError, TypeError, KeyError):
        return ""


def _group_dict(view: TeamView, group: TeamQuestionGroup) -> dict:
    """A group's own name and rationale were written about all of its questions, so a
    viewer who cannot see every one of them gets the first visible question's name and no
    rationale. A group nobody named shows that same question's name."""
    seen = _group_member_ids(view, group)
    whole = len(seen) == len(_all_member_ids(view, group))
    fallback = view.questions[seen[0]].name if seen else ""
    return {
        "id": group.id,
        "name": (group.name if whole else "") or fallback,
        "status": group.status,
        "rationale": group.rationale if whole else None,
        "created_by": view.known_author(group.created_by),
        "question_ids": seen,
        "theme_count": len(visible_themes(view, group)),
    }


def _theme_dict(view: TeamView, theme: TeamTheme, visible_insight_ids: set[int]) -> dict:
    return {
        "id": theme.id,
        "name": theme.name,
        "definition": theme.definition,
        "status": theme.status,
        "created_by": view.known_author(theme.created_by),
        "updated_by": view.known_author(theme.updated_by),
        "updated_at": iso_utc(theme.updated_at),
        "member_connection_ids": sorted(
            m.connection_id for m in theme.members if m.connection_id in visible_insight_ids
        ),
    }


# --- reads --------------------------------------------------------------------------------


def _people(view: TeamView) -> list[dict]:
    """Everyone with at least one visible row, and the viewer, who comes first."""
    highlights = Counter(h.user_id for h in view.highlights)
    insights = Counter(view.author_of_insight(c) for c in view.insights)
    questions = Counter(q.user_id for q in view.questions.values())
    reflections = Counter(m.user_id for m in visible_reflections(view))
    opened = Counter(
        row.user_id
        for row in _in_scope(view.scope, view.db.query(ReadingState.user_id), ReadingState.conversation_id)
    )
    people = [
        {
            **view.author(uid),
            "conversations_opened": opened.get(uid, 0),
            "highlight_count": highlights.get(uid, 0),
            "insight_count": insights.get(uid, 0),
            "question_count": questions.get(uid, 0),
            "reflection_count": reflections.get(uid, 0),
        }
        for uid in view.people_ids
    ]
    return sorted(people, key=lambda p: (not p["is_you"], p["name"].casefold(), p["user_id"]))


def overview(view: TeamView) -> dict:
    return {
        "people": _people(view),
        "questions": [_question_dict(view, q) for q in view.questions.values()],
        "groups": [_group_dict(view, g) for g in visible_groups(view)],
        "conversation_count": len(view.scope.conversations),
    }


def question_detail(view: TeamView, rq_id: int) -> dict:
    question = visible_question(view, rq_id)
    return {
        "question": _question_dict(view, question),
        "insights": _insight_dicts(view, view.insights_by_question.get(question.id, [])),
        "highlights": _question_highlight_dicts(view, view.highlights_by_question.get(question.id, [])),
    }


def person_detail(view: TeamView, user_id: int) -> dict:
    person = next((p for p in _people(view) if p["user_id"] == user_id), None)
    if person is None:
        raise HTTPException(404, NO_PERSON)
    reflections = sorted(
        (m for m in visible_reflections(view) if m.user_id == user_id),
        key=lambda m: (m.recorded_at, m.id), reverse=True,
    )
    return {
        "person": person,
        "reflections": [
            {
                "voice_memo_id": m.id,
                "conversation_id": m.conversation_id,
                "conversation_title": view.scope.title(m.conversation_id),
                "recorded_at": iso_utc(m.recorded_at),
                "typed": m.audio_path == "",
                "text": _reflection_text(m),
            }
            for m in reflections
        ],
        "questions": [_question_dict(view, q) for q in view.questions.values() if q.user_id == user_id],
    }


def conversation_highlights(view: TeamView, conversation_id: int) -> list[dict]:
    """Other users' highlights on one conversation, never the viewer's own."""
    visible_conversation(view, conversation_id)
    highlights = [
        h for h in view.highlights if h.conversation_id == conversation_id and h.user_id != view.viewer_id
    ]
    snippets = _scoped_snippets(view, {s.snippet_id for h in highlights for s in h.snippets})
    cortico_ids = {
        row.local_id: row.cortico_id
        for row in _where_in(
            view.db.query(RLCorticoSync).filter(
                RLCorticoSync.local_kind == "highlight", RLCorticoSync.status == "synced"
            ),
            RLCorticoSync.local_id, [h.id for h in highlights],
        )
    }
    return [
        {
            "highlight_id": h.id,
            "author": view.author(h.user_id),
            "notes": h.notes,
            "starred": bool(h.starred),
            "created_at": iso_utc(h.created_at),
            "question_names": [view.questions[qid].name for qid in view.question_ids_by_highlight.get(h.id, [])],
            "snippets": [
                {"snippet_id": s.snippet_id, "start_index": s.start_index, "end_index": s.end_index}
                for s in h.snippets
                if s.snippet_id in snippets and snippets[s.snippet_id].conversation_id == conversation_id
            ],
            "cortico_id": cortico_ids.get(h.id),
        }
        for h in highlights
    ]


def group_detail(view: TeamView, group_id: int, include_dismissed: bool = False) -> dict:
    group = visible_group(view, group_id)
    insights = group_insights(view, group)
    insight_ids = {c.id for c in insights}
    statuses = LIVE_THEME_STATUSES + (("dismissed",) if include_dismissed else ())
    return {
        "group": _group_dict(view, group),
        "questions": [_question_dict(view, view.questions[qid]) for qid in _group_member_ids(view, group)],
        "insights": _insight_dicts(view, insights),
        "themes": [_theme_dict(view, t, insight_ids) for t in visible_themes(view, group, statuses)],
    }


def theme_history(view: TeamView, theme_id: int) -> list[dict]:
    """The theme before each edit, newest first. Every revision's members are in the
    viewer's scope, or `visible_theme` would have hidden the theme."""
    theme, _group, insight_ids = visible_theme(view, theme_id)
    revisions = (
        view.db.query(TeamThemeRevision)
        .filter(TeamThemeRevision.theme_id == theme.id)
        .order_by(TeamThemeRevision.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "definition": r.definition,
            "member_connection_ids": [cid for cid in json.loads(r.member_ids_json) if cid in insight_ids],
            "edited_by": view.known_author(r.edited_by),
            "created_at": iso_utc(r.created_at),
        }
        for r in revisions
    ]


# --- adopting a question --------------------------------------------------------------------


def adopt_question(view: TeamView, user: CurrentUser, rq_id: int) -> ResearchQuestion:
    """Copies a teammate's question into the viewer's own, through the same code that
    POST /research_questions runs."""
    question = visible_question(view, rq_id)
    if question.user_id == user.id:
        raise HTTPException(409, "This is already one of your questions.")
    if question.id in view.adopted:
        raise HTTPException(409, "You already added this question to yours.")
    new = create_question(view.db, user.id, view.scope.workspace_project_ids, question.name, question.description)
    view.db.add(
        Adoption(user_id=user.id, kind="question", source_id=question.id, source_user_id=question.user_id, new_id=new.id)
    )
    activity.log_activity(
        view.db, user.id, "user", activity.QUESTION_ADOPTED,
        f"You added a teammate’s question to yours: “{activity.excerpt(new.name)}”",
        research_question_id=new.id, source_rq_id=question.id, source_user_id=question.user_id,
    )
    view.db.commit()
    view.db.refresh(new)
    return new


# --- question groups ------------------------------------------------------------------------


def _checked_question_ids(view: TeamView, rq_ids: list[int]) -> list[int]:
    return [visible_question(view, rq_id).id for rq_id in dict.fromkeys(rq_ids)]


def _claim_for_active_group(view: TeamView, rq_ids, group_id: int | None = None) -> None:
    """Refuses (409) when one of `rq_ids` is in an active group other than `group_id`.
    Called right before the insert, in its transaction: it locks the question rows (on
    Postgres; SQLite has one writer anyway) and reads the memberships again, so two requests
    cannot both put one question in a group. The message names a question only if the viewer
    can see it."""
    rq_ids = list(rq_ids)
    view.db.query(ResearchQuestion.id).filter(ResearchQuestion.id.in_(rq_ids)).with_for_update().all()
    view.active_group_of = _active_group_of(view.db, view.edges)
    for qid in rq_ids:
        if view.active_group_of.get(qid) not in (None, group_id):
            if qid in view.questions:
                raise HTTPException(409, f"“{activity.excerpt(view.questions[qid].name, 80)}” is already in another group.")
            raise HTTPException(409, "One of these questions is already in another group.")


def _saved_group(view: TeamView, group: TeamQuestionGroup) -> dict:
    view.db.commit()
    view.db.refresh(group)
    view.active_group_of = _active_group_of(view.db, view.edges)
    return _group_dict(view, group)


def create_group(view: TeamView, user: CurrentUser, name: str | None, rq_ids: list[int]) -> dict:
    ids = _checked_question_ids(view, rq_ids)
    _claim_for_active_group(view, ids)
    # No name typed: none is stored, and each viewer is shown a question of the group they can see.
    group = TeamQuestionGroup(name=(name or "").strip(), status="active", created_by=user.id)
    view.db.add(group)
    view.db.flush()
    for qid in ids:
        view.db.add(TeamQuestionMember(group_id=group.id, research_question_id=qid, added_by=user.id))
    shown = group.name or view.questions[ids[0]].name
    activity.log_activity(
        view.db, user.id, "user", activity.TEAM_GROUP_CREATED,
        f"You grouped {activity.plural(len(ids), 'question')} as “{activity.excerpt(shown, 60)}”.",
        team_group_id=group.id, research_question_ids=ids,
    )
    return _saved_group(view, group)


def rename_group(view: TeamView, user: CurrentUser, group_id: int, name: str | None) -> dict:
    group = visible_group(view, group_id)
    if name is None:
        return _group_dict(view, group)
    if not name.strip():
        raise HTTPException(422, "Give the group a name.")
    if len(_group_member_ids(view, group)) != len(_all_member_ids(view, group)):
        # The viewer is shown a question's name in place of the group's, and would be
        # overwriting a name they cannot read.
        raise HTTPException(409, "This group reaches beyond your workspace, so its name can't be changed from here.")
    if name.strip() != group.name:
        group.name = name.strip()
        activity.log_activity(
            view.db, user.id, "user", activity.TEAM_GROUP_RENAMED,
            f"You renamed a group of questions to “{activity.excerpt(group.name, 60)}”.", team_group_id=group.id,
        )
    return _saved_group(view, group)


def _refuse_suggested(group: TeamQuestionGroup) -> None:
    if group.status != "active":
        raise HTTPException(409, "The AI suggested this group. Group these or keep them separate before changing it.")


def add_group_question(view: TeamView, user: CurrentUser, group_id: int, rq_id: int) -> dict:
    group = visible_group(view, group_id)
    question = visible_question(view, rq_id)
    _refuse_suggested(group)
    _claim_for_active_group(view, [question.id], group.id)
    if view.active_group_of.get(question.id) != group.id:
        view.db.add(TeamQuestionMember(group_id=group.id, research_question_id=question.id, added_by=user.id))
    return _saved_group(view, group)


def remove_group_question(view: TeamView, group_id: int, rq_id: int) -> dict:
    """A group left with no question is seen by nobody; its themes are kept."""
    group = visible_group(view, group_id)
    question = visible_question(view, rq_id)
    rows = [m for m in group.members if view.resolve(m.research_question_id) == question.id]
    if not rows or question.id not in _group_member_ids(view, group):
        raise HTTPException(404, NO_QUESTION)
    _refuse_suggested(group)
    for row in rows:
        view.db.delete(row)
    return _saved_group(view, group)


def accept_group(view: TeamView, user: CurrentUser, group_id: int) -> dict:
    group = visible_group(view, group_id, statuses=("active", "suggested", "dismissed"))
    if group.status == "dismissed":
        raise HTTPException(409, "These questions were kept separate.")
    if group.status == "active":
        return _group_dict(view, group)
    # Every member counts, seen by the viewer or not.
    member_ids = _all_member_ids(view, group)
    live = view.db.query(ResearchQuestion.id).filter(
        ResearchQuestion.id.in_(member_ids), ResearchQuestion.archived.is_(False)
    ).count()
    if live < 2:
        raise HTTPException(409, "Fewer than two of these questions are still open, so there is nothing to group.")
    _claim_for_active_group(view, member_ids, group.id)
    group.status = "active"
    activity.log_activity(
        view.db, user.id, "user", activity.TEAM_GROUP_CREATED,
        "You grouped questions the AI matched.", team_group_id=group.id,
        research_question_ids=_group_member_ids(view, group),
    )
    return _saved_group(view, group)


def dismiss_group(view: TeamView, user: CurrentUser, group_id: int) -> dict:
    group = visible_group(view, group_id, statuses=("active", "suggested", "dismissed"))
    if group.status == "active":
        raise HTTPException(409, "This group is already in use. Take its questions out one at a time.")
    if group.status == "suggested":
        group.status = "dismissed"
        activity.log_activity(
            view.db, user.id, "user", activity.TEAM_GROUP_DISMISSED,
            "You kept questions the AI matched separate.", team_group_id=group.id,
        )
    return _saved_group(view, group)


# --- the two paid calls -----------------------------------------------------------------------


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _claim_run(prototype_db: Session, kind: str, group_id: int | None, user_id: int, payload: dict) -> TeamAIRun | None:
    """None when the newest run of this kind (and group) was sent exactly this and did not
    fail: asking again would pay for the same answer. Otherwise the run row, committed before
    the model is called, so a second request arriving meanwhile sees it."""
    fingerprint = _fingerprint(payload)
    query = prototype_db.query(TeamAIRun).filter(TeamAIRun.kind == kind)
    if group_id is not None:
        query = query.filter(TeamAIRun.group_id == group_id)
    newest = query.order_by(TeamAIRun.id.desc()).first()
    if newest is not None and newest.fingerprint == fingerprint and newest.status != "failed":
        return None
    run = TeamAIRun(kind=kind, group_id=group_id, user_id=user_id, fingerprint=fingerprint, status="running")
    prototype_db.add(run)
    prototype_db.commit()
    return run


def _ask(prototype_db: Session, run: TeamAIRun, **request):
    try:
        return structured_json(**request)
    except Exception:  # AIProviderError, and anything else: a run left "running" would block every retry
        prototype_db.rollback()
        run.status = "failed"
        prototype_db.commit()
        raise


def _apply_group_suggestions(
    prototype_db: Session, *, raw_groups: list[dict], valid_question_ids: set[int], ai_metadata: dict
) -> list[TeamQuestionGroup]:
    """Saves the model's groups as `suggested` rows. An id outside `valid_question_ids` (the
    questions it was sent) is dropped, and so is a question that would sit with one it
    already shares a suggested or dismissed group with, so "keep separate" sticks. A group
    left with fewer than two questions, or with no name, is not saved."""
    decided: set[frozenset[int]] = set()
    earlier = prototype_db.query(TeamQuestionGroup).filter(TeamQuestionGroup.status.in_(("suggested", "dismissed")))
    for group in earlier:
        ids = sorted({m.research_question_id for m in group.members})
        decided |= {frozenset((a, b)) for i, a in enumerate(ids) for b in ids[i + 1:]}

    created: list[TeamQuestionGroup] = []
    for raw in raw_groups:
        name = (raw.get("name") or "").strip()
        kept: list[int] = []
        for qid in dict.fromkeys(raw.get("question_ids", [])):
            if qid in valid_question_ids and all(frozenset((qid, other)) not in decided for other in kept):
                kept.append(qid)
        if len(kept) < 2 or not name:
            continue
        group = TeamQuestionGroup(
            name=name, status="suggested", created_by=None, rationale=raw.get("rationale") or None,
            ai_metadata=json.dumps(ai_metadata),
        )
        prototype_db.add(group)
        prototype_db.flush()
        for qid in kept:
            prototype_db.add(TeamQuestionMember(group_id=group.id, research_question_id=qid, added_by=None))
        decided |= {frozenset((a, b)) for i, a in enumerate(kept) for b in kept[i + 1:]}
        created.append(group)
    prototype_db.commit()
    return created


def suggest_groups(view: TeamView, user: CurrentUser, config: Config) -> list[dict]:
    """One paid call: which of the visible questions that are in no group, active or
    suggested, ask the same thing. No call when there are fewer than two, or when the newest
    run was sent the same questions."""
    waiting = {
        view.resolve(row.research_question_id)
        for row in view.db.query(TeamQuestionMember.research_question_id)
        .join(TeamQuestionGroup, TeamQuestionGroup.id == TeamQuestionMember.group_id)
        .filter(TeamQuestionGroup.status == "suggested")
    }
    free = [q for q in view.questions.values() if q.id not in view.active_group_of and q.id not in waiting]
    if len(free) < 2:
        return []
    payload = {
        "questions": [
            {"id": q.id, "question": q.name, "description": q.description, "asked_by": view.name(q.user_id)}
            for q in free
        ]
    }
    run = _claim_run(view.db, "groups", None, user.id, payload)
    if run is None:
        return []
    answer = _ask(
        view.db, run,
        system=QUESTION_GROUP_PROMPT, user=json.dumps(payload), schema=_GROUP_SCHEMA, name="team_question_groups",
        model=config.team.model, effort=config.team.reasoning_effort,
    )
    created = _apply_group_suggestions(
        view.db,
        raw_groups=answer.data.get("groups", []),
        valid_question_ids={q.id for q in free},
        ai_metadata={
            "model": answer.model, "reasoning_effort": answer.reasoning_effort, "prompt": QUESTION_GROUP_PROMPT,
            "requested_by": user.id,
        },
    )
    run.status = "done"
    activity.log_activity(
        view.db, user.id, "ai", activity.TEAM_GROUPS_SUGGESTED,
        f"The AI matched {activity.plural(len(created), 'group')} of questions that ask the same thing.",
        count=len(created), team_group_ids=[g.id for g in created],
    )
    view.db.commit()
    return [_group_dict(view, g) for g in created]


# --- team themes ----------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _write_theme_revision(prototype_db: Session, theme: TeamTheme, user_id: int) -> None:
    """Snapshot the theme before a user edit changes it."""
    prototype_db.add(
        TeamThemeRevision(
            theme_id=theme.id, name=theme.name, definition=theme.definition,
            member_ids_json=json.dumps(sorted(m.connection_id for m in theme.members)), edited_by=user_id,
        )
    )


def _apply_theme_results(
    prototype_db: Session,
    *,
    group_id: int,
    raw_themes: list[dict],
    valid_connection_ids: set[int],
    existing_names: set[str],
    ai_metadata: dict,
    max_new: int,
) -> list[TeamTheme]:
    """Saves the model's themes as `suggested` rows. Connection ids outside
    `valid_connection_ids` (the insights it was sent) are dropped, then a theme with no
    member left, one whose normalised name is in `existing_names` or repeats in this run,
    and anything past `max_new`."""
    taken = {_norm(name) for name in existing_names}
    created: list[TeamTheme] = []
    for raw in raw_themes:
        if len(created) >= max_new:
            break  # enforced here too, not just asked of the model
        name = (raw.get("name") or "").strip()
        definition = (raw.get("definition") or "").strip()
        member_ids = [cid for cid in dict.fromkeys(raw.get("connection_ids", [])) if cid in valid_connection_ids]
        if not member_ids or not name or not definition or _norm(name) in taken:
            continue
        taken.add(_norm(name))
        theme = TeamTheme(
            group_id=group_id, name=name, definition=definition, status="suggested",
            ai_metadata=json.dumps(ai_metadata), created_by=None,
        )
        prototype_db.add(theme)
        prototype_db.flush()
        for cid in member_ids:
            prototype_db.add(TeamThemeMember(theme_id=theme.id, connection_id=cid))
        created.append(theme)
    prototype_db.commit()
    return created


def synthesize_themes(view: TeamView, user: CurrentUser, group_id: int, config: Config) -> list[dict]:
    """One paid call: themes across the group's filed insights that the viewer can see. No
    call when nothing is filed, when the group already has `max_suggested_themes` undecided
    suggestions, or when the newest run for the group was sent the same thing."""
    group = visible_group(view, group_id)
    insights = group_insights(view, group)
    every_theme = view.db.query(TeamTheme).filter(TeamTheme.group_id == group.id).all()
    room = config.team.max_suggested_themes - sum(1 for t in every_theme if t.status == "suggested")
    if not insights or room <= 0:
        return []
    max_new = min(config.team.max_new_themes_per_run, room)
    insight_ids = {c.id for c in insights}
    payload = {
        # Only what this viewer can see: neither the group's own name nor a hidden theme.
        "questions": [view.questions[qid].name for qid in _group_member_ids(view, group)],
        "insights": [
            {
                "connection_id": c.id,
                "insight": c.insight_text,
                "written_by": view.name(view.author_of_insight(c)),
                "conversation": view.scope.title(view.memos[c.voice_memo_id].conversation_id),
            }
            for c in insights
        ],
        "existing_themes": [
            {
                "name": t.name,
                "definition": t.definition,
                "connection_ids": sorted(m.connection_id for m in t.members if m.connection_id in insight_ids),
            }
            for t in visible_themes(view, group)
        ],
        "max_new_themes": max_new,
    }
    run = _claim_run(view.db, "themes", group.id, user.id, payload)
    if run is None:
        return []
    answer = _ask(view.db, run, system=TEAM_THEME_PROMPT, user=json.dumps(payload), schema=_THEME_SCHEMA, name="team_themes")
    created = _apply_theme_results(
        view.db,
        group_id=group.id,
        raw_themes=answer.data.get("themes", []),
        valid_connection_ids=insight_ids,
        # Every theme of the group, dismissed ones too, so "not this" sticks.
        existing_names={t.name for t in every_theme},
        ai_metadata={
            "model": answer.model, "reasoning_effort": answer.reasoning_effort, "prompt": TEAM_THEME_PROMPT,
            "requested_by": user.id,
        },
        max_new=max_new,
    )
    run.status = "done"
    activity.log_activity(
        view.db, user.id, "ai", activity.TEAM_THEMES_SUGGESTED,
        f"The AI suggested {activity.plural(len(created), 'team theme')}.",
        team_group_id=group.id, count=len(created), team_theme_ids=[t.id for t in created],
    )
    view.db.commit()
    return [_theme_dict(view, t, insight_ids) for t in created]


def _checked_connection_ids(connection_ids: list[int], visible_insight_ids: set[int]) -> list[int]:
    ids = list(dict.fromkeys(connection_ids))
    if any(cid not in visible_insight_ids for cid in ids):
        raise HTTPException(422, NO_INSIGHT)
    return ids


def _log_theme_edit(view: TeamView, user: CurrentUser, theme: TeamTheme, group: TeamQuestionGroup, summary: str) -> None:
    theme.updated_by = user.id
    activity.log_activity(
        view.db, user.id, "user", activity.TEAM_THEME_EDITED, summary, team_theme_id=theme.id, team_group_id=group.id
    )


def _saved_theme(view: TeamView, theme: TeamTheme, insight_ids: set[int]) -> dict:
    view.db.commit()
    view.db.refresh(theme)
    return _theme_dict(view, theme, insight_ids)


def create_theme(
    view: TeamView, user: CurrentUser, group_id: int, name: str, definition: str, connection_ids: list[int]
) -> dict:
    group = visible_group(view, group_id)
    insight_ids = {c.id for c in group_insights(view, group)}
    member_ids = _checked_connection_ids(connection_ids, insight_ids)
    if not name.strip():
        raise HTTPException(422, "Give the theme a name.")
    theme = TeamTheme(
        group_id=group.id, name=name.strip(), definition=definition, status="accepted",
        created_by=user.id, updated_by=user.id,
    )
    view.db.add(theme)
    view.db.flush()
    for cid in member_ids:
        view.db.add(TeamThemeMember(theme_id=theme.id, connection_id=cid))
    activity.log_activity(
        view.db, user.id, "user", activity.TEAM_THEME_ACCEPTED,
        f"You added a team theme: “{activity.excerpt(theme.name, 60)}”", team_theme_id=theme.id,
        team_group_id=group.id,
    )
    return _saved_theme(view, theme, insight_ids)


def update_theme(view: TeamView, user: CurrentUser, theme_id: int, name: str | None, definition: str | None) -> dict:
    theme, group, insight_ids = visible_theme(view, theme_id)
    if name is not None and not name.strip():
        raise HTTPException(422, "Give the theme a name.")
    if name is None and definition is None:
        return _theme_dict(view, theme, insight_ids)
    _write_theme_revision(view.db, theme, user.id)
    if name is not None:
        theme.name = name.strip()
    if definition is not None:
        theme.definition = definition
    _log_theme_edit(view, user, theme, group, f"You edited a team theme: “{activity.excerpt(theme.name, 60)}”")
    return _saved_theme(view, theme, insight_ids)


def set_theme_members(view: TeamView, user: CurrentUser, theme_id: int, connection_ids: list[int]) -> dict:
    """Replaces the whole member list. Two people editing at once should use the add and
    remove routes, which cannot undo each other."""
    theme, group, insight_ids = visible_theme(view, theme_id)
    wanted = set(_checked_connection_ids(connection_ids, insight_ids))
    _write_theme_revision(view.db, theme, user.id)
    have: set[int] = set()
    for member in list(theme.members):
        if member.connection_id in wanted and member.connection_id not in have:
            have.add(member.connection_id)
        else:
            view.db.delete(member)
    for cid in sorted(wanted - have):
        view.db.add(TeamThemeMember(theme_id=theme.id, connection_id=cid))
    _log_theme_edit(
        view, user, theme, group,
        f"You changed which insights are in the team theme “{activity.excerpt(theme.name, 60)}”.",
    )
    return _saved_theme(view, theme, insight_ids)


def add_theme_member(view: TeamView, user: CurrentUser, theme_id: int, connection_id: int) -> dict:
    """Adds one insight. Adding one that is already there changes nothing and writes no revision."""
    theme, group, insight_ids = visible_theme(view, theme_id)
    _checked_connection_ids([connection_id], insight_ids)
    if any(m.connection_id == connection_id for m in theme.members):
        return _theme_dict(view, theme, insight_ids)
    _write_theme_revision(view.db, theme, user.id)
    view.db.add(TeamThemeMember(theme_id=theme.id, connection_id=connection_id))
    _log_theme_edit(
        view, user, theme, group, f"You added an insight to the team theme “{activity.excerpt(theme.name, 60)}”."
    )
    return _saved_theme(view, theme, insight_ids)


def remove_theme_member(view: TeamView, user: CurrentUser, theme_id: int, connection_id: int) -> dict:
    """Removes one insight. Removing one that is not there changes nothing and writes no
    revision. A member that is no longer filed can still be removed: the theme is visible,
    so every member of it is in the viewer's scope."""
    theme, group, insight_ids = visible_theme(view, theme_id)
    rows = [m for m in theme.members if m.connection_id == connection_id]
    if not rows:
        _checked_connection_ids([connection_id], insight_ids)
        return _theme_dict(view, theme, insight_ids)
    _write_theme_revision(view.db, theme, user.id)
    for row in rows:
        view.db.delete(row)
    _log_theme_edit(
        view, user, theme, group,
        f"You took an insight out of the team theme “{activity.excerpt(theme.name, 60)}”.",
    )
    return _saved_theme(view, theme, insight_ids)


def decide_theme(view: TeamView, user: CurrentUser, theme_id: int, status: str) -> dict:
    """Accept or dismiss a theme. A status change, so the row is kept either way."""
    theme, group, insight_ids = visible_theme(view, theme_id)
    if theme.status == status:
        return _theme_dict(view, theme, insight_ids)
    theme.status = status
    theme.updated_by = user.id
    accepted = status == "accepted"
    activity.log_activity(
        view.db, user.id, "user",
        activity.TEAM_THEME_ACCEPTED if accepted else activity.TEAM_THEME_DISMISSED,
        f"You {'kept' if accepted else 'set aside'} a team theme: “{activity.excerpt(theme.name, 60)}”",
        team_theme_id=theme.id, team_group_id=group.id,
    )
    return _saved_theme(view, theme, insight_ids)
