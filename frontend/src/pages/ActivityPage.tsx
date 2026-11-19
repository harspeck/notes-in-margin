import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { shortDate } from "../lib/format";
import type { ActivityFilter, ActivityItem } from "../lib/types";

const FILTERS: { value: ActivityFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "ai", label: "Claude suggested" },
  { value: "changed", label: "You changed" },
  { value: "set_aside", label: "You set aside" },
];

// Mirror the `CHANGED` and `SET_ASIDE` buckets of `KINDS` in backend/app/services/activity.py,
// because a row doesn't say which filter it falls under. A kind missing here is tagged "You".
const CHANGED_KINDS = new Set([
  "insight_edited",
  "memo_entry_edited",
  "rq_reworded",
  "rq_archived",
  "rq_restored",
  "rq_merged",
  "rq_merge_undone",
  "memo_entry_moved",
  "insights_merged",
  "insight_merge_undone",
  "team_theme_edited",
]);
const SET_ASIDE_KINDS = new Set([
  "insight_set_aside",
  "memo_entry_retracted",
  "next_step_dismissed",
  "listening_line_dismissed",
  "code_family_dismissed",
  "merge_nudge_dismissed",
  "passage_dismissed",
  "question_gap_dismissed",
  "team_group_dismissed",
  "team_theme_dismissed",
]);

function KindTag({ item }: { item: ActivityItem }) {
  if (item.actor === "ai") return <span className="tag ai">Claude suggested</span>;
  if (SET_ASIDE_KINDS.has(item.kind)) return <span className="tag both">You set aside</span>;
  if (CHANGED_KINDS.has(item.kind)) return <span className="tag yours">You changed</span>;
  return <span className="tag kept">You</span>;
}

function RefLinks({ refs }: { refs: ActivityItem["refs"] }) {
  const id = (key: string) => (typeof refs[key] === "number" ? refs[key] : null);
  const voiceMemoId = id("voice_memo_id");
  const conversationId = id("conversation_id");
  const questionId = id("research_question_id");
  if (voiceMemoId === null && conversationId === null && questionId === null) return null;
  return (
    <span className="refs">
      {voiceMemoId !== null && (
        <Link className="linkb" to={`/review/${voiceMemoId}`}>
          Open the review
        </Link>
      )}
      {conversationId !== null && (
        <Link className="linkb" to={`/read/${conversationId}`}>
          Open the conversation
        </Link>
      )}
      {questionId !== null && (
        <Link className="linkb" to={`/questions?q=${questionId}`}>
          Open the question
        </Link>
      )}
    </span>
  );
}

function Log({ filter }: { filter: ActivityFilter }) {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (before: number | null) =>
      api
        .listActivity(filter, before)
        .then((page) => {
          setItems((prev) => (before === null ? page.items : [...(prev ?? []), ...page.items]));
          setNextBefore(page.next_before);
          setError(null);
        })
        .catch((e) => setError(errorText(e)))
        .finally(() => setLoading(false)),
    [filter]
  );

  useEffect(() => {
    load(null);
  }, [load]);

  function more(before: number | null) {
    setLoading(true);
    load(before);
  }

  return (
    <>
      {error && (
        <div className="err" role="alert">
          The activity log couldn't be loaded ({error}).{" "}
          <button className="linkb" onClick={() => more(items ? nextBefore : null)}>
            Try again
          </button>
        </div>
      )}
      {items === null && !error && <p className="hint">Loading…</p>}
      {items?.length === 0 && (
        <p className="hint" style={{ padding: "14px 0" }}>
          {filter === "all"
            ? "Nothing here yet. What Claude suggests and what you decide is listed here as it happens."
            : "Nothing here yet."}
        </p>
      )}
      <ul className="loglist">
        {items?.map((item) => (
          <li key={item.id}>
            <span className="d">{shortDate(item.created_at)}</span>
            <span className="what">
              {item.summary}
              <RefLinks refs={item.refs} />
            </span>
            <KindTag item={item} />
          </li>
        ))}
      </ul>
      {items !== null && nextBefore !== null && (
        <button className="btn outline small" style={{ marginTop: 16 }} disabled={loading} onClick={() => more(nextBefore)}>
          {loading ? "Loading…" : "Show more"}
        </button>
      )}
    </>
  );
}

export default function ActivityPage() {
  const [filter, setFilter] = useState<ActivityFilter>("all");
  return (
    <div className="wrap narrow">
      <h1 className="h1">activity</h1>
      <p className="lede">Everything Claude suggested and everything you decided, including what you left out.</p>
      <div className="filters" role="group" aria-label="Show">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            className="btn outline small"
            aria-pressed={filter === f.value}
            onClick={() => setFilter(f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>
      <Log key={filter} filter={filter} />
    </div>
  );
}
