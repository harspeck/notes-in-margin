import type {
  CorticoHighlight,
  ActivityFilter,
  ActivityPage,
  Breadth,
  CodebookPrep,
  CodeFamily,
  CodeFamilyCap,
  CodeFamilyMergeResult,
  CodeFamilyState,
  ConnectionEvidence,
  ConversationDetail,
  ConversationSummary,
  Desk,
  FindPassagesResult,
  GapListenNextResult,
  GuideIdeas,
  Highlight,
  InsightMergeResult,
  GistRun,
  GistStatus,
  ListeningInterest,
  ListeningSuggestion,
  ListeningSuggestionRefresh,
  NextRecommendation,
  Me,
  MemoEntryEditResult,
  MemoResponse,
  MergeNudge,
  MergeNudgeAcceptResult,
  NextStepSuggestion,
  Organization,
  OrganizeResponse,
  PassageAcceptResult,
  PassageSuggestions,
  Project,
  QuestionGaps,
  QuestionHighlights,
  ReadingState,
  Workspace,
  ResearchQuestion,
  RestoreResult,
  ReviewDraftResponse,
  ReviewResult,
  RQMergeResult,
  RQMergeUndoResult,
  SetAsideItem,
  VoiceMemo,
} from "./types";

// Deployed, the backend is reached through the `/api` rewrite in `frontend/vercel.json`, so the
// session cookie is first-party and no CORS setting is involved. Locally it is the backend's port.
const BASE: string = import.meta.env.VITE_API_BASE ?? (import.meta.env.PROD ? "/api" : "http://localhost:8000");

export const SIGNED_OUT_EVENT = "nim-signed-out";
/** Fired when what `GET /desk` returns has changed (a conversation was opened), so the header re-reads it. */
export const DESK_CHANGED_EVENT = "nim-desk-changed";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: init?.body && !(init.body instanceof FormData) ? { "content-type": "application/json" } : undefined,
    credentials: "include",
    ...init,
  });
  if (res.status === 401 && path !== "/auth/login") window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${path}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  me: () => request<Me>("/auth/me"),
  login: (email: string, password: string) =>
    request<{ user: Me["user"] }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request("/auth/logout", { method: "POST" }),

  listOrganizations: () => request<Organization[]>("/organizations"),
  listProjects: (organizationId: number) => request<Project[]>(`/organizations/${organizationId}/projects`),
  listWorkspaces: () => request<Workspace[]>("/workspaces"),
  createWorkspace: (body: { name: string; organization_id: number; project_ids: number[] }) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(body) }),
  activateWorkspace: (id: number) => request<Workspace>(`/workspaces/${id}/activate`, { method: "POST" }),

  listConversations: () => request<ConversationSummary[]>("/conversations"),
  getConversation: (id: number) => request<ConversationDetail>(`/conversations/${id}`),
  setTranscriptState: (id: number, body: { reached_end_at?: boolean; prompt_dismissed?: boolean }) =>
    request(`/conversations/${id}/state`, { method: "PUT", body: JSON.stringify(body) }),
  /** With no body this only records that the conversation was opened. A field left out keeps its stored value. */
  setReadingPosition: (id: number, body?: { position_seconds?: number; snippet_id?: number }, keepalive = false) =>
    request<ReadingState>(`/conversations/${id}/reading_position`, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
      keepalive,
    }),
  getDesk: () => request<Desk>("/desk"),

  listHighlights: (conversationId: number) => request<Highlight[]>(`/conversations/${conversationId}/highlights`),
  listCorticoHighlights: (conversationId: number) =>
    request<CorticoHighlight[]>(`/conversations/${conversationId}/cortico_highlights`),
  createHighlight: (
    conversationId: number,
    body: { title?: string; notes?: string; snippets: { snippet_id: number; start_index: number; end_index: number }[] }
  ) => request<Highlight>(`/conversations/${conversationId}/highlights`, { method: "POST", body: JSON.stringify(body) }),
  updateHighlight: (id: number, body: { notes?: string; starred?: boolean; title?: string }) =>
    request<Highlight>(`/highlights/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  /** Replaces the set of questions the highlight is filed under. */
  setHighlightQuestions: (id: number, researchQuestionIds: number[]) =>
    request<Highlight>(`/highlights/${id}/questions`, {
      method: "PUT",
      body: JSON.stringify({ research_question_ids: researchQuestionIds }),
    }),
  deleteHighlight: (id: number) => request(`/highlights/${id}`, { method: "DELETE" }),
  retryHighlightSync: (id: number) => request<Highlight>(`/highlights/${id}/cortico_sync`, { method: "POST" }),

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

  getBreadth: () => request<Breadth>("/research_questions/breadth"),
  getGuideIdeas: () => request<GuideIdeas>("/research_questions/guide_ideas"),
  listQuestionHighlights: (rqId: number) => request<QuestionHighlights>(`/research_questions/${rqId}/highlights`),

  /** Paid. */
  generateMergeNudges: () => request<MergeNudge[]>("/merge_nudges/generate", { method: "POST" }),
  listMergeNudges: () => request<MergeNudge[]>("/merge_nudges"),
  acceptMergeNudge: (id: number, body: { target_id?: number; apply_suggested_wording?: boolean } = {}) =>
    request<MergeNudgeAcceptResult>(`/merge_nudges/${id}/accept`, { method: "POST", body: JSON.stringify(body) }),
  dismissMergeNudge: (id: number) =>
    request<{ id: number; status: MergeNudge["status"] }>(`/merge_nudges/${id}/dismiss`, { method: "POST" }),

  /** Paid: one model call per conversation, so a caller names one conversation per request. */
  findPassages: (rqId: number, conversationId?: number) =>
    request<FindPassagesResult>(`/research_questions/${rqId}/find_passages`, {
      method: "POST",
      body: JSON.stringify(conversationId === undefined ? {} : { conversation_id: conversationId }),
    }),
  listPassageSuggestions: (rqId: number, conversationId?: number) =>
    request<PassageSuggestions>(
      `/research_questions/${rqId}/passage_suggestions${conversationId === undefined ? "" : `?conversation_id=${conversationId}`}`
    ),
  acceptPassageSuggestion: (id: number) =>
    request<PassageAcceptResult>(`/passage_suggestions/${id}/accept`, { method: "POST" }),
  dismissPassageSuggestion: (id: number) =>
    request<{ id: number; status: "dismissed" }>(`/passage_suggestions/${id}/dismiss`, { method: "POST" }),

  /** Paid. */
  runWhatsMissing: (rqId: number) => request<QuestionGaps>(`/research_questions/${rqId}/whats_missing`, { method: "POST" }),
  listQuestionGaps: (rqId: number) => request<QuestionGaps>(`/research_questions/${rqId}/gaps`),
  dismissQuestionGap: (id: number) =>
    request<{ id: number; status: "dismissed" }>(`/question_gaps/${id}/dismiss`, { method: "POST" }),
  gapListenNext: (id: number, conversationIds?: number[]) =>
    request<GapListenNextResult>(`/question_gaps/${id}/listen_next`, {
      method: "POST",
      body: JSON.stringify(conversationIds ? { conversation_ids: conversationIds } : {}),
    }),

  getOrganize: () => request<OrganizeResponse>("/organize"),
  getCodebookPrep: () => request<CodebookPrep>("/codebook_prep"),

  uploadVoiceMemo: (conversationId: number, audio: Blob, duration: number) => {
    const form = new FormData();
    form.append("audio", audio, "memo.webm");
    form.append("duration", String(duration));
    return request<VoiceMemo>(`/conversations/${conversationId}/voice_memos`, { method: "POST", body: form });
  },
  createTypedReflection: (conversationId: number, text: string) =>
    request<VoiceMemo>(`/conversations/${conversationId}/reflections`, { method: "POST", body: JSON.stringify({ text }) }),
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
  getReviewDraft: (voiceMemoId: number) => request<ReviewDraftResponse>(`/voice_memos/${voiceMemoId}/review_draft`),
  saveReviewDraft: (voiceMemoId: number, payload: object, keepalive = false) =>
    request<ReviewDraftResponse>(`/voice_memos/${voiceMemoId}/review_draft`, {
      method: "PUT",
      body: JSON.stringify({ payload }),
      keepalive,
    }),
  deleteReviewDraft: (voiceMemoId: number, keepalive = false) =>
    request<{ deleted: boolean }>(`/voice_memos/${voiceMemoId}/review_draft`, { method: "DELETE", keepalive }),

  getMemo: () => request<MemoResponse>("/memo"),
  editMemoEntry: (connectionId: number, body: { insight_text?: string; evidence?: ConnectionEvidence[] }) =>
    request<MemoEntryEditResult>(`/memo/connections/${connectionId}`, { method: "PATCH", body: JSON.stringify(body) }),
  /** `null` moves it to "doesn't fit a question yet". */
  moveMemoEntry: (connectionId: number, researchQuestionId: number | null) =>
    request<MemoEntryEditResult>(`/memo/connections/${connectionId}`, {
      method: "PATCH",
      body: JSON.stringify({ research_question_id: researchQuestionId }),
    }),
  mergeMemoEntry: (connectionId: number, targetConnectionId: number) =>
    request<InsightMergeResult>(`/memo/connections/${connectionId}/merge_into`, {
      method: "POST",
      body: JSON.stringify({ target_connection_id: targetConnectionId }),
    }),
  undoInsightMerge: (mergeId: number) => request<InsightMergeResult>(`/insight_merges/${mergeId}/undo`, { method: "POST" }),
  retractMemoEntry: (connectionId: number) => request(`/memo/connections/${connectionId}/retract`, { method: "POST" }),
  listSetAside: () => request<{ items: SetAsideItem[] }>("/memo/set_aside"),
  restoreConnection: (connectionId: number) =>
    request<RestoreResult>(`/memo/connections/${connectionId}/restore`, { method: "POST" }),

  listListeningInterests: () => request<ListeningInterest[]>("/listening_interests"),
  addListeningInterest: (text: string) =>
    request<ListeningInterest>("/listening_interests", { method: "POST", body: JSON.stringify({ text }) }),
  editListeningInterest: (id: number, text: string) =>
    request<ListeningInterest>(`/listening_interests/${id}`, { method: "PATCH", body: JSON.stringify({ text }) }),
  removeListeningInterest: (id: number) =>
    request<{ id: number; status: "removed" }>(`/listening_interests/${id}`, { method: "DELETE" }),

  listListeningSuggestions: () => request<ListeningSuggestion[]>("/listening_interests/suggestions"),
  /** May ask the AI: the server decides, and answers from its cache when nothing changed. */
  refreshListeningSuggestions: () =>
    request<ListeningSuggestionRefresh>("/listening_interests/suggestions/refresh", { method: "POST" }),
  acceptListeningSuggestion: (id: number) =>
    request<{ suggestion: ListeningSuggestion; interest: ListeningInterest }>(
      `/listening_interests/suggestions/${id}/accept`,
      { method: "POST" }
    ),
  dismissListeningSuggestion: (id: number) =>
    request<ListeningSuggestion>(`/listening_interests/suggestions/${id}/dismiss`, { method: "POST" }),

  getGistStatus: () => request<GistStatus>("/gists/status"),
  /** Paid: summarises a few conversations per call. Call again until `remaining` is 0. */
  generateGists: () => request<GistRun>("/gists/generate", { method: "POST" }),

  listActivity: (filter: ActivityFilter = "all", before: number | null = null, limit = 50) =>
    request<ActivityPage>(`/activity?filter=${filter}&limit=${limit}${before === null ? "" : `&before=${before}`}`),

  /** May ask the AI: only when nothing is already suggested. */
  recommendNext: () => request<NextRecommendation>("/next_steps/recommend", { method: "POST" }),
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

/** The status code `request` puts at the front of its error message, or null for a network failure. */
export const errorStatus = (e: unknown) => {
  const status = Number(errorText(e).slice(0, 3));
  return Number.isInteger(status) && status >= 400 ? status : null;
};

export const audioUrlFor = (audioPath: string) => `${BASE}/audio/${audioPath.split("/").pop()}`;
