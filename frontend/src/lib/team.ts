import { plural } from "./format";
import type { TeamPerson, TeamQuestion } from "./types";

/** Moves focus once the reloaded list has rendered, for when an action removed the focused control. */
export function focusLater(elementId: string) {
  setTimeout(() => document.getElementById(elementId)?.focus(), 0);
}

export const questionCounts = (q: TeamQuestion) =>
  [
    plural(q.insight_count, "insight"),
    plural(q.highlight_count, "highlight"),
    plural(q.conversation_count, "conversation"),
  ].join(" · ");

export const personCounts = (p: TeamPerson) =>
  [
    `Opened ${plural(p.conversations_opened, "conversation")}`,
    plural(p.highlight_count, "highlight"),
    plural(p.reflection_count, "reflection"),
    plural(p.insight_count, "insight"),
    plural(p.question_count, "question"),
  ].join(" · ");

/** The server's own sentence for a refused write, without the status and path api.ts puts in front. */
export function teamError(e: unknown): string {
  const message = e instanceof Error ? e.message : String(e);
  const body = message.slice(message.indexOf(": ") + 2);
  try {
    const detail = (JSON.parse(body) as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  } catch {
    // Not a JSON body: a network failure, shown as it is.
  }
  return message;
}
