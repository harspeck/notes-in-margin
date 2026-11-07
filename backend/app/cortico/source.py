"""The data-source layer every Cortico read goes through.

Two implementations share one interface:

- `SnapshotSource` reads the local read-only SQLite copy (local dev and tests). Ids are the
  snapshot's own row ids, as they always have been in this app.
- `ApiSource` reads Cortico's v2 API with the signed-in user's token, through read-through
  caches in the sidecar (`rl_cortico_conversation`, `rl_cortico_snippet`). Ids are Cortico's.

A deployment uses exactly one source, and the ids stored in the sidecar belong to it
(`rl_workspace.data_source` records which). Moving a snapshot-era database onto the API
would need its conversation and snippet ids remapped through the snapshot's `cortico_id`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy.orm import Session

from .client import CorticoClient
from .errors import CorticoNotFound


@dataclass(frozen=True)
class ConversationRef:
    id: int
    title: str
    project_id: int | None
    forum_id: int | None = None
    audio_url: str | None = None
    has_audio: bool = False
    duration_seconds: float | None = None
    language: str | None = None
    start_time: str | None = None
    num_participants: int | None = None
    conversation_guide_id: int | None = None


@dataclass(frozen=True)
class SnippetRef:
    id: int
    conversation_id: int
    order_index: int
    text: str
    words: list[dict] = field(default_factory=list)
    uuid: str | None = None
    speaker_name: str | None = None
    is_facilitator: bool = False
    start_offset: float | None = None
    end_offset: float | None = None


@dataclass(frozen=True)
class GuideQuestionRef:
    id: int
    text: str
    shorthand_code: str | None
    order_index: int


class CorticoSource(Protocol):
    kind: str

    def conversation(self, conversation_id: int) -> ConversationRef | None: ...
    def conversations_by_ids(self, ids: set[int] | list[int]) -> dict[int, ConversationRef]: ...
    def list_conversations(self, project_ids: list[int] | None = None) -> list[ConversationRef]: ...
    def snippets(self, conversation_id: int) -> list[SnippetRef]: ...
    def snippets_by_ids(self, ids: set[int] | list[int]) -> dict[int, SnippetRef]: ...
    def guide_questions(self, conversation: ConversationRef) -> list[GuideQuestionRef]: ...


def as_source(obj) -> CorticoSource:
    """Accepts a source, or a snapshot Session (what services and tests have always passed)."""
    if isinstance(obj, Session):
        return SnapshotSource(obj)
    return obj


def _words(raw) -> list[dict]:
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


# --- snapshot ----------------------------------------------------------------------------


class SnapshotSource:
    kind = "snapshot"

    def __init__(self, cortico_db: Session):
        self.db = cortico_db

    @staticmethod
    def _conv(c) -> ConversationRef:
        return ConversationRef(
            id=c.id,
            title=c.title,
            project_id=c.project_id,
            forum_id=c.forum_id,
            audio_url=c.audio_url,
            has_audio=bool(c.audio_url),
            duration_seconds=c.duration_seconds,
            language=c.language,
            conversation_guide_id=c.conversation_guide_id,
        )

    def conversation(self, conversation_id: int) -> ConversationRef | None:
        from ..models_cortico import Conversation

        c = self.db.get(Conversation, conversation_id)
        return self._conv(c) if c else None

    def conversations_by_ids(self, ids) -> dict[int, ConversationRef]:
        from ..models_cortico import Conversation

        ids = set(ids)
        if not ids:
            return {}
        return {c.id: self._conv(c) for c in self.db.query(Conversation).filter(Conversation.id.in_(ids))}

    def list_conversations(self, project_ids: list[int] | None = None) -> list[ConversationRef]:
        from ..models_cortico import Conversation

        q = self.db.query(Conversation)
        if project_ids:
            q = q.filter(Conversation.project_id.in_(project_ids))
        return [self._conv(c) for c in q.order_by(Conversation.id).all()]

    def _snippet_refs(self, rows) -> list[SnippetRef]:
        from ..models_cortico import Participant

        participant_ids = {s.participant_id for s in rows if s.participant_id}
        participants = (
            {p.id: p for p in self.db.query(Participant).filter(Participant.id.in_(participant_ids))}
            if participant_ids
            else {}
        )
        out = []
        for s in rows:
            p = participants.get(s.participant_id)
            out.append(
                SnippetRef(
                    id=s.id,
                    conversation_id=s.conversation_id,
                    order_index=s.order_index,
                    text=s.text,
                    words=_words(s.words),
                    speaker_name=p.name if p else None,
                    is_facilitator=bool(p.is_facilitator) if p else False,
                    start_offset=s.start_offset,
                    end_offset=s.end_offset,
                )
            )
        return out

    def snippets(self, conversation_id: int) -> list[SnippetRef]:
        from ..models_cortico import Snippet

        rows = (
            self.db.query(Snippet)
            .filter(Snippet.conversation_id == conversation_id)
            .order_by(Snippet.order_index)
            .all()
        )
        return self._snippet_refs(rows)

    def snippets_by_ids(self, ids) -> dict[int, SnippetRef]:
        from ..models_cortico import Snippet

        ids = set(ids)
        if not ids:
            return {}
        rows = self.db.query(Snippet).filter(Snippet.id.in_(ids)).all()
        return {s.id: s for s in self._snippet_refs(rows)}

    def guide_questions(self, conversation: ConversationRef) -> list[GuideQuestionRef]:
        from ..guide import guide_questions_in_order, resolve_guide

        guide = resolve_guide(self.db, conversation)
        if guide is None:
            return []
        return [
            GuideQuestionRef(id=q.id, text=q.text, shorthand_code=q.shorthand_code, order_index=q.order_index)
            for q in guide_questions_in_order(self.db, guide)
        ]


# --- Cortico v2 API ---------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ApiSource:
    kind = "cortico_api"

    def __init__(self, client: CorticoClient, sidecar: Session, *, snippet_ttl_seconds: int = 3600,
                 workspace_project_ids: list[int] | None = None):
        self.client = client
        self.db = sidecar
        self.snippet_ttl = timedelta(seconds=snippet_ttl_seconds)
        self.workspace_project_ids = workspace_project_ids or []

    # conversations

    @staticmethod
    def _ref(row) -> ConversationRef:
        return ConversationRef(
            id=row.id,
            title=row.title or f"Conversation {row.id}",
            project_id=row.project_id,
            forum_id=row.forum_id,
            has_audio=bool(row.has_audio),
            duration_seconds=row.duration_seconds,
            start_time=row.start_time,
            num_participants=row.num_participants,
        )

    def _upsert_conversation(self, c: dict, project_id: int | None):
        from ..models_prototype import RLCorticoConversation

        row = self.db.get(RLCorticoConversation, c["id"])
        if row is None:
            row = RLCorticoConversation(id=c["id"])
            self.db.add(row)
        if project_id is not None:
            row.project_id = project_id
        row.forum_id = c.get("forum_id")
        row.title = c.get("title")
        row.duration_seconds = c.get("duration")
        row.start_time = c.get("start_time")
        row.has_audio = bool(c.get("audio_url"))
        row.num_participants = c.get("num_participants")
        row.fetched_at = _now()
        return row

    def list_conversations(self, project_ids: list[int] | None = None) -> list[ConversationRef]:
        project_ids = project_ids if project_ids is not None else self.workspace_project_ids
        rows = []
        for project_id in project_ids:
            for c in self.client.conversations(project_id):
                rows.append(self._upsert_conversation(c, project_id))
        self.db.commit()
        rows.sort(key=lambda r: (r.start_time or "", r.id), reverse=True)
        return [self._ref(r) for r in rows]

    def conversation(self, conversation_id: int) -> ConversationRef | None:
        from ..models_prototype import RLCorticoConversation

        try:
            c = self.client.conversation(conversation_id)
        except CorticoNotFound:
            return None
        cached = self.db.get(RLCorticoConversation, conversation_id)
        project_id = cached.project_id if cached else None
        if project_id is None and self.workspace_project_ids:
            # Deep link to a conversation not listed yet: refresh the workspace listing once.
            self.list_conversations()
            cached = self.db.get(RLCorticoConversation, conversation_id)
            project_id = cached.project_id if cached else None
        row = self._upsert_conversation(c, project_id)
        self.db.commit()
        ref = self._ref(row)
        return ConversationRef(**{**ref.__dict__, "audio_url": c.get("audio_url")})

    def conversations_by_ids(self, ids) -> dict[int, ConversationRef]:
        from ..models_prototype import RLCorticoConversation

        ids = set(ids)
        if not ids:
            return {}
        return {r.id: self._ref(r) for r in self.db.query(RLCorticoConversation).filter(RLCorticoConversation.id.in_(ids))}

    # snippets

    @staticmethod
    def _snippet_ref(r) -> SnippetRef:
        return SnippetRef(
            id=r.id,
            conversation_id=r.conversation_id,
            order_index=r.order_index,
            text=r.text or "",
            words=_words(r.words_json),
            uuid=r.uuid,
            speaker_name=r.speaker_name,
            is_facilitator=bool(r.is_facilitator),
            start_offset=r.start_offset,
            end_offset=r.end_offset,
        )

    def snippets(self, conversation_id: int) -> list[SnippetRef]:
        from ..models_prototype import RLCorticoSnippet

        cached = (
            self.db.query(RLCorticoSnippet)
            .filter(RLCorticoSnippet.conversation_id == conversation_id)
            .order_by(RLCorticoSnippet.order_index)
            .all()
        )
        fresh = cached and all(_aware(r.fetched_at) > _now() - self.snippet_ttl for r in cached)
        if fresh:
            return [self._snippet_ref(r) for r in cached]

        by_id = {r.id: r for r in cached}
        rows = []
        for i, s in enumerate(self.client.transcript(conversation_id)):
            row = by_id.pop(s["id"], None) or RLCorticoSnippet(id=s["id"], conversation_id=conversation_id)
            if row not in self.db:
                self.db.add(row)
            row.uuid = s.get("uuid")
            row.order_index = s.get("index_in_conversation") if s.get("index_in_conversation") is not None else i
            row.speaker_name = s.get("speaker_name")
            row.is_facilitator = bool(s.get("is_facilitator"))
            row.start_offset = s.get("audio_start_offset")
            row.end_offset = s.get("audio_end_offset")
            words = s.get("words") or []
            row.words_json = json.dumps(words)
            row.text = s.get("content") or " ".join(w.get("word", "") for w in words)
            row.fetched_at = _now()
            rows.append(row)
        # Snippets that vanished upstream stay cached: evidence may still quote them.
        self.db.commit()
        rows.sort(key=lambda r: r.order_index)
        return [self._snippet_ref(r) for r in rows]

    def snippets_by_ids(self, ids) -> dict[int, SnippetRef]:
        from ..models_prototype import RLCorticoSnippet

        ids = set(ids)
        if not ids:
            return {}
        return {r.id: self._snippet_ref(r) for r in self.db.query(RLCorticoSnippet).filter(RLCorticoSnippet.id.in_(ids))}

    # guide

    def guide_questions(self, conversation: ConversationRef) -> list[GuideQuestionRef]:
        if conversation.project_id is None:
            return []
        guides = self.client.conversation_guides(conversation.project_id)
        if not guides:
            return []
        # Same rule as the snapshot: the project's latest guide version.
        guide = max(guides, key=lambda g: ((g.get("version") or {}).get("version") or 0, g.get("id") or 0))
        sections = sorted((guide.get("version") or {}).get("sections") or [], key=lambda s: s.get("order") or 0)
        out: list[GuideQuestionRef] = []
        for section in sections:
            for prompt in sorted(section.get("prompts") or [], key=lambda p: p.get("order") or 0):
                out.append(
                    GuideQuestionRef(
                        id=prompt["id"],
                        text=prompt.get("content") or "",
                        shorthand_code=section.get("title"),
                        order_index=len(out),
                    )
                )
        return out
