import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { plural, useToast } from "../lib/format";
import { focusLater, personCounts, questionCounts, teamError } from "../lib/team";
import type { TeamOverview, TeamQuestion, TeamQuestionGroup } from "../lib/types";
import { AiMark } from "../lib/ui";
import { Byline, PickThenAct, TeamQuestionWork } from "../components/TeamParts";

const QUESTIONS_HEADING = "team-questions";

function QuestionRow({
  question,
  others,
  groups,
  busy,
  onAdopt,
  onSameAs,
}: {
  question: TeamQuestion;
  others: TeamQuestion[];
  groups: TeamQuestionGroup[];
  busy: boolean;
  onAdopt: () => void;
  /** `target` is `g:<group id>` or `q:<question id>`. */
  onSameAs: (target: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const id = question.research_question_id;
  return (
    <li className="tq">
      <h3 id={`team-q-${id}`} tabIndex={-1}>
        {question.name}
      </h3>
      {question.description && <p className="hint">{question.description}</p>}
      <p className="byline">
        <Byline author={question.author} />
        <span>{questionCounts(question)}</span>
      </p>
      <div className="acts">
        <button
          className="btn outline small"
          aria-expanded={open}
          aria-controls={`team-q-work-${id}`}
          onClick={() => setOpen(!open)}
        >
          {open ? "Hide what's filed here" : "Show what's filed here"}
        </button>
        {!question.author.is_you &&
          (question.adopted_by_you ? (
            <span className="hint">In your questions</span>
          ) : (
            <button className="btn quiet small" onClick={onAdopt} disabled={busy}>
              Add to my questions
            </button>
          ))}
        {(others.length > 0 || groups.length > 0) && (
          <PickThenAct
            label={`Say that "${question.name}" is the same question as another`}
            placeholder="Same question as…"
            action="Group"
            disabled={busy}
            onPick={onSameAs}
          >
            {groups.length > 0 && (
              <optgroup label="A group">
                {groups.map((g) => (
                  <option key={g.id} value={`g:${g.id}`}>
                    {g.name}
                  </option>
                ))}
              </optgroup>
            )}
            {others.length > 0 && (
              <optgroup label="Another question">
                {others.map((o) => (
                  <option key={o.research_question_id} value={`q:${o.research_question_id}`}>
                    {o.name} ({o.author.is_you ? "you" : o.author.name})
                  </option>
                ))}
              </optgroup>
            )}
          </PickThenAct>
        )}
      </div>
      <div className="tqwork" id={`team-q-work-${id}`} hidden={!open}>
        {open && <TeamQuestionWork rqId={id} />}
      </div>
    </li>
  );
}

export default function TeamPage() {
  const toast = useToast();
  const [data, setData] = useState<TeamOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [looking, setLooking] = useState(false);
  const [lookNote, setLookNote] = useState<string | null>(null);
  const [lookError, setLookError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .getTeam()
        .then((d) => {
          setData(d);
          setLoadError(null);
          return d;
        })
        .catch((e) => {
          setLoadError(errorText(e));
          return null;
        }),
    []
  );

  useEffect(() => {
    load();
  }, [load]);

  if (!data)
    return loadError ? (
      <div className="wrap narrow">
        <div className="err" role="alert">
          The team page couldn't be loaded ({loadError}).{" "}
          <button className="linkb" onClick={load}>
            Try again
          </button>
        </div>
      </div>
    ) : (
      <p className="loading">Loading…</p>
    );

  const byId = new Map(data.questions.map((q) => [q.research_question_id, q]));
  const members = (g: TeamQuestionGroup) => g.question_ids.flatMap((id) => byId.get(id) ?? []);
  const active = data.groups.filter((g) => g.status === "active");
  const suggested = data.groups.filter((g) => g.status === "suggested");
  const activeIds = new Set(active.map((g) => g.id));
  const ungrouped = data.questions.filter((q) => q.group_id === null || !activeIds.has(q.group_id));
  const teammates = data.people.filter((p) => !p.is_you);

  async function run(action: () => Promise<unknown>, done: string, focusId: string) {
    setBusy(true);
    setError(null);
    try {
      await action();
      // Shown as saved only on fresh data: an edit built from the old page would undo this one.
      if (!(await load())) {
        setData(null);
        return;
      }
      toast(done);
      focusLater(focusId);
    } catch (e) {
      setError(teamError(e));
      // The page was out of date (a teammate got there first), so show what is true now.
      await load();
    } finally {
      setBusy(false);
    }
  }

  function sameAs(question: TeamQuestion, target: string) {
    const targetId = Number(target.slice(2));
    const id = question.research_question_id;
    run(
      () =>
        target.startsWith("g:")
          ? api.addTeamGroupQuestion(targetId, id)
          : api.createTeamQuestionGroup({ research_question_ids: [targetId, id] }),
      "Grouped. Their insights now sit together in the team codebook.",
      QUESTIONS_HEADING
    );
  }

  async function look() {
    setLooking(true);
    setBusy(true);
    setLookError(null);
    setLookNote(null);
    try {
      const made = await api.suggestTeamQuestionGroups();
      await load();
      const fresh = made.length;
      setLookNote(
        fresh === 0
          ? "No questions looked alike enough to group."
          : `${plural(fresh, "possible match", "possible matches")} below. Nothing is grouped until you say so.`
      );
    } catch (e) {
      setLookError(teamError(e));
    } finally {
      setLooking(false);
      setBusy(false);
    }
  }

  return (
    <div className="wrap narrow team">
      <h1 className="h1">team</h1>
      <p className="lede">
        Everything your team has made on the conversations in this workspace. You can read it all; only your own work
        is yours to change.
      </p>
      {loadError && (
        <div className="err" role="alert" style={{ marginTop: 16 }}>
          The page couldn't be refreshed ({loadError}).{" "}
          <button className="linkb" onClick={load}>
            Try again
          </button>
        </div>
      )}
      {teammates.length === 0 && (
        <p className="backnote" style={{ marginTop: 24 }}>
          Nobody else has made anything on these conversations yet. This page fills in as your teammates read,
          highlight and reflect.
        </p>
      )}

      <section aria-labelledby="team-people">
        <h2 className="h2 rule" id="team-people">
          People <span className="n">across {plural(data.conversation_count, "conversation")}</span>
        </h2>
        <ul className="qlist">
          {data.people.map((p) => {
            const name = (
              <>
                <b>
                  {p.name} {p.is_you && <span className="tag yours">you</span>}
                </b>
                <span className="hint">{personCounts(p)}</span>
              </>
            );
            // The person page answers 404 for someone with nothing visible, which can be the viewer.
            const hasWork = p.highlight_count + p.insight_count + p.question_count + p.reflection_count > 0;
            return (
              <li key={p.user_id}>
                {hasWork ? (
                  <Link className="qrow" to={`/team/people/${p.user_id}`}>
                    {name}
                  </Link>
                ) : (
                  <div className="qrow">{name}</div>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <section aria-labelledby={QUESTIONS_HEADING}>
        <h2 className="h2 rule" id={QUESTIONS_HEADING} tabIndex={-1}>
          Questions <span className="n">{data.questions.length}</span>
        </h2>
        <div className="mergelook" style={{ marginBottom: 20 }}>
          <div className="acts">
            <button className="btn outline small" onClick={look} disabled={looking || ungrouped.length < 2}>
              <AiMark /> {looking ? "Looking…" : "Look for questions that match"}
            </button>
          </div>
          <p className="hint">
            Finds questions that different people worded differently. Nothing is grouped until you say so.
          </p>
          {lookNote && (
            <p className="hint" role="status">
              {lookNote}
            </p>
          )}
          {lookError && (
            <p className="err" role="alert">
              {lookError}
            </p>
          )}
        </div>
        {error && (
          <p className="err" role="alert">
            {error}
          </p>
        )}
        {data.questions.length === 0 && <p className="hint">Nobody has a question on these conversations yet.</p>}

        {suggested.map((g) => (
          <div className="tgroup ai" key={g.id}>
            <p className="eyebrow">
              <AiMark label="Suggested by AI" /> These may be the same question
            </p>
            <h3>{g.name}</h3>
            {g.rationale && <p className="why">{g.rationale}</p>}
            <ul>
              {members(g).map((q) => (
                <li className="tq" key={q.research_question_id}>
                  <p>{q.name}</p>
                  <Byline author={q.author} />
                </li>
              ))}
            </ul>
            <div className="acts">
              <button
                className="btn small"
                disabled={busy}
                onClick={() =>
                  run(
                    () => api.acceptTeamQuestionGroup(g.id),
                    "Grouped. Their insights now sit together in the team codebook.",
                    QUESTIONS_HEADING
                  )
                }
              >
                Group these
              </button>
              <button
                className="btn quiet small"
                disabled={busy}
                onClick={() =>
                  run(
                    () => api.dismissTeamQuestionGroup(g.id),
                    "Kept separate. It won't be suggested again.",
                    QUESTIONS_HEADING
                  )
                }
              >
                Keep separate
              </button>
            </div>
          </div>
        ))}

        {active.map((g) => (
          <div className="card tgroup" key={g.id}>
            <h3>{g.name}</h3>
            <p className="hint">
              {plural(members(g).length, "question")} asking the same thing · {plural(g.theme_count, "theme")}
            </p>
            <ul style={{ marginTop: 12 }}>
              {members(g).map((q) => (
                <li className="tq" key={q.research_question_id}>
                  <p>{q.name}</p>
                  <p className="byline">
                    <Byline author={q.author} />
                    <span>{questionCounts(q)}</span>
                  </p>
                </li>
              ))}
            </ul>
            <div className="acts">
              <Link className="btn outline small" to={`/team/codebook/${g.id}`}>
                Open the team codebook
              </Link>
            </div>
          </div>
        ))}

        {ungrouped.length > 0 && (
          <>
            {active.length > 0 && (
              <h3 className="h2" style={{ marginTop: 32 }}>
                Not grouped <span className="n">{ungrouped.length}</span>
              </h3>
            )}
            <ul>
              {ungrouped.map((q) => (
                <QuestionRow
                  key={q.research_question_id}
                  question={q}
                  others={ungrouped.filter((o) => o.research_question_id !== q.research_question_id)}
                  groups={active}
                  busy={busy}
                  onAdopt={() =>
                    run(
                      () => api.adoptTeamQuestion(q.research_question_id),
                      "Added to your questions. It starts gathering passages as you listen.",
                      `team-q-${q.research_question_id}`
                    )
                  }
                  onSameAs={(target) => sameAs(q, target)}
                />
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
