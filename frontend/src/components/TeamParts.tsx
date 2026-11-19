import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../lib/api";
import type { TeamAuthor, TeamInsight, TeamQuestionDetail } from "../lib/types";
import { Icon } from "../lib/ui";

export function Byline({ author }: { author: TeamAuthor }) {
  return (
    <span className="byline">
      {author.name}
      {author.is_you && <span className="tag yours">you</span>}
    </span>
  );
}

/** A filed insight with the passages it quotes, each linked to its own conversation. */
export function TeamInsightCard({ insight }: { insight: TeamInsight }) {
  return (
    <div className="ins">
      <div className="instop">
        <h3>{insight.insight_text}</h3>
      </div>
      <p className="fit">
        <Byline author={insight.author} />
      </p>
      {insight.evidence.length > 0 && (
        <ul className="receipts" style={{ marginTop: 14 }}>
          {insight.evidence.map((e) => (
            <li key={`${e.snippet_id}-${e.start_index}-${e.end_index}`}>
              "{e.text || "This passage's text couldn't be loaded."}"
              <div className="src">
                {e.conversation_title ?? `Conversation ${e.conversation_id}`}
                <Link className="linkb" to={`/read/${e.conversation_id}?snippet=${e.snippet_id}`}>
                  Open in transcript
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** What the team filed under one question. Mounted when its disclosure opens, so it loads then. */
export function TeamQuestionWork({ rqId }: { rqId: number }) {
  const [data, setData] = useState<TeamQuestionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .getTeamQuestion(rqId)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e) => setError(errorText(e))),
    [rqId]
  );

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <div className="err" role="alert">
        What's filed under this question couldn't be loaded ({error}).{" "}
        <button className="linkb" onClick={load}>
          Try again
        </button>
      </div>
    );
  if (!data) return <p className="hint">Loading…</p>;
  if (data.insights.length === 0 && data.highlights.length === 0)
    return <p className="hint">Nothing is filed under this question on these conversations yet.</p>;
  return (
    <>
      {data.insights.length > 0 && (
        <>
          <h4>Insights</h4>
          {data.insights.map((i) => (
            <TeamInsightCard key={i.connection_id} insight={i} />
          ))}
        </>
      )}
      {data.highlights.length > 0 && (
        <>
          <h4>Highlights</h4>
          <ul className="receipts">
            {data.highlights.map((h) => (
              <li className={h.author.is_you ? "yours" : undefined} key={h.highlight_id}>
                "{h.text || "This highlight's text couldn't be loaded."}"
                {h.notes && <p className="fromsaid">Note: {h.notes}</p>}
                <div className="src">
                  {h.starred && (
                    <span role="img" aria-label="Starred" title="Starred">
                      <Icon name="star" fill />
                    </span>
                  )}
                  <Byline author={h.author} />
                  <Link className="linkb" to={`/read/${h.conversation_id}`}>
                    {h.conversation_title ?? `Conversation ${h.conversation_id}`}
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
