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


# --- Server-side state for the hosted app (2026-11-18). New tables only.


class ReadingState(PrototypeBase):
    """That a user opened a conversation, and how far they got. One row per pair."""

    __tablename__ = "rl_reading_state"
    user_id = Column(Integer, nullable=False)
    conversation_id = Column(Integer, nullable=False)
    first_opened_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_opened_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    position_seconds = Column(Float)
    position_snippet_id = Column(Integer)

    __table_args__ = (PrimaryKeyConstraint("user_id", "conversation_id"),)


class ReviewDraft(PrototypeBase):
    """The client's unsaved review decisions for one voice memo. The payload is opaque to
    the server. Filing the memo deletes the row."""

    __tablename__ = "rl_review_draft"
    voice_memo_id = Column(Integer, ForeignKey("voice_memo.id"), primary_key=True, autoincrement=False)
    user_id = Column(Integer, nullable=False)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ListeningInterest(PrototypeBase):
    """One "what I want to hear next" line. Lines the AI review extracted from a reflection
    have origin "reflection" and its voice memo id; lines the user typed have origin "user".
    Removing sets the status; rows are never deleted."""

    __tablename__ = "rl_listening_interest"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    text = Column(Text, nullable=False)
    origin = Column(String, nullable=False)  # reflection | user | suggestion
    voice_memo_id = Column(Integer, ForeignKey("voice_memo.id"), nullable=True)
    status = Column(String, nullable=False, default="active")  # active -> removed
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ListeningSuggestion(PrototypeBase):
    """A "what I want to hear next" line the AI offered. Accepting it adds a
    `rl_listening_interest` row; dismissing keeps the row, so the line is not offered again."""

    __tablename__ = "rl_listening_suggestion"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("rl_listening_suggestion_run.id"), nullable=False)
    text = Column(Text, nullable=False)
    reason = Column(Text)
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    listening_interest_id = Column(Integer, ForeignKey("rl_listening_interest.id"), nullable=True)
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt}
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ListeningSuggestionRun(PrototypeBase):
    """One model call for listening suggestions. `fingerprint` is the cache key: the
    questions and highlights the model was shown. A run that offered nothing still gets a
    row, so the same inputs are not sent twice."""

    __tablename__ = "rl_listening_suggestion_run"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    fingerprint = Column(String, nullable=False)
    suggestion_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RecommendationRun(PrototypeBase):
    """One time `POST /next_steps/recommend` went to the model. Written before the call, so
    a second request arriving meanwhile sees it. `fingerprint` is what the model was shown,
    or "failed" when the call did not finish."""

    __tablename__ = "rl_recommendation_run"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class ReviewRun(PrototypeBase):
    """One AI review of a reflection, claimed before the model is called, so a second
    request (a double click, another tab, a retry) does not pay for and save a second set
    of suggestions. status: running | done | failed."""

    __tablename__ = "rl_review_run"
    voice_memo_id = Column(Integer, ForeignKey("voice_memo.id"), primary_key=True)
    status = Column(String, nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class SetAside(PrototypeBase):
    """One row each time a connection leaves the user's working set: retracted from the memo
    or rejected at review. It holds the status the connection had before, which is what
    restore puts back, and the time, which reflection_connection has no column for."""

    __tablename__ = "rl_set_aside"
    id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String, nullable=False)  # retracted | rejected
    previous_status = Column(String, nullable=False)
    set_aside_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    restored_at = Column(DateTime(timezone=True))


class Activity(PrototypeBase):
    """The activity log: one row per thing the AI suggested or the user decided. Written only
    by services/activity.py, where the kind strings are listed."""

    __tablename__ = "rl_activity"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    actor = Column(String, nullable=False)  # ai | user
    kind = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    refs_json = Column(Text, nullable=False, default="{}")  # JSON object of ids


# --- Questions screen and notebook levers (2026-11-18). New tables only.


class HighlightQuestion(PrototypeBase):
    """A highlight filed under a research question by the user. Not audit data: replacing a
    highlight's questions, or deleting the highlight, deletes its rows."""

    __tablename__ = "rl_highlight_question"
    id = Column(Integer, primary_key=True)
    highlight_id = Column(Integer, ForeignKey("rl_highlight.id"), nullable=False, index=True)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("highlight_id", "research_question_id", name="uq_rl_highlight_question"),
    )


class InsightMove(PrototypeBase):
    """One move of a filed insight from one research question to another (either side null
    for "doesn't fit a question yet"). reflection_connection_revision has no column for the
    question an insight was under, so the move is recorded here."""

    __tablename__ = "rl_insight_move"
    id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    from_research_question_id = Column(Integer)
    to_research_question_id = Column(Integer)
    moved_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class InsightMerge(PrototypeBase):
    """One merge of a filed insight (the source) into another (the target). The target gained
    the evidence rows listed here and the source's status became "merged". Undo sets the
    status; the row is kept."""

    __tablename__ = "rl_insight_merge"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    source_connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False, index=True)
    target_connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False, index=True)
    source_previous_status = Column(String, nullable=False)
    # JSON [{id, snippet_id, start_index, end_index}]: the evidence rows the target gained.
    # Undo matches on the span, because editing the target's evidence re-creates its rows.
    added_evidence_json = Column(Text, nullable=False, default="[]")
    status = Column(String, nullable=False, default="active")  # active -> undone
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    undone_at = Column(DateTime(timezone=True))


class MergeNudge(PrototypeBase):
    """An AI suggestion that two research questions look alike, or that two insights under
    one question say nearly the same thing. Dismissed means "kept separate"; a pair with any
    row here is never suggested again."""

    __tablename__ = "rl_merge_nudge"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String, nullable=False)  # questions | insights
    item_a_id = Column(Integer, nullable=False)  # research_question.id or reflection_connection.id
    item_b_id = Column(Integer, nullable=False)
    default_target_id = Column(Integer, nullable=False)  # one of the two
    research_question_id = Column(Integer)  # insights: the question both were under
    reason = Column(Text, nullable=False)
    suggested_wording = Column(Text)  # questions only
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    result_merge_id = Column(Integer)  # rl_rq_merge.id or rl_insight_merge.id once accepted
    ai_metadata = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class PassageSuggestion(PrototypeBase):
    """A transcript passage the AI found for a research question. Accepting creates a real
    highlight (highlight_id, not a foreign key, so deleting that highlight later is not
    blocked) filed under the question."""

    __tablename__ = "rl_passage_suggestion"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False, index=True)
    conversation_id = Column(Integer, nullable=False)
    snippet_id = Column(Integer, nullable=False)
    start_index = Column(Integer, nullable=False)
    end_index = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    highlight_id = Column(Integer)
    ai_metadata = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class QuestionGap(PrototypeBase):
    """One "what am I missing" observation the AI made about a research question, optionally
    naming conversations the user has not opened."""

    __tablename__ = "rl_question_gap"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False, index=True)
    gap_kind = Column(String, nullable=False)  # perspective | counter_view | thin_spot
    observation = Column(Text, nullable=False)
    conversation_ids_json = Column(Text, nullable=False, default="[]")  # validated Cortico ids
    status = Column(String, nullable=False, default="suggested")  # suggested -> dismissed
    ai_metadata = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class RLCorticoGuide(PrototypeBase):
    """Read-through cache of a project's latest conversation guide questions (cortico_api
    mode), so listing guide ideas does not call Cortico per conversation."""

    __tablename__ = "rl_cortico_guide"
    project_id = Column(Integer, primary_key=True, autoincrement=False)
    questions_json = Column(Text, nullable=False, default="[]")  # [{id, text, shorthand_code, order_index}]
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_now)


# --- Team Page and team codebook (2026-11-18). New tables only. Every read of these goes
# through services/team.py, which limits it to the viewer's conversations.


class Adoption(PrototypeBase):
    """That a user copied a teammate's object into their own work. Today only questions:
    `source_id` is the teammate's research_question.id and `new_id` the viewer's copy."""

    __tablename__ = "rl_adoption"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String, nullable=False)  # question
    source_id = Column(Integer, nullable=False)
    source_user_id = Column(Integer, nullable=False)
    new_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class TeamQuestionGroup(PrototypeBase):
    """Research questions of different people that ask the same thing. A group belongs to
    nobody: whoever can see one of its questions can see and change it."""

    __tablename__ = "rl_team_question_group"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="active")  # suggested -> active | dismissed
    created_by = Column(Integer)  # user id; null when the AI suggested it
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt, requested_by}; null for user rows
    rationale = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    members = relationship("TeamQuestionMember", backref="group", cascade="all, delete-orphan")


class TeamQuestionMember(PrototypeBase):
    """A question in a group. One question is in at most one active group, which
    services/team.py enforces (409), because a suggested group may overlap an active one."""

    __tablename__ = "rl_team_question_member"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("rl_team_question_group.id"), nullable=False, index=True)
    research_question_id = Column(Integer, ForeignKey("research_question.id"), nullable=False, index=True)
    added_by = Column(Integer)  # user id; null when the AI suggested the group
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class TeamTheme(PrototypeBase):
    """A theme of the team codebook: a cluster of filed insights, from any author, under one
    question group."""

    __tablename__ = "rl_team_theme"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("rl_team_question_group.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    definition = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="suggested")  # suggested -> accepted | dismissed
    ai_metadata = Column(Text)  # JSON: {model, reasoning_effort, prompt, requested_by}; null for user rows
    created_by = Column(Integer)  # user id; null when the AI suggested it
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    members = relationship("TeamThemeMember", backref="theme", cascade="all, delete-orphan")


class TeamThemeMember(PrototypeBase):
    __tablename__ = "rl_team_theme_member"
    id = Column(Integer, primary_key=True)
    theme_id = Column(Integer, ForeignKey("rl_team_theme.id"), nullable=False, index=True)
    connection_id = Column(Integer, ForeignKey("reflection_connection.id"), nullable=False)


class TeamThemeRevision(PrototypeBase):
    """Snapshot of a theme taken before each user edit (name, definition, members), so a
    removed member stays auditable although its rl_team_theme_member row is deleted."""

    __tablename__ = "rl_team_theme_revision"
    id = Column(Integer, primary_key=True)
    theme_id = Column(Integer, ForeignKey("rl_team_theme.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    definition = Column(Text, nullable=False)
    member_ids_json = Column(Text, nullable=False)  # JSON list of reflection_connection ids
    edited_by = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
