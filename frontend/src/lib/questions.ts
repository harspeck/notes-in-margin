import type { MemoEntry, MemoResponse, OrganizeGroup, OrganizeResponse, ResearchQuestion } from "./types";

export interface QuestionView {
  rq: ResearchQuestion;
  group: OrganizeGroup;
  entries: MemoEntry[];
}

export interface QuestionsData {
  questions: QuestionView[];
  unmapped: MemoEntry[];
}

/** `GET /organize` knows the questions, their merges and code families; `GET /memo` knows who
 * wrote each insight. Both read a merged question under its target, so they join on the id.
 * How widely each was heard is not computed here: it comes from `GET /research_questions/breadth`. */
export function buildQuestions(organize: OrganizeResponse, memo: MemoResponse): QuestionsData {
  const sectionById = new Map(memo.sections.map((s) => [s.research_question_id, s]));
  const groups = [...organize.focused, ...organize.other].sort(
    (a, b) => a.rq.display_order - b.rq.display_order || a.rq.id - b.rq.id
  );
  return {
    questions: groups.map((group) => ({ rq: group.rq, group, entries: sectionById.get(group.rq.id)?.entries ?? [] })),
    unmapped: memo.unmapped,
  };
}
