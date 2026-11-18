import { createContext, useContext } from "react";
import type { Breadth, BreadthQuestion } from "./types";

/** A way back offered inside the toast, such as "Undo". */
export interface ToastAction {
  label: string;
  run: () => void;
}

export const ToastContext = createContext<(text: string, action?: ToastAction) => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function clock(totalSeconds: number) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export function minutes(totalSeconds: number) {
  return `${Math.max(1, Math.round(totalSeconds / 60))} min`;
}

export function shortDate(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** When an insight was set aside. A `last_changed` time is the row's last change, not the
 * moment it was set aside, so it is worded as a bound and never as the exact date. */
export function setAsideDate(item: { set_aside_at: string | null; set_aside_at_source: string } | undefined) {
  const date = shortDate(item?.set_aside_at);
  if (!date) return "";
  return item?.set_aside_at_source === "recorded" ? `Set aside ${date}` : `Set aside on or before ${date}`;
}

export const plural =(n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

/** How widely a question was heard. Every count comes from `GET /research_questions/breadth`:
 * `heard` is the question's `conversation_count` and `opened` the conversations the user has opened. */
export function heardLine(heard: number, opened: number) {
  return heard ? `Heard in ${heard} of ${plural(opened, "conversation")}` : "Not heard in anything yet";
}

export function breadthWord(heard: number, opened: number) {
  if (!heard) return "New";
  // With only a couple of conversations opened, "broad" or "narrow" would be a guess.
  if (opened < 3) return "Too early to tell";
  if (heard / opened >= 0.5) return "Broad";
  if (heard >= 3) return "Comes up in some places";
  return "Narrow so far";
}

/** The line under a question's name in the Desk, notebook and Questions lists. */
export function questionRowLine(q: BreadthQuestion, opened: number) {
  const counts =
    q.insight_count === 0 && q.highlight_count > 0
      ? `${plural(q.highlight_count, "highlight")}, no insight yet`
      : plural(q.insight_count, "insight");
  return `${heardLine(q.conversation_count, opened)} · ${counts}`;
}

/** The sentence beside the gauge on a question's own page. */
export function breadthSentence(q: BreadthQuestion, breadth: Breadth) {
  if (!q.conversation_count) return "Nothing you've opened touches it yet.";
  const scope = breadth.projects_in_scope_count;
  const across =
    scope > 1
      ? `, across ${q.projects_heard_count === scope ? `all ${scope}` : `${q.projects_heard_count} of ${scope}`} projects`
      : "";
  const only = q.highlight_only_conversation_ids.length;
  const onlyLine =
    only === 0
      ? ""
      : q.insight_conversation_ids.length === 0
        ? " So far it came up in highlights only, with no insight yet."
        : ` In ${only} of them it came up in highlights only.`;
  return `Heard in ${q.conversation_count} of the ${plural(breadth.opened_conversation_count, "conversation")} you've opened${across}.${onlyLine}`;
}

export type Provenance = "ai" | "kept" | "both" | "yours";
