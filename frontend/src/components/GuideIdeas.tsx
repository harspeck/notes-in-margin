import { useCallback, useEffect, useState } from "react";
import { api, errorText } from "../lib/api";
import { plural } from "../lib/format";
import type { GuideIdeas as GuideIdeasData, ResearchQuestion } from "../lib/types";

const SHOWN_FIRST = 4;

/** Starting questions drawn from the conversation guides of the projects in scope. With
 * `emptyState` it also says so when there is nothing to offer, for a list with no questions. */
export default function GuideIdeas({
  onAdded,
  emptyState = false,
}: {
  onAdded: (rq: ResearchQuestion) => void;
  emptyState?: boolean;
}) {
  const [data, setData] = useState<GuideIdeasData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(
    () =>
      api
        .getGuideIdeas()
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e) => setError(errorText(e)))
        .finally(() => setLoading(false)),
    []
  );

  useEffect(() => {
    load();
  }, [load]);

  function loadMore() {
    setLoading(true);
    load();
  }

  async function add(text: string) {
    setAdding(text);
    try {
      const rq = await api.createResearchQuestion({ name: text });
      setData((d) => (d ? { ...d, ideas: d.ideas.filter((i) => i.text !== text) } : d));
      setError(null);
      onAdded(rq);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setAdding(null);
    }
  }

  const ideas = data?.ideas ?? [];
  const notLoaded = data?.projects_not_loaded.length ?? 0;
  if (ideas.length === 0 && notLoaded === 0 && !error) {
    if (!emptyState) return null;
    return (
      <p className="hint">
        {loading ? "Looking in your conversation guides…" : "No questions yet. Jot one below. It doesn't have to be good."}
      </p>
    );
  }

  return (
    <div className="card ai guide">
      <p className="eyebrow">
        <span className="tag ai">Idea</span> From your conversation guide
      </p>
      <p className="hint" style={{ marginBottom: 8 }}>
        What the guide's authors were curious about, as starting questions.
      </p>
      {error && <p className="err" role="alert">{error}</p>}
      <ul className="merged">
        {ideas.slice(0, showAll ? ideas.length : SHOWN_FIRST).map((idea) => (
          <li key={idea.text}>
            {idea.text}{" "}
            <span className="hint">Asked in {plural(idea.conversation_count, "conversation")}.</span>{" "}
            <button
              className="linkb"
              onClick={() => add(idea.text)}
              disabled={adding !== null}
              aria-label={`Add as a question: ${idea.text}`}
            >
              {adding === idea.text ? "Adding…" : "Add as a question"}
            </button>
          </li>
        ))}
      </ul>
      {(ideas.length > SHOWN_FIRST && !showAll) || notLoaded > 0 ? (
        <div className="acts" style={{ marginTop: 8 }}>
          {ideas.length > SHOWN_FIRST && !showAll && (
            <button className="btn quiet small" onClick={() => setShowAll(true)}>
              Show {ideas.length - SHOWN_FIRST} more
            </button>
          )}
          {notLoaded > 0 && (
            <>
              <button className="btn outline small" onClick={loadMore} disabled={loading}>
                {loading ? "Loading…" : "Load more"}
              </button>
              <span className="hint">The guides of {plural(notLoaded, "more project")} haven't been read yet.</span>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
