import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { shortDate } from "../lib/format";
import { personCounts, questionCounts } from "../lib/team";
import type { TeamPersonDetail } from "../lib/types";

function Person({ userId }: { userId: number }) {
  const [data, setData] = useState<TeamPersonDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .getTeamPerson(userId)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e) => setError(errorText(e))),
    [userId]
  );

  useEffect(() => {
    load();
  }, [load]);

  if (!data)
    return error ? (
      <div className="wrap narrow">
        <div className="err" role="alert">
          This person's work couldn't be loaded ({error}).{" "}
          <button className="linkb" onClick={load}>
            Try again
          </button>
        </div>
        <Link to="/team">Back to the team page</Link>
      </div>
    ) : (
      <p className="loading">Loading…</p>
    );

  const { person } = data;
  return (
    <div className="wrap narrow team">
      <p className="eyebrow">
        <Link to="/team">Team</Link>
      </p>
      <h1 className="h1 md">
        {person.name} {person.is_you && <span className="tag yours">you</span>}
      </h1>
      <p className="lede">
        {person.is_you
          ? "What your teammates can see of your work on the conversations in this workspace."
          : "Their reflections and questions on the conversations in this workspace, for reading only."}
      </p>
      <p className="hint" style={{ marginTop: 8 }}>
        {personCounts(person)}
      </p>

      <section aria-labelledby="person-reflections">
        <h2 className="h2 rule" id="person-reflections">
          Reflections <span className="n">{data.reflections.length}</span>
        </h2>
        {data.reflections.length === 0 && <p className="hint">No reflections on these conversations yet.</p>}
        {data.reflections.map((r) => (
          <article className="treflect" key={r.voice_memo_id}>
            <div className="byline">
              <Link to={`/read/${r.conversation_id}`}>{r.conversation_title ?? `Conversation ${r.conversation_id}`}</Link>
              <span>{[shortDate(r.recorded_at), r.typed ? "written" : "spoken"].filter(Boolean).join(" · ")}</span>
            </div>
            <p>{r.text || "This reflection has no text."}</p>
          </article>
        ))}
      </section>

      <section aria-labelledby="person-questions">
        <h2 className="h2 rule" id="person-questions">
          Questions <span className="n">{data.questions.length}</span>
        </h2>
        {data.questions.length === 0 && <p className="hint">No questions on these conversations yet.</p>}
        <ul>
          {data.questions.map((q) => (
            <li className="tq" key={q.research_question_id}>
              <h3>{q.name}</h3>
              {q.description && <p className="hint">{q.description}</p>}
              <p className="byline">
                <span>{questionCounts(q)}</span>
                {q.group_id !== null && <Link to={`/team/codebook/${q.group_id}`}>In the team codebook</Link>}
              </p>
            </li>
          ))}
        </ul>
        {data.questions.length > 0 && (
          <p className="hint" style={{ marginTop: 12 }}>
            What is filed under each question is on the <Link to="/team">team page</Link>.
          </p>
        )}
      </section>
    </div>
  );
}

export default function TeamPersonPage() {
  const userId = Number(useParams().userId);
  if (!Number.isInteger(userId))
    return (
      <div className="wrap narrow">
        <p className="err">That isn't a person on this team.</p>
        <Link to="/team">Back to the team page</Link>
      </div>
    );
  return <Person key={userId} userId={userId} />;
}
