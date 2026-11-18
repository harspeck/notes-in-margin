export interface Me {
  data_source: "snapshot" | "cortico_api";
  user: { id: number; email: string | null; name: string | null };
  active_workspace: { id: number; name: string; organization_id: number; project_ids: number[] } | null;
}

export interface Organization {
  id: number;
  name: string | null;
  project_count: number;
}

export interface Project {
  id: number;
  name: string | null;
  description: string | null;
  organization_id: number;
  conversation_count?: number;
}

/** A named bundle of projects in one organization: what the conversation list is scoped to. */
export interface Workspace {
  id: number;
  name: string;
  organization_id: number;
  project_ids: number[];
  archived: boolean;
  active: boolean;
  updated_at: string | null;
}

export interface ConversationSummary {
  id: number;
  title: string;
  project_id: number;
  duration_seconds: number;
  language: string;
  audio_url: string | null;
  has_audio: boolean;
  start_time: string | null;
  num_participants: number | null;
}

export interface Word {
  word: string;
  start: number;
  end: number;
  confidence: number;
}

export interface Snippet {
  snippet_id: number;
  order_index: number;
  speaker_name: string | null;
  is_facilitator: boolean;
  start_offset: number;
  end_offset: number;
  text: string;
  words: Word[];
}

export interface GuideQuestion {
  guide_question_id: number;
  text: string;
  shorthand_code: string | null;
  order_index: number;
  checkpoint_snippet_id: number | null;
}

export interface ConversationDetail {
  id: number;
  title: string;
  project_id: number;
  audio_url: string | null;
  has_audio: boolean;
  duration_seconds: number;
  language: string;
  snippets: Snippet[];
  guide_questions: GuideQuestion[];
  transcript_state: { reached_end_at: string | null; prompt_dismissed_at: string | null };
}

export interface HighlightSnippetSpan {
  snippet_id: number;
  start_index: number;
  end_index: number;
  text?: string;
}

export interface Highlight {
  id: number;
  conversation_id: number;
  title: string | null;
  notes: string | null;
  starred: boolean;
  created_at: string;
  snippets: HighlightSnippetSpan[];
  /** The questions it is filed under, each resolved through question merges. */
  research_question_ids: number[];
  cortico_sync?: CorticoSync | null;
}

/** A highlight that exists in Cortico and was not made here. Read-only. */
export interface CorticoHighlight {
  cortico_id: number;
  title: string | null;
  description: string | null;
  author_name: string | null;
  /** The signed-in user made it, in Cortico or another tool. */
  mine: boolean;
  tool_id: string | null;
  created_at: string | null;
  start_offset: number;
  end_offset: number;
  snippets: HighlightSnippetSpan[];
}

// Mirrors `sync_status` in backend/app/cortico/write_back.py. Null in snapshot mode.
export interface CorticoSync {
  status: "pending" | "synced" | "skipped" | "failed" | "deleted";
  cortico_id: number | string | null;
  error: string | null;
}

export interface ResearchQuestion {
  id: number;
  name: string;
  description: string | null;
  display_order: number;
  archived: boolean;
  focused: boolean;
  merged_into: number | null;
  merged_sources: RQMergedSource[];
}

export interface RQMergedSource {
  merge_id: number;
  id: number;
  name: string | null;
  into_id: number;
}

export interface RQMerge {
  id: number;
  source_rq_id: number;
  target_rq_id: number;
  status: "active" | "undone";
  created_at: string;
  undone_at: string | null;
}

export interface RQMergeResult {
  merge: RQMerge;
  source: ResearchQuestion;
  target: ResearchQuestion;
}

export interface RQMergeUndoResult extends RQMergeResult {
  families_kept_with_target: { id: number; name: string }[];
}

export interface VoiceMemo {
  id: number;
  conversation_id: number;
  /** A reflection the user typed: no audio, no duration and no word timings. */
  typed: boolean;
  audio_path: string | null;
  duration: number | null;
  recorded_at: string;
  transcription_raw: string | null;
  transcription_edited: string | null;
  transcription_engine: string | null;
  transcription_progress: number;
  transcription_error: string | null;
  extracted_interests: string | null;
  status: "recorded" | "transcribing" | "ready_for_review" | "filed" | "failed";
}

export type EvidenceSource = "user" | "ai";

export interface ConnectionEvidence {
  id?: number;
  highlight_id: number | null;
  snippet_id: number;
  start_index: number;
  end_index: number;
  source: EvidenceSource;
  starred?: boolean;
  text?: string;
  /** The passage's own conversation, which differs from the entry's after an insight merge. */
  conversation_id?: number | null;
  conversation_title?: string | null;
}

export interface ReflectionConnection {
  id: number;
  research_question_id: number | null;
  insight_text: string;
  source: "user" | "ai";
  /** `merged`: folded into another insight. Neither filed nor set aside. */
  status: "suggested" | "accepted" | "edited" | "rejected" | "retracted" | "merged";
  edited_from_ai: boolean;
  ai_metadata: {
    model: string;
    reasoning_effort: string;
    confidence: string;
    rationale: string;
    reflection_timestamp: number | null;
  } | null;
  evidence: ConnectionEvidence[];
}

export interface StarredUnassignedHighlight {
  highlight_id: number;
  notes: string | null;
  snippets: HighlightSnippetSpan[];
}

export interface ReviewResult {
  connections: ReflectionConnection[];
  starred_unassigned: StarredUnassignedHighlight[];
}

export interface MemoMergedFrom {
  merge_id: number;
  connection_id: number;
  insight_text: string;
  merged_at: string | null;
}

export interface MemoEntry {
  connection_id: number;
  /** As stored: a merged question's own id, not its target's. */
  research_question_id: number | null;
  /** The reflection's conversation first, then any other its evidence comes from. */
  conversation_ids: number[];
  merged_from: MemoMergedFrom[];
  insight_text: string;
  source: "user" | "ai";
  edited_from_ai: boolean;
  conversation_id: number;
  conversation_title: string;
  voice_memo_id: number;
  voice_memo_audio_path: string | null;
  voice_memo_typed: boolean | null;
  reflection_timestamp: number | null;
  filed_at: string;
  evidence: ConnectionEvidence[];
}

export interface MemoSection {
  research_question_id: number;
  name: string;
  archived: boolean;
  merged_from: number[];
  entries: MemoEntry[];
}

export interface MemoStarredUnassignedGroup {
  conversation_id: number;
  conversation_title: string;
  highlights: StarredUnassignedHighlight[];
}

export interface MemoResponse {
  sections: MemoSection[];
  unmapped: MemoEntry[];
  starred_unassigned: MemoStarredUnassignedGroup[];
}

export interface NextStepSuggestion {
  id: number;
  research_question_id: number | null;
  suggested_direction: string | null;
  recommended_conversation_id: number | null;
  recommended_conversation_title: string | null;
  rationale: string;
  status: "suggested" | "accepted" | "dismissed";
  ai_metadata: { model: string; reasoning_effort: string; prompt: string } | null;
  created_at: string;
}

/** `POST /next_steps/recommend`. `reason` says why the AI was not asked: nothing summarised
 * to choose from, or it was asked a moment ago. */
export interface NextRecommendation {
  recommendation: NextStepSuggestion | null;
  generated: boolean;
  reason: "no_summaries" | "recent" | null;
}

/** A "what you want to hear next" line the AI offered and the user has not decided on. */
export interface ListeningSuggestion {
  id: number;
  text: string;
  reason: string | null;
  status: "suggested" | "accepted" | "dismissed";
  created_at: string;
}

export interface ListeningSuggestionRefresh {
  suggestions: ListeningSuggestion[];
  generated: boolean;
  reason: "undecided" | "nothing_to_go_on" | "unchanged" | "recent" | null;
}

/** Conversation summaries the AI recommends from, counted over the user's conversations. */
export interface GistStatus {
  total: number;
  with_gist: number;
  remaining: number;
}

export interface GistRun extends GistStatus {
  generated: number[];
  without_transcript: number[];
  failed: { conversation_id: number; detail: string }[];
}

export interface CodeFamilyMember {
  connection_id: number;
  insight_text: string;
}

export interface CodeFamily {
  id: number;
  research_question_id: number;
  name: string;
  definition: string;
  status: "suggested" | "accepted" | "dismissed" | "merged";
  starred: boolean;
  members: CodeFamilyMember[];
  ai_metadata: { model: string; reasoning_effort: string; prompt: string } | null;
  created_at: string;
}

export interface CodeFamilyMergeSuggestion {
  id: number;
  research_question_id: number;
  family_ids: number[];
  target_family_id: number;
  proposed_name: string;
  proposed_definition: string;
  rationale: string | null;
  status: "suggested" | "accepted" | "dismissed";
  created_at: string;
}

export interface CodeFamilyCap {
  live_count: number;
  max_per_rq: number;
  near_cap: boolean;
  at_cap: boolean;
}

export interface CodeFamilyState {
  families: CodeFamily[];
  merge_suggestions: CodeFamilyMergeSuggestion[];
  cap: CodeFamilyCap;
  withheld_new_families: number;
}

export interface CodeFamilyMergeResult extends CodeFamilyState {
  family: CodeFamily;
}

export interface OrganizeEntry {
  connection_id: number;
  insight_text: string;
  research_question_id: number;
  conversation_id: number | null;
  conversation_title: string | null;
  voice_memo_id: number;
  family_ids: number[];
  evidence: ConnectionEvidence[];
}

export interface OrganizeGroup {
  rq: ResearchQuestion;
  merged_sources: RQMergedSource[];
  entry_count: number;
  entries: OrganizeEntry[];
  families: CodeFamily[];
  cap: CodeFamilyCap;
  starred_unassigned: MemoStarredUnassignedGroup[];
}

export interface OrganizeResponse {
  focused: OrganizeGroup[];
  other: OrganizeGroup[];
  focus_count: number;
  focus_range: [number, number];
}

export interface CodebookEvidence {
  snippet_id: number;
  start_index: number;
  end_index: number;
  text: string;
  source: EvidenceSource;
}

export interface CodebookCode {
  family_id: number;
  name: string;
  definition: string;
  status: CodeFamily["status"];
  entries: { connection_id: number; insight_text: string; conversation_id: number | null; evidence: CodebookEvidence[] }[];
}

export interface CodebookPrep {
  generated_at: string;
  user_id: number;
  research_questions: {
    id: number;
    name: string;
    description: string | null;
    merged_from: number[];
    codes: CodebookCode[];
  }[];
  warnings: string[];
}

/** `PUT /conversations/{id}/reading_position`. */
export interface ReadingState {
  conversation_id: number;
  first_opened_at: string | null;
  last_opened_at: string | null;
  position_seconds: number | null;
  position_snippet_id: number | null;
}

export interface DeskReadingItem {
  conversation_id: number;
  title: string;
  project_id: number | null;
  /** Null in `cortico_api` mode, where project names are not cached. */
  project_name: string | null;
  start_time: string | null;
  duration_seconds: number | null;
  first_opened_at: string | null;
  last_opened_at: string | null;
  position_seconds: number | null;
  position_snippet_id: number | null;
  highlight_count: number;
  starred_count: number;
}

export interface DeskFinishItem {
  voice_memo_id: number;
  conversation_id: number;
  conversation_title: string;
  state: "undecided" | "not_reviewed" | "transcribing";
  undecided_count: number;
  typed: boolean;
  voice_memo_status: VoiceMemo["status"];
  created_at: string | null;
}

export interface DeskLastConversation {
  conversation_id: number;
  title: string;
  last_opened_at: string | null;
  position_seconds: number | null;
  position_snippet_id: number | null;
}

export interface Desk {
  keep_reading: DeskReadingItem[];
  finish: DeskFinishItem[];
  last_conversation: DeskLastConversation | null;
}

export interface ReviewDraftResponse {
  voice_memo_id: number;
  payload: Record<string, unknown> | null;
  updated_at: string | null;
}

export interface ListeningInterest {
  id: number;
  text: string;
  origin: string;
  voice_memo_id: number | null;
  status: "active" | "removed";
  created_at: string | null;
  updated_at: string | null;
}

export interface SetAsideItem {
  connection_id: number;
  kind: "retracted" | "rejected";
  insight_text: string;
  source: "user" | "ai";
  research_question_id: number | null;
  research_question_name: string | null;
  conversation_id: number;
  conversation_title: string | null;
  voice_memo_id: number;
  set_aside_at: string | null;
  /** `last_changed`: set aside before the time was recorded, so the date is the row's last change. */
  set_aside_at_source: "recorded" | "last_changed";
  restores_to: ReflectionConnection["status"];
}

export interface RestoreResult {
  id: number;
  status: ReflectionConnection["status"];
  restored_from: "retracted" | "rejected";
  voice_memo_id: number;
  in_memo: boolean;
}

export type ActivityFilter = "all" | "ai" | "changed" | "set_aside";

export interface ActivityItem {
  id: number;
  created_at: string | null;
  actor: "ai" | "user";
  kind: string;
  summary: string;
  /** Only the keys that apply, e.g. `conversation_id`, `voice_memo_id`, `research_question_id`. */
  refs: Record<string, number | string | null>;
}

export interface ActivityPage {
  items: ActivityItem[];
  next_before: number | null;
}

/** `PATCH /memo/connections/{id}`. `moved` is true when the body named `research_question_id`. */
export interface MemoEntryEditResult {
  id: number;
  status: ReflectionConnection["status"];
  research_question_id: number | null;
  moved: boolean;
  /** Code families of the old question that the entry left. */
  removed_from_family_ids: number[];
}

export interface InsightMerge {
  id: number;
  source_connection_id: number;
  target_connection_id: number;
  status: "active" | "undone";
  added_evidence_count: number;
  source_previous_status: ReflectionConnection["status"];
  created_at: string | null;
  undone_at: string | null;
}

/** `POST /memo/connections/{id}/merge_into` and `POST /insight_merges/{id}/undo`. */
export interface InsightMergeResult {
  merge: InsightMerge;
  source: { id: number; status: ReflectionConnection["status"] };
  target: { id: number; status: ReflectionConnection["status"]; evidence_count: number };
}

/** `GET /research_questions/{id}/highlights`: filed under the question, cited by no filed insight. */
export interface QuestionHighlight {
  highlight_id: number;
  conversation_id: number;
  conversation_title: string | null;
  title: string | null;
  notes: string | null;
  starred: boolean;
  created_at: string | null;
  research_question_ids: number[];
  snippets: Required<HighlightSnippetSpan>[];
  text: string;
}

export interface QuestionHighlights {
  research_question_id: number;
  highlights: QuestionHighlight[];
}

export interface BreadthQuestion {
  research_question_id: number;
  name: string;
  conversation_ids: number[];
  conversation_count: number;
  insight_conversation_ids: number[];
  highlight_only_conversation_ids: number[];
  project_ids_heard: number[];
  projects_heard_count: number;
  insight_count: number;
  highlight_count: number;
}

/** `GET /research_questions/breadth`. */
export interface Breadth {
  opened_conversation_ids: number[];
  opened_conversation_count: number;
  projects_in_scope: number[];
  projects_in_scope_count: number;
  questions: BreadthQuestion[];
  unfiled: {
    insight_count: number;
    conversation_ids: number[];
    conversation_count: number;
    project_ids_heard: number[];
    projects_heard_count: number;
  };
}

export interface GuideIdea {
  text: string;
  shorthand_code: string | null;
  conversation_count: number;
  project_ids: number[];
}

/** `GET /research_questions/guide_ideas`. */
export interface GuideIdeas {
  ideas: GuideIdea[];
  /** Projects whose guide wasn't read this time; a later request loads them. */
  projects_not_loaded: number[];
}

type AiMetadata = Record<string, unknown> | null;

export interface MergeNudgeQuestionItem {
  id: number;
  name: string;
  description: string | null;
  insight_count: number;
  highlight_count: number;
}

export interface MergeNudgeInsightItem {
  id: number;
  insight_text: string;
  conversation_id: number | null;
  conversation_title: string | null;
  evidence_count: number;
  conversation_count: number;
}

interface MergeNudgeBase {
  id: number;
  status: "suggested" | "accepted" | "dismissed";
  reason: string | null;
  default_target_id: number | null;
  research_question_id: number | null;
  research_question_name: string | null;
  result_merge_id: number | null;
  ai_metadata: AiMetadata;
  created_at: string | null;
}

export interface QuestionMergeNudge extends MergeNudgeBase {
  kind: "questions";
  /** A wording for the merged question. */
  suggested_wording: string | null;
  items: MergeNudgeQuestionItem[];
  merged_preview: null;
}

export interface InsightMergeNudge extends MergeNudgeBase {
  kind: "insights";
  suggested_wording: string | null;
  items: MergeNudgeInsightItem[];
  merged_preview: { evidence_count: number; conversation_count: number } | null;
}

export type MergeNudge = QuestionMergeNudge | InsightMergeNudge;

/** `POST /merge_nudges/{id}/accept`. `merge.id` is what the matching undo route takes. */
export interface MergeNudgeAcceptResult {
  id: number;
  status: MergeNudge["status"];
  kind: MergeNudge["kind"];
  source_id: number;
  target_id: number;
  merge: { id: number; kind: MergeNudge["kind"] };
}

export interface PassageSuggestion {
  id: number;
  research_question_id: number;
  conversation_id: number;
  conversation_title: string | null;
  snippet_id: number;
  start_index: number;
  end_index: number;
  speaker_name: string | null;
  is_facilitator: boolean | null;
  time_seconds: number | null;
  snippet_start_offset: number | null;
  text: string;
  reason: string | null;
  status: "suggested" | "accepted" | "dismissed";
  highlight_id: number | null;
  ai_metadata: AiMetadata;
  created_at: string | null;
}

/** `POST /research_questions/{id}/find_passages`. */
export interface FindPassagesResult {
  research_question_id: number;
  searched_conversation_ids: number[];
  failed: { conversation_id: number; detail: string }[];
  suggestions: PassageSuggestion[];
}

export interface PassageSuggestions {
  research_question_id: number;
  suggestions: PassageSuggestion[];
}

export interface PassageAcceptResult {
  suggestion: PassageSuggestion;
  highlight: Highlight;
}

export interface QuestionGapConversation {
  conversation_id: number;
  title: string | null;
  in_listen_next: boolean;
  next_step_id: number | null;
}

export interface QuestionGap {
  id: number;
  research_question_id: number;
  kind: "perspective" | "counter_view" | "thin_spot";
  observation: string;
  /** Unopened conversations the observation names. */
  conversations: QuestionGapConversation[];
  status: "suggested" | "dismissed";
  ai_metadata: AiMetadata;
  created_at: string | null;
}

export interface QuestionGaps {
  research_question_id: number;
  gaps: QuestionGap[];
}

export interface GapListenNextResult {
  gap: QuestionGap;
  next_steps: (NextStepSuggestion & { created: boolean })[];
}
