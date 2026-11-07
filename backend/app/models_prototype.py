"""Prototype-owned tables, V2-shaped where Cortico has an equivalent object.

Table/column choices follow docs/prototype-log.md where they diverge from the PRD's
data-model section (notably: `highlight` -> `rl_highlight` to avoid colliding with
Cortico's own read-only `highlight` table, and `snippet_uuid` -> `snippet_id` since
snippets here carry no UUID).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import PrototypeBase


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchQuestion(PrototypeBase):
    __tablename__ = "research_question"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    display_order = Column(Integer, nullable=False, default=0)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_now)


class RLHighlight(PrototypeBase):
    """Mirrors Cortico's `Highlight`/`CreateHighlightSchema` shape; renamed from the PRD's
    `highlight` to avoid colliding with Cortico's own read-only `highlight` table."""

    __tablename__ = "rl_highlight"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False)
    title = Column(String)
    notes = Column(Text)  # the margin memo
    starred = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    snippets = relationship("HighlightSnippet", backref="highlight", cascade="all, delete-orphan")


class HighlightSnippet(PrototypeBase):
    __tablename__ = "highlight_snippet"
    id = Column(Integer, primary_key=True)
    highlight_id = Column(Integer, ForeignKey("rl_highlight.id"), nullable=False)
    snippet_id = Column(Integer, nullable=False)  # Cortico snippet.id (no uuid in schema.sql)
    start_index = Column(Integer, nullable=False)
    end_index = Column(Integer, nullable=False)


class VoiceMemo(PrototypeBase):
    __tablename__ = "voice_memo"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=False)
    audio_path = Column(String, nullable=False)
    duration = Column(Float)
    recorded_at = Column(DateTime, nullable=False, default=_now)
    transcription_raw = Column(Text)  # JSON: [{word,start,end,confidence}, ...]
    transcription_edited = Column(Text)
    transcription_engine = Column(String)
    transcription_progress = Column(Float, nullable=False, default=0.0)  # 0..1, while transcribing
    transcription_error = Column(Text)
    extracted_interests = Column(Text)  # JSON: list[str]
    status = Column(String, nullable=False, default="recorded")
    # recorded -> transcribing -> ready_for_review | failed


class ReflectionConnection(PrototypeBase):
    __tablename__ = "reflection_connection"
    id = Column(Integer, primary_key=True)
    voice_memo_id = Column(Integer, ForeignKey("voice_memo.id"), nullable=False)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=True)
    insight_text = Column(Text, nullable=False)
    source = Column(String, nullable=False)  # user | ai
    status = Column(String, nullable=False, default="suggested")
    # suggested -> accepted | edited | rejected ; accepted/edited can later -> retracted
    edited_from_ai = Column(Boolean, nullable=False, default=False)
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt, confidence, rationale}
    memo_order = Column(Integer)  # manual reorder within its RQ section; falls back to created_at
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    evidence = relationship("ConnectionEvidence", backref="connection", cascade="all, delete-orphan")
    revisions = relationship(
        "ReflectionConnectionRevision", backref="connection", cascade="all, delete-orphan"
    )


class ConnectionEvidence(PrototypeBase):
    __tablename__ = "connection_evidence"
    id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False)
    highlight_id = Column(Integer, ForeignKey("rl_highlight.id"), nullable=True)
    snippet_id = Column(Integer, nullable=False)
    start_index = Column(Integer, nullable=False)
    end_index = Column(Integer, nullable=False)
    source = Column(String, nullable=False)  # user | ai
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class ReflectionConnectionRevision(PrototypeBase):
    __tablename__ = "reflection_connection_revision"
    id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False)
    insight_text = Column(Text, nullable=False)
    evidence_snapshot = Column(Text, nullable=False)  # JSON snapshot of evidence rows
    changed_at = Column(DateTime, nullable=False, default=_now)


class TranscriptState(PrototypeBase):
    __tablename__ = "transcript_state"
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=False)
    reached_end_at = Column(DateTime)
    prompt_dismissed_at = Column(DateTime)

    __table_args__ = (PrimaryKeyConstraint("user_id", "conversation_id"),)


class SectionCheckpoint(PrototypeBase):
    __tablename__ = "section_checkpoint"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=False)
    snippet_id = Column(Integer, nullable=False)
    guide_question_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "conversation_id", "guide_question_id", name="uq_checkpoint_per_question"
        ),
    )


class ConversationGist(PrototypeBase):
    """Cached AI summary of a Cortico conversation — a corpus-level fact (not per-user),
    generated once by backend/scripts/backfill_gists.py and reused across users/RQs so
    next-step suggestion generation never has to re-summarize the corpus."""

    __tablename__ = "conversation_gist"
    conversation_id = Column(Integer, primary_key=True)  # Cortico's int id, not a surrogate key
    project_id = Column(Integer, nullable=False)
    forum_id = Column(Integer)
    summary_text = Column(Text, nullable=False)
    keywords_json = Column(Text, nullable=False)  # JSON: list[str]
    generated_at = Column(DateTime, nullable=False, default=_now)


class NextStepSuggestion(PrototypeBase):
    __tablename__ = "next_step_suggestion"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=True)
    suggested_direction = Column(Text)
    recommended_conversation_id = Column(Integer)  # Cortico conversation.id, validated before persisting
    rationale = Column(Text)
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt}
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class CodeFamily(PrototypeBase):
    """A proposed thematic cluster of a research question's filed memo entries — a
    precursor to a future codebook/tagging system, not a replacement for one (see
    docs/prd-phase-2.md)."""

    __tablename__ = "code_family"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False)
    name = Column(String, nullable=False)
    definition = Column(Text, nullable=False)
    # suggested -> accepted | dismissed; also "merged" once folded into another family by an
    # accepted CodeFamilyMergeSuggestion (kept logged, not deleted, like dismissed ones)
    status = Column(String, nullable=False, default="suggested")
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt}
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    members = relationship("CodeFamilyMember", backref="family", cascade="all, delete-orphan")


class CodeFamilyMember(PrototypeBase):
    """Join: which filed memo entries (reflection_connection rows) exemplify a family."""

    __tablename__ = "code_family_member"
    id = Column(Integer, primary_key=True)
    code_family_id = Column(Integer, ForeignKey("code_family.id"), nullable=False)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False)


class CodeFamilyMergeSuggestion(PrototypeBase):
    """An AI-proposed merge of two or more of an RQ's code families, offered once the RQ
    nears its family cap (config.code_families). Accepting folds every source family's
    members into target_family_id, renames it to the proposed name/definition, and marks the
    other source families "merged". Not in docs/prd-phase-2.md — see docs/prototype-log.md."""

    __tablename__ = "code_family_merge_suggestion"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False)
    family_ids_json = Column(Text, nullable=False)  # JSON list of code_family ids being merged
    target_family_id = Column(Integer, ForeignKey("code_family.id"), nullable=False)
    proposed_name = Column(String, nullable=False)
    proposed_definition = Column(Text, nullable=False)
    rationale = Column(Text)
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt}
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class RQFocus(PrototypeBase):
    """Whether the user has picked this RQ for their working set on the Organize page. A
    missing row means not focused."""

    __tablename__ = "rl_rq_focus"
    research_question_id = Column(Integer, ForeignKey("research_question.id"), primary_key=True)
    user_id = Column(Integer, nullable=False)
    focused = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class RQMerge(PrototypeBase):
    """One user merge of a research question into another. The source is archived and its
    entries and families read under the target (services/rq_merges.py). Undo sets the
    status; the row is kept."""

    __tablename__ = "rl_rq_merge"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    source_rq_id = Column(Integer, ForeignKey("research_question.id"), nullable=False)
    target_rq_id = Column(Integer, ForeignKey("research_question.id"), nullable=False)
    status = Column(String, nullable=False, default="active")  # active -> undone
    created_at = Column(DateTime, nullable=False, default=_now)
    undone_at = Column(DateTime)


class CodeFamilyStar(PrototypeBase):
    __tablename__ = "rl_code_family_star"
    code_family_id = Column(Integer, ForeignKey("code_family.id"), primary_key=True)
    starred = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class CodeFamilyRevision(PrototypeBase):
    """Snapshot of a family taken before each user edit (rename, membership, merge), so a
    removed member stays auditable even though its code_family_member row is deleted."""

    __tablename__ = "rl_code_family_revision"
    id = Column(Integer, primary_key=True)
    code_family_id = Column(Integer, ForeignKey("code_family.id"), nullable=False)
    name = Column(String, nullable=False)
    definition = Column(Text, nullable=False)
    member_connection_ids_json = Column(Text, nullable=False)  # JSON list of connection ids
    changed_at = Column(DateTime, nullable=False, default=_now)


class AppSetting(PrototypeBase):
    """Not in the PRD's data model — added to persist the editable AI-review prompt
    (view/save/reset-to-default) across restarts. See docs/prototype-log.md."""

    __tablename__ = "app_setting"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# --- Cortico v2 integration (cortico_api mode). New tables only, so create_all picks them
# up on an existing database without a migration. All carry the rl_ prefix (db/schema.sql).


class RLSession(PrototypeBase):
    """A signed-in browser session. The cookie holds a random token; only its sha256 is
    stored. Cortico tokens are encrypted with SESSION_SECRET. Refresh tokens rotate on every
    use, so refresh runs under a row lock (see app/auth.py)."""

    __tablename__ = "rl_session"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)  # Cortico user id from /v2/me
    email = Column(String)
    display_name = Column(String)
    access_token_enc = Column(Text, nullable=False)
    refresh_token_enc = Column(Text, nullable=False)
    access_expires_at = Column(DateTime(timezone=True), nullable=False)
    active_workspace_id = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RLWorkspace(PrototypeBase):
    """A user-named bundle of Cortico projects in one organization. The integer id is the
    handle other apps (llm-auto-highlighter stores single-int workspace keys) can adopt."""

    __tablename__ = "rl_workspace"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    project_ids_json = Column(Text, nullable=False)  # JSON list[int] of Cortico project ids
    data_source = Column(String, nullable=False)  # ids only make sense within one source
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class RLCorticoConversation(PrototypeBase):
    """Read-through cache of v2 conversation metadata. v2 conversations carry forum_id but
    not project_id, so the project comes from the /v2/conversations?project_id= listing."""

    __tablename__ = "rl_cortico_conversation"
    id = Column(Integer, primary_key=True, autoincrement=False)  # Cortico conversation id
    project_id = Column(Integer, index=True)
    forum_id = Column(Integer)
    title = Column(String)
    duration_seconds = Column(Float)
    start_time = Column(String)
    has_audio = Column(Boolean, nullable=False, default=False)
    num_participants = Column(Integer)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RLCorticoSnippet(PrototypeBase):
    """Read-through cache of v2 transcript snippets. Evidence rows store only
    (snippet_id, start, end), so quoting them later needs the words without a conversation
    id in hand. The uuid is what POST /v2/highlights needs."""

    __tablename__ = "rl_cortico_snippet"
    id = Column(Integer, primary_key=True, autoincrement=False)  # Cortico snippet id
    uuid = Column(String)
    conversation_id = Column(Integer, nullable=False, index=True)
    order_index = Column(Integer, nullable=False)
    speaker_name = Column(String)
    is_facilitator = Column(Boolean, nullable=False, default=False)
    start_offset = Column(Float)
    end_offset = Column(Float)
    text = Column(Text, nullable=False, default="")
    words_json = Column(Text, nullable=False, default="[]")
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RLCorticoSync(PrototypeBase):
    """One row per local object mirrored to Cortico (today: highlights). The local write
    always succeeds first; this row records whether the Cortico copy exists, so a failed
    push can be retried without creating duplicates."""

    __tablename__ = "rl_cortico_sync"
    id = Column(Integer, primary_key=True)
    local_kind = Column(String, nullable=False)  # "highlight"
    local_id = Column(Integer, nullable=False)
    cortico_kind = Column(String, nullable=False)  # "highlight"
    cortico_id = Column(Integer)
    status = Column(String, nullable=False, default="pending")  # pending|synced|failed|skipped|deleted
    error = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("local_kind", "local_id", name="uq_rl_cortico_sync_local"),)
