import type {
  CodebookPrep,
  CodeFamily,
  CodeFamilyCap,
  CodeFamilyMergeResult,
  CodeFamilyState,
  ConnectionEvidence,
  ConversationDetail,
  ConversationSummary,
  Highlight,
  MemoResponse,
  NextStepSuggestion,
  OrganizeResponse,
  ResearchQuestion,
  ReviewResult,
  RQMergeResult,
  RQMergeUndoResult,
  VoiceMemo,
} from "./types";

const BASE = "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: init?.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${path}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  listConversations: () => request<ConversationSummary[]>("/conversations"),
  getConversation: (id: number) => request<ConversationDetail>(`/conversations/${id}`),
  setTranscriptState: (id: number, body: { reached_end_at?: boolean; prompt_dismissed?: boolean }) =>
    request(`/conversations/${id}/state`, { method: "PUT", body: JSON.stringify(body) }),

  listHighlights: (conversationId: number) => request<Highlight[]>(`/conversations/${conversationId}/highlights`),
  createHighlight: (
    conversationId: number,
    body: { title?: string; notes?: string; snippets: { snippet_id: number; start_index: number; end_index: number }[] }
  ) => request<Highlight>(`/conversations/${conversationId}/highlights`, { method: "POST", body: JSON.stringify(body) }),
  updateHighlight: (id: number, body: { notes?: string; starred?: boolean; title?: string }) =>
    request<Highlight>(`/highlights/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteHighlight: (id: number) => request(`/highlights/${id}`, { method: "DELETE" }),

  setCheckpoint: (conversationId: number, guideQuestionId: number, snippetId: number) =>
    request(`/conversations/${conversationId}/checkpoints`, {
      method: "PUT",
      body: JSON.stringify({ guide_question_id: guideQuestionId, snippet_id: snippetId }),
    }),
  deleteCheckpoint: (conversationId: number, guideQuestionId: number) =>
    request(`/conversations/${conversationId}/checkpoints/${guideQuestionId}`, { method: "DELETE" }),

  listResearchQuestions: (includeArchived = false) =>
    request<ResearchQuestion[]>(`/research_questions?include_archived=${includeArchived}`),
  createResearchQuestion: (body: { name: string; description?: string }) =>
    request<ResearchQuestion>("/research_questions", { method: "POST", body: JSON.stringify(body) }),
  updateResearchQuestion: (id: number, body: { name?: string; description?: string; archived?: boolean }) =>
    request<ResearchQuestion>(`/research_questions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  reorderResearchQuestions: (orderedIds: number[]) =>
    request(`/research_questions/reorder`, { method: "PUT", body: JSON.stringify({ ordered_ids: orderedIds }) }),
  setResearchQuestionFocus: (id: number, focused: boolean) =>
    request<ResearchQuestion>(`/research_questions/${id}/focus`, { method: "POST", body: JSON.stringify({ focused }) }),
  mergeResearchQuestion: (id: number, targetId: number) =>
    request<RQMergeResult>(`/research_questions/${id}/merge_into`, {
      method: "POST",
      body: JSON.stringify({ target_id: targetId }),
    }),
  undoResearchQuestionMerge: (mergeId: number) =>
    request<RQMergeUndoResult>(`/rq_merges/${mergeId}/undo`, { method: "POST" }),

  getOrganize: () => request<OrganizeResponse>("/organize"),
  getCodebookPrep: () => request<CodebookPrep>("/codebook_prep"),

  uploadVoiceMemo: (conversationId: number, audio: Blob, duration: number) => {
    const form = new FormData();
    form.append("audio", audio, "memo.webm");
    form.append("duration", String(duration));
    return request<VoiceMemo>(`/conversations/${conversationId}/voice_memos`, { method: "POST", body: form });
  },
  getVoiceMemo: (id: number) => request<VoiceMemo>(`/voice_memos/${id}`),
  listVoiceMemosForConversation: (conversationId: number) =>
    request<VoiceMemo[]>(`/conversations/${conversationId}/voice_memos`),
  editVoiceMemoTranscription: (id: number, text: string) =>
    request<VoiceMemo>(`/voice_memos/${id}`, { method: "PATCH", body: JSON.stringify({ transcription_edited: text }) }),

  runReview: (voiceMemoId: number) => request<ReviewResult>(`/voice_memos/${voiceMemoId}/review`, { method: "POST" }),
  getReview: (voiceMemoId: number) => request<ReviewResult>(`/voice_memos/${voiceMemoId}/review`),
  fileToMemo: (
    voiceMemoId: number,
    decisions: {
      connection_id: number | null;
      action: "accept" | "edit" | "reject";
      research_question_id?: number | null;
      insight_text?: string;
      evidence?: ConnectionEvidence[];
    }[]
  ) => request(`/voice_memos/${voiceMemoId}/file`, { method: "POST", body: JSON.stringify({ decisions }) }),

  getMemo: () => request<MemoResponse>("/memo"),
  editMemoEntry: (connectionId: number, body: { insight_text?: string; evidence?: ConnectionEvidence[] }) =>
    request(`/memo/connections/${connectionId}`, { method: "PATCH", body: JSON.stringify(body) }),
  retractMemoEntry: (connectionId: number) => request(`/memo/connections/${connectionId}/retract`, { method: "POST" }),

  generateNextSteps: () => request<NextStepSuggestion[]>("/next_steps/generate", { method: "POST" }),
  listNextSteps: (includeDismissed = false) =>
    request<NextStepSuggestion[]>(`/next_steps?include_dismissed=${includeDismissed}`),
  acceptNextStep: (id: number) => request(`/next_steps/${id}/accept`, { method: "POST" }),
  dismissNextStep: (id: number) => request(`/next_steps/${id}/dismiss`, { method: "POST" }),

  listCodeFamilies: (rqId: number, includeDismissed = false) =>
    request<CodeFamily[]>(`/research_questions/${rqId}/code_families?include_dismissed=${includeDismissed}`),
  getCodeFamilyState: (rqId: number) => request<CodeFamilyState>(`/research_questions/${rqId}/code_families/state`),
  synthesizeCodeFamilies: (rqId: number, overrideCap = false) =>
    request<CodeFamilyState>(`/research_questions/${rqId}/code_families/synthesize?override_cap=${overrideCap}`, {
      method: "POST",
    }),
  acceptCodeFamilyMerge: (id: number) => request<CodeFamilyState>(`/code_family_merges/${id}/accept`, { method: "POST" }),
  dismissCodeFamilyMerge: (id: number) => request(`/code_family_merges/${id}/dismiss`, { method: "POST" }),
  updateCodeFamily: (id: number, body: { name?: string; definition?: string }) =>
    request<CodeFamily>(`/code_families/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  createCodeFamily: (rqId: number, body: { name: string; definition: string; connection_ids: number[] }) =>
    request<{ family: CodeFamily; cap: CodeFamilyCap }>(`/research_questions/${rqId}/code_families`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  setCodeFamilyMembers: (id: number, connectionIds: number[]) =>
    request<CodeFamily>(`/code_families/${id}/members`, {
      method: "PUT",
      body: JSON.stringify({ connection_ids: connectionIds }),
    }),
  starCodeFamily: (id: number, starred: boolean) =>
    request<CodeFamily>(`/code_families/${id}/star`, { method: "POST", body: JSON.stringify({ starred }) }),
  mergeCodeFamilies: (body: { family_ids: number[]; name: string; definition: string }) =>
    request<CodeFamilyMergeResult>("/code_families/merge", { method: "POST", body: JSON.stringify(body) }),
  acceptCodeFamily: (id: number) => request(`/code_families/${id}/accept`, { method: "POST" }),
  dismissCodeFamily: (id: number) => request(`/code_families/${id}/dismiss`, { method: "POST" }),

  getReviewPrompt: () => request<{ text: string; default_text: string }>("/settings/review-prompt"),
  saveReviewPrompt: (text: string) =>
    request(`/settings/review-prompt`, { method: "PUT", body: JSON.stringify({ text }) }),
  resetReviewPrompt: () => request<{ text: string }>(`/settings/review-prompt/reset`, { method: "POST" }),
};

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const audioUrlFor = (audioPath: string) => `${BASE}/audio/${audioPath.split("/").pop()}`;
