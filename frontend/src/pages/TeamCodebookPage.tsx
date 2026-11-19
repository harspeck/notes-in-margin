import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { plural, shortDate, useToast } from "../lib/format";
import { focusLater, questionCounts, teamError } from "../lib/team";
import type { TeamGroupDetail, TeamInsight, TeamTheme, TeamThemeRevision } from "../lib/types";
import { AiMark } from "../lib/ui";
import { Byline, PickThenAct, TeamInsightCard } from "../components/TeamParts";

const THEMES_HEADING = "team-themes";

type Run = (action: () => Promise<unknown>, done: string, focusId: string) => Promise<boolean>;

/** Earlier versions of a theme. Mounted when its disclosure opens, so it loads then. */
function ThemeHistory({ themeId }: { themeId: number }) {
  const [rows, setRows] = useState<TeamThemeRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .getTeamThemeHistory(themeId)
        .then((r) => {
          setRows(r);
          setError(null);
        })
        .catch((e) => setError(errorText(e))),
    [themeId]
  );

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <div className="err" role="alert">
        The history couldn't be loaded ({error}).{" "}
        <button className="linkb" onClick={load}>
          Try again
        </button>
      </div>
    );
  if (!rows) return <p className="hint">Loading…</p>;
  if (rows.length === 0) return <p className="hint">Nobody has edited this theme yet.</p>;
  return (
    <ul className="merged">
      {rows.map((r) => (
        <li key={r.id}>
          Before {r.edited_by ? `${r.edited_by.is_you ? "you" : r.edited_by.name} edited it` : "an edit"}
          {shortDate(r.created_at) ? ` on ${shortDate(r.created_at)}` : ""}: "{r.name}"
          {r.definition ? `, ${r.definition}` : ""}
          {r.member_connection_ids ? ` (${plural(r.member_connection_ids.length, "insight")})` : ""}
        </li>
      ))}
    </ul>
  );
}

function ThemeMembers({
  theme,
  insights,
  busy,
  onRemove,
}: {
  theme: TeamTheme;
  insights: Map<number, TeamInsight>;
  busy: boolean;
  onRemove?: (insight: TeamInsight) => void;
}) {
  const members = theme.member_connection_ids.flatMap((id) => insights.get(id) ?? []);
  if (members.length === 0) return <p className="hint" style={{ marginTop: 12 }}>No insights in this theme yet.</p>;
  return (
    <ul className="receipts">
      {members.map((i) => (
        <li key={i.connection_id}>
          {i.insight_text}
          <div className="src">
            <Byline author={i.author} />
            <Link className="linkb" to={`/read/${i.conversation_id}`}>
              {i.conversation_title ?? `Conversation ${i.conversation_id}`}
            </Link>
            {onRemove && (
              <button className="linkb" onClick={() => onRemove(i)} disabled={busy}>
                Take out of this theme
              </button>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function ThemeCard({
  theme,
  insights,
  busy,
  run,
}: {
  theme: TeamTheme;
  insights: Map<number, TeamInsight>;
  busy: boolean;
  run: Run;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(theme.name);
  const [definition, setDefinition] = useState(theme.definition);
  const [history, setHistory] = useState(false);
  // Bumped after each edit so an open history reloads.
  const [edits, setEdits] = useState(0);
  const headingId = `team-theme-${theme.id}`;
  const lastEdit = theme.updated_by
    ? `Last edited by ${theme.updated_by.is_you ? "you" : theme.updated_by.name}${shortDate(theme.updated_at) ? `, ${shortDate(theme.updated_at)}` : ""}`
    : theme.created_by
      ? `Added by ${theme.created_by.is_you ? "you" : theme.created_by.name}`
      : null;

  async function save(e: FormEvent) {
    e.preventDefault();
    const body: { name?: string; definition?: string } = {};
    if (name.trim() !== theme.name) body.name = name.trim();
    if (definition.trim() !== theme.definition) body.definition = definition.trim();
    if (Object.keys(body).length === 0) {
      setEditing(false);
      focusLater(headingId);
      return;
    }
    if (await run(() => api.updateTeamTheme(theme.id, body), "Saved. The earlier wording is in its history.", headingId)) {
      setEditing(false);
      setEdits((n) => n + 1);
    }
  }

  return (
    <div className="ins theme">
      {editing ? (
        <form className="form" onSubmit={save}>
          <label>
            Theme name
            <input className="inp" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </label>
          <label>
            What it means
            <textarea className="inp" rows={3} value={definition} onChange={(e) => setDefinition(e.target.value)} />
          </label>
          <div className="acts">
            <button className="btn small" disabled={busy || !name.trim()}>
              Save
            </button>
            <button
              type="button"
              className="btn quiet small"
              onClick={() => {
                setName(theme.name);
                setDefinition(theme.definition);
                setEditing(false);
                focusLater(headingId);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="instop">
            <h3 id={headingId} tabIndex={-1}>
              {theme.name}
            </h3>
            <button
              className="btn quiet small"
              aria-label={`Edit the theme "${theme.name}"`}
              onClick={() => {
                setName(theme.name);
                setDefinition(theme.definition);
                setEditing(true);
              }}
            >
              Edit
            </button>
          </div>
          {theme.definition && <p className="def">{theme.definition}</p>}
          {lastEdit && <p className="fit">{lastEdit}</p>}
        </>
      )}
      <ThemeMembers
        theme={theme}
        insights={insights}
        busy={busy}
        onRemove={async (i) => {
          const done = await run(
            () =>
              api.removeTeamThemeMember(theme.id, i.connection_id),
            "Taken out of the theme. The insight itself is unchanged.",
            headingId
          );
          if (done) setEdits((n) => n + 1);
        }}
      />
      <div className="insfoot">
        <button
          className="btn quiet small"
          aria-expanded={history}
          aria-controls={`team-theme-history-${theme.id}`}
          onClick={() => setHistory(!history)}
        >
          History
        </button>
      </div>
      <div id={`team-theme-history-${theme.id}`} hidden={!history}>
        {history && <ThemeHistory key={`${edits}-${theme.updated_at}`} themeId={theme.id} />}
      </div>
    </div>
  );
}

function AddTheme({
  insights,
  busy,
  onAdd,
  onCancel,
}: {
  insights: TeamInsight[];
  busy: boolean;
  onAdd: (body: { name: string; definition: string; connection_ids: number[] }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [definition, setDefinition] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  return (
    <form
      className="form card"
      style={{ marginBottom: 16 }}
      onSubmit={(e) => {
        e.preventDefault();
        onAdd({ name: name.trim(), definition: definition.trim(), connection_ids: picked });
      }}
    >
      <label>
        Theme name
        <input className="inp" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </label>
      <label>
        What it means
        <textarea className="inp" rows={3} value={definition} onChange={(e) => setDefinition(e.target.value)} />
      </label>
      {insights.length > 0 && (
        <fieldset className="tpick">
          <legend>Insights that belong in it</legend>
          {insights.map((i) => (
            <label key={i.connection_id}>
              <input
                type="checkbox"
                checked={picked.includes(i.connection_id)}
                onChange={(e) =>
                  setPicked(e.target.checked ? [...picked, i.connection_id] : picked.filter((id) => id !== i.connection_id))
                }
              />
              <span>
                {i.insight_text} <span className="hint">{i.author.is_you ? "you" : i.author.name}</span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      <div className="acts">
        <button className="btn small" disabled={busy || !name.trim() || !definition.trim()}>
          Add theme
        </button>
        <button type="button" className="btn quiet small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function Codebook({ groupId }: { groupId: number }) {
  const toast = useToast();
  const navigate = useNavigate();
  const [data, setData] = useState<TeamGroupDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestNote, setSuggestNote] = useState<string | null>(null);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [groupName, setGroupName] = useState("");

  const load = useCallback(
    () =>
      api
        .getTeamQuestionGroup(groupId)
        .then((d) => {
          setData(d);
          setLoadError(null);
          return d;
        })
        .catch((e) => {
          setLoadError(errorText(e));
          return null;
        }),
    [groupId]
  );

  useEffect(() => {
    load();
  }, [load]);

  if (!data)
    return loadError ? (
      <div className="wrap narrow">
        <div className="err" role="alert">
          This team codebook couldn't be loaded ({loadError}).{" "}
          <button className="linkb" onClick={load}>
            Try again
          </button>
        </div>
        <Link to="/team">Back to the team page</Link>
      </div>
    ) : (
      <p className="loading">Loading…</p>
    );

  const { group } = data;
  const insightById = new Map(data.insights.map((i) => [i.connection_id, i]));
  const accepted = data.themes.filter((t) => t.status === "accepted");
  const suggested = data.themes.filter((t) => t.status === "suggested");
  const themed = new Set(accepted.flatMap((t) => t.member_connection_ids));
  const loose = data.insights.filter((i) => !themed.has(i.connection_id));

  const run: Run = async (action, done, focusId) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      // Shown as saved only on fresh data: an edit built from the old page would undo this one.
      if (!(await load())) {
        setData(null);
        return true;
      }
      toast(done);
      focusLater(focusId);
      return true;
    } catch (e) {
      setError(teamError(e));
      // The page was out of date (a teammate got there first), so show what is true now.
      await load();
      return false;
    } finally {
      setBusy(false);
    }
  };

  async function suggest() {
    setSuggesting(true);
    setBusy(true);
    setSuggestError(null);
    setSuggestNote(null);
    try {
      const made = await api.synthesizeTeamThemes(groupId);
      await load();
      const fresh = made.length;
      setSuggestNote(
        fresh === 0
          ? "No new themes came out of these insights."
          : `${plural(fresh, "suggested theme")} below. Keep the ones that fit.`
      );
    } catch (e) {
      setSuggestError(teamError(e));
    } finally {
      setSuggesting(false);
      setBusy(false);
    }
  }

  async function rename(e: FormEvent) {
    e.preventDefault();
    const name = groupName.trim();
    if (!name || name === group.name) {
      setRenaming(false);
      focusLater("team-group-name");
      return;
    }
    if (await run(() => api.renameTeamQuestionGroup(group.id, name), "Renamed.", "team-group-name")) setRenaming(false);
  }

  async function takeOut(rqId: number) {
    setBusy(true);
    setError(null);
    try {
      await api.removeTeamGroupQuestion(group.id, rqId);
      toast("Taken out of the group. The question and its insights are unchanged.");
      // A group with no questions left has nothing to show.
      if (data && data.questions.length <= 1) {
        navigate("/team");
        return;
      }
      await load();
      focusLater("team-group-questions");
    } catch (err) {
      setError(teamError(err));
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="wrap narrow team">
      <p className="eyebrow">
        <Link to="/team">Team</Link> Team codebook
      </p>
      {renaming ? (
        <form className="qrename" onSubmit={rename}>
          <input
            className="inp"
            aria-label="Group name"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            autoFocus
          />
          <button className="btn small" disabled={busy || !groupName.trim()}>
            Save
          </button>
          <button
            type="button"
            className="btn quiet small"
            onClick={() => {
              setRenaming(false);
              focusLater("team-group-name");
            }}
          >
            Cancel
          </button>
        </form>
      ) : (
        <div className="qhead">
          <h1 className="h1 md" id="team-group-name" tabIndex={-1}>
            {group.name}
          </h1>
          <button
            className="btn outline small"
            onClick={() => {
              setGroupName(group.name);
              setRenaming(true);
            }}
          >
            Rename
          </button>
        </div>
      )}
      <p className="lede">
        One question, asked by different people, and the themes your team draws from everyone's insights under it.
        The questions and insights stay their authors'. Anyone on the team can shape the themes, and every edit is
        kept in a theme's history.
      </p>
      {group.status !== "active" && (
        <p className="backnote" style={{ marginTop: 16 }}>
          {group.status === "suggested"
            ? "This grouping is only a suggestion so far. Decide on it from the team page."
            : "This grouping was set aside."}{" "}
          <Link to="/team">Open the team page</Link>
        </p>
      )}
      {loadError && (
        <div className="err" role="alert" style={{ marginTop: 16 }}>
          The page couldn't be refreshed ({loadError}).{" "}
          <button className="linkb" onClick={load}>
            Try again
          </button>
        </div>
      )}
      {error && (
        <p className="err" role="alert" style={{ marginTop: 16 }}>
          {error}
        </p>
      )}

      <section aria-labelledby="team-group-questions">
        <h2 className="h2 rule" id="team-group-questions" tabIndex={-1}>
          Questions in this group <span className="n">{data.questions.length}</span>
        </h2>
        {data.questions.length === 0 && <p className="hint">No questions in this group.</p>}
        <ul>
          {data.questions.map((q) => (
            <li className="tq" key={q.research_question_id}>
              <h3>{q.name}</h3>
              {q.description && <p className="hint">{q.description}</p>}
              <p className="byline">
                <Byline author={q.author} />
                <span>{questionCounts(q)}</span>
                <button
                  className="linkb"
                  disabled={busy}
                  aria-label={`Take "${q.name}" out of this group`}
                  onClick={() => takeOut(q.research_question_id)}
                >
                  Take out of this group
                </button>
              </p>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby={THEMES_HEADING}>
        <h2 className="h2 rule" id={THEMES_HEADING} tabIndex={-1}>
          Themes <span className="n">{accepted.length}</span>
        </h2>
        <div className="mergelook" style={{ marginBottom: 20 }}>
          <div className="acts">
            <button className="btn outline small" onClick={suggest} disabled={suggesting || data.insights.length === 0}>
              <AiMark /> {suggesting ? "Reading the insights…" : "Suggest themes"}
            </button>
            <button className="btn outline small" aria-expanded={adding} onClick={() => setAdding(!adding)}>
              Add a theme
            </button>
          </div>
          {data.insights.length === 0 && (
            <p className="hint">Themes are drawn from filed insights, and nobody has filed one under these questions yet.</p>
          )}
          {suggestNote && (
            <p className="hint" role="status">
              {suggestNote}
            </p>
          )}
          {suggestError && (
            <p className="err" role="alert">
              {suggestError}
            </p>
          )}
        </div>
        {adding && (
          <AddTheme
            insights={data.insights}
            busy={busy}
            onCancel={() => {
              setAdding(false);
              focusLater(THEMES_HEADING);
            }}
            onAdd={async (body) => {
              if (await run(() => api.createTeamTheme(groupId, body), "Theme added.", THEMES_HEADING)) setAdding(false);
            }}
          />
        )}

        {suggested.map((t) => (
          <div className="ins wait theme" key={t.id}>
            <p className="eyebrow">
              <AiMark label="Suggested by AI" /> Suggested theme
            </p>
            <div className="instop">
              <h3>{t.name}</h3>
            </div>
            {t.definition && <p className="def">{t.definition}</p>}
            <ThemeMembers theme={t} insights={insightById} busy={busy} />
            <div className="insfoot">
              <button
                className="btn small"
                disabled={busy}
                aria-label={`Keep the theme "${t.name}"`}
                onClick={() => run(() => api.acceptTeamTheme(t.id), "Kept. It's in the team codebook now.", `team-theme-${t.id}`)}
              >
                Keep
              </button>
              <button
                className="btn quiet small"
                disabled={busy}
                aria-label={`Not this theme: "${t.name}"`}
                onClick={() => run(() => api.dismissTeamTheme(t.id), "Set aside. It won't be suggested again.", THEMES_HEADING)}
              >
                Not this
              </button>
            </div>
          </div>
        ))}

        {accepted.length === 0 && suggested.length === 0 && !adding && (
          <p className="hint">No themes yet. Add one by hand, or ask for suggestions from the insights below.</p>
        )}
        {accepted.map((t) => (
          <ThemeCard key={t.id} theme={t} insights={insightById} busy={busy} run={run} />
        ))}
      </section>

      <section aria-labelledby="team-loose">
        <h2 className="h2 rule" id="team-loose" tabIndex={-1}>
          Insights not in any theme <span className="n">{loose.length}</span>
        </h2>
        {loose.length === 0 && (
          <p className="hint">
            {data.insights.length === 0 ? "No insights are filed under these questions yet." : "Every insight is in a theme."}
          </p>
        )}
        {loose.map((i) => (
          <div key={i.connection_id} style={{ marginBottom: 16 }}>
            <TeamInsightCard insight={i} />
            {accepted.length > 0 && (
              <PickThenAct
                label={`Add the insight "${i.insight_text}" to a theme`}
                placeholder="Add to a theme…"
                action="Add"
                disabled={busy}
                onPick={(value) => {
                  const theme = accepted.find((t) => t.id === Number(value));
                  if (theme)
                    run(
                      () => api.addTeamThemeMember(theme.id, i.connection_id),
                      `Added to "${theme.name}".`,
                      "team-loose"
                    );
                }}
              >
                {accepted.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </PickThenAct>
            )}
          </div>
        ))}
      </section>
    </div>
  );
}

export default function TeamCodebookPage() {
  const groupId = Number(useParams().groupId);
  if (!Number.isInteger(groupId))
    return (
      <div className="wrap narrow">
        <p className="err">That isn't a team codebook.</p>
        <Link to="/team">Back to the team page</Link>
      </div>
    );
  return <Codebook key={groupId} groupId={groupId} />;
}
