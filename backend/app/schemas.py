from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HighlightSnippetIn(BaseModel):
    snippet_id: int
    start_index: int
    end_index: int


class HighlightCreate(BaseModel):
    title: str | None = None
    notes: str | None = None
    snippets: list[HighlightSnippetIn]


class HighlightUpdate(BaseModel):
    title: str | None = None
    notes: str | None = None
    starred: bool | None = None


class CheckpointSet(BaseModel):
    guide_question_id: int
    snippet_id: int


class ResearchQuestionCreate(BaseModel):
    name: str
    description: str | None = None


class ResearchQuestionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


class ReorderRequest(BaseModel):
    ordered_ids: list[int]


class TranscriptStateUpdate(BaseModel):
    reached_end_at: bool | None = None
    prompt_dismissed: bool | None = None


class VoiceMemoEdit(BaseModel):
    transcription_edited: str


class PromptUpdate(BaseModel):
    text: str


EvidenceSource = Literal["user", "ai"]


class EvidenceIn(BaseModel):
    highlight_id: int | None = None
    snippet_id: int
    start_index: int
    end_index: int
    source: EvidenceSource = "user"


class ConnectionDecision(BaseModel):
    connection_id: int | None = None  # None => a new, user-added connection
    action: Literal["accept", "edit", "reject"]
    research_question_id: int | None = None
    insight_text: str | None = None
    evidence: list[EvidenceIn] | None = None


class FileToMemoRequest(BaseModel):
    decisions: list[ConnectionDecision]


class MemoEntryUpdate(BaseModel):
    insight_text: str | None = None
    evidence: list[EvidenceIn] | None = None


class MemoReorderRequest(BaseModel):
    research_question_id: int
    ordered_connection_ids: list[int]


class CodeFamilyUpdate(BaseModel):
    name: str | None = None
    definition: str | None = None


class CodeFamilyCreate(BaseModel):
    name: str
    definition: str
    connection_ids: list[int] = []


class CodeFamilyMembersUpdate(BaseModel):
    connection_ids: list[int]


class CodeFamilyMergeRequest(BaseModel):
    family_ids: list[int]
    name: str
    definition: str


class StarRequest(BaseModel):
    starred: bool


class FocusRequest(BaseModel):
    focused: bool


class RQMergeRequest(BaseModel):
    target_id: int
