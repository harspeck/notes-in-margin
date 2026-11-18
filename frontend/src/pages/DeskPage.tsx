import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { isHosted, useSession } from "../lib/session";
import type {
  Breadth,
  ConversationSummary,
  Desk,
  DeskFinishItem,
  DeskReadingItem,
  GistStatus,
  NextStepSuggestion,
} from "../lib/types";
import { clock, minutes, plural, questionRowLine, shortDate, useToast } from "../lib/format";
import { AiMark, Dots } from "../lib/ui";
import GuideIdeas from "../components/GuideIdeas";
import ListeningInterests from "../components/ListeningInterests";

const LIST_PAGE = 25;
const KEEP_READING_SHOWN = 3;

function KeepReadingCard({ item, projectName }: { item: DeskReadingItem; projectName: string | null }) {
  const { position_seconds: at, duration_seconds: duration } = item;
  const percent = at !== null && duration ? Math.min(100, Math.round((at / duration) * 100)) : null;
  const highlights = item.highlight_count
    ? `${plural(item.highlight_count, "highlight")}${item.starred_count ? `, ${item.starred_count} starred` : ""}.`
    : "No highlights yet.";
  return (
    <div className="card lcard">
      <div className="t">
        <span className="pip read" aria-hidden="true" />
        {item.title}
      </div>
      <div className="m">
        {[projectName, shortDate(item.start_time), duration ? minutes(duration) : ""].filter(Boolean).join(" · ")}
      </div>
      {percent !== null && (
        <div className="prog" role="img" aria-label={`${percent}% listened`}>
          <i style={{ width: `${percent}%` }} />
        </div>
      )}
      <div className="s">
        {at !== null ? `You stopped at ${clock(at)}. ` : ""}
        {highlights}
      </div>
      <div className="acts">
        <Link className="btn small" to={`/read/${item.conversation_id}?resume=1`}>
          {at !== null ? `Pick up at ${clock(at)}` : "Pick up"}
        </Link>
      </div>
    </div>
  );
}

function FinishCard({ item }: { item: DeskFinishItem }) {
  return (
    <div className="card lcard">
      <div className="t">
        <span className="pip wait" aria-hidden="true" />
        {item.conversation_title}
      </div>
      <div className="m">
        {item.typed ? "Written" : "Recorded"} {shortDate(item.created_at)}
      </div>
      {item.state === "transcribing" && (
        <>
          <div className="s">Still transcribing.</div>
          <div className="acts">
            <Link className="btn outline small" to={`/read/${item.conversation_id}`}>
              Open the conversation
            </Link>
          </div>
        </>
      )}
      {item.state === "not_reviewed" && (
        <>
          <div className="s">Not connected to your questions yet</div>
          <div className="m">Nothing has been suggested for it. You can ask from its review, or add insights by hand.</div>
          <div className="acts">
            <Link className="btn outline small" to={`/review/${item.voice_memo_id}`}>
              Open review
            </Link>
          </div>
        </>
      )}
      {item.state === "undecided" && (
        <>
          <div className="s">{plural(item.undecided_count, "insight")} still undecided</div>
          <div className="m">Opens your saved suggestions. The AI isn't asked again.</div>
          <div className="acts">
            <Link className="btn outline small" to={`/review/${item.voice_memo_id}`}>
              Finish review
            </Link>
          </div>
        </>
      )}
    </div>
  );
}

export default function DeskPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const { me } = useSession();
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [breadth, setBreadth] = useState<Breadth | null>(null);
  const [nextSteps, setNextSteps] = useState<NextStepSuggestion[]>([]);
  const [desk, setDesk] = useState<Desk | null>(null);
  const [projectNames, setProjectNames] = useState<Map<number, string>>(new Map());
  const [allReading, setAllReading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [gists, setGists] = useState<GistStatus | null>(null);
  const [summarising, setSummarising] = useState(false);
  const stopSummarising = useRef(false);
  const [search, setSearch] = useState("");
  const [shown, setShown] = useState(LIST_PAGE);
  const [jot, setJot] = useState("");
  // Hosted, `GET /desk` has no project names, so they come from the organization's project
  // listing: one request for the whole desk.
  const organizationId = isHosted(me) ? (me.active_workspace?.organization_id ?? null) : null;

  // The questions and how widely each was heard, counted by the server.
  const loadQuestions = useCallback(
    () =>
      api
        .getBreadth()
        .then(setBreadth)
        .catch((e) => setError(errorText(e))),
    []
  );

  useEffect(() => {
    api
      .listConversations()
      .then(setConversations)
      .catch((e) => setError(errorText(e)));
    // Suggestions saved earlier show at once. With none waiting, the server is asked for one:
    // it only goes to the AI when there are summaries to choose from and it has not just asked.
    api
      .listNextSteps()
      .then((saved) => {
        setNextSteps(saved);
        if (saved.some((s) => s.status === "suggested")) return;
        setAsking(true);
        return api
          .recommendNext()
          .then((r) => (r.recommendation ? api.listNextSteps().then(setNextSteps) : undefined))
          .finally(() => setAsking(false));
      })
      .catch(() => {});
    api
      .getGistStatus()
      .then(setGists)
      .catch(() => {});
    loadQuestions();
    api
      .getDesk()
      .then(setDesk)
      .catch((e) => setError(errorText(e)));
  }, [loadQuestions]);

  useEffect(() => {
    if (organizationId === null) return;
    api
      .listProjects(organizationId)
      .then((projects) => setProjectNames(new Map(projects.flatMap((p) => (p.name ? [[p.id, p.name]] : [])))))
      // The cards still read without a project name.
      .catch(() => {});
  }, [organizationId]);

  const byId = useMemo(() => new Map((conversations ?? []).map((c) => [c.id, c])), [conversations]);
  // Conversations with a reflection the desk knows about: one a filed insight comes from, or one
  // still waiting in Finish. A reflection whose insights were all set aside is not counted.
  const reflected = useMemo(() => {
    const ids = new Set((desk?.finish ?? []).map((f) => f.conversation_id));
    for (const q of breadth?.questions ?? []) for (const id of q.insight_conversation_ids) ids.add(id);
    for (const id of breadth?.unfiled.conversation_ids ?? []) ids.add(id);
    return ids;
  }, [desk, breadth]);
  const opened = breadth?.opened_conversation_count ?? 0;
  const keepReading = desk?.keep_reading ?? [];
  const suggestions = nextSteps.filter((s) => s.status === "suggested");
  const projectCount = new Set((conversations ?? []).map((c) => c.project_id)).size;

  const needle = search.trim().toLowerCase();
  const matches = (conversations ?? []).filter((c) => !needle || c.title.toLowerCase().includes(needle));

  function meta(id: number) {
    const c = byId.get(id);
    if (!c) return "";
    return [shortDate(c.start_time), c.duration_seconds ? minutes(c.duration_seconds) : ""].filter(Boolean).join(" · ");
  }

  async function suggestNextSteps() {
    setAsking(true);
    setError(null);
    try {
      const created = await api.generateNextSteps();
      setNextSteps(await api.listNextSteps());
      toast(
        created.length
          ? `Claude suggested ${plural(created.length, "next step")}. Nothing is opened for you.`
          : "Claude had nothing new to suggest."
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setAsking(false);
    }
  }

  /** Paid: a few conversations per request, until none are left or the user stops it. */
  async function summarise() {
    stopSummarising.current = false;
    setSummarising(true);
    setError(null);
    try {
      for (;;) {
        const run = await api.generateGists();
        setGists(run);
        if (run.failed.length) {
          setError(run.failed[0].detail);
          break;
        }
        const done = run.generated.length + run.without_transcript.length;
        if (run.remaining === 0 || done === 0 || stopSummarising.current) break;
      }
      const r = await api.recommendNext();
      if (r.recommendation) setNextSteps(await api.listNextSteps());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSummarising(false);
    }
  }

  async function openSuggestion(s: NextStepSuggestion) {
    await api.acceptNextStep(s.id).catch(() => {});
    if (s.recommended_conversation_id !== null) navigate(`/read/${s.recommended_conversation_id}`);
    else setNextSteps((prev) => prev.filter((x) => x.id !== s.id));
  }

  async function dismissSuggestion(s: NextStepSuggestion) {
    try {
      await api.dismissNextStep(s.id);
      setNextSteps((prev) => prev.filter((x) => x.id !== s.id));
      toast("Set aside. It stays on the record.");
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function addQuestion(e: FormEvent) {
    e.preventDefault();
    const name = jot.trim();
    if (!name) return;
    try {
      await api.createResearchQuestion({ name });
      setJot("");
      await loadQuestions();
      toast("Question added. It starts gathering passages as you listen.");
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="wrap">
      <div className="deskhead">
        <h1 className="h1">your desk</h1>
        {conversations && (
          <p className="bundle">
            <b>{plural(conversations.length, "conversation")}</b> across {plural(projectCount, "project")}
          </p>
        )}
      </div>
      {error && <p className="err">{error}</p>}

      <div className="lanes">
        <section className="rule lane">
          <h2 className="h2">Keep reading</h2>
          {!desk && !error && <p className="hint">Loading…</p>}
          {desk && keepReading.length === 0 && (
            <p className="hint">Nothing open. Pick a conversation from the list below.</p>
          )}
          {keepReading.slice(0, allReading ? keepReading.length : KEEP_READING_SHOWN).map((item) => (
            <KeepReadingCard
              key={item.conversation_id}
              item={item}
              projectName={item.project_name ?? (item.project_id !== null ? (projectNames.get(item.project_id) ?? null) : null)}
            />
          ))}
          {!allReading && keepReading.length > KEEP_READING_SHOWN && (
            <div className="acts">
              <button className="btn quiet small" onClick={() => setAllReading(true)}>
                Show {keepReading.length - KEEP_READING_SHOWN} more
              </button>
            </div>
          )}
        </section>

        <section className="rule lane">
          <h2 className="h2">Finish</h2>
          {!desk && !error && <p className="hint">Loading…</p>}
          {desk && desk.finish.length === 0 && <p className="hint">No reviews waiting.</p>}
          {desk?.finish.map((item) => <FinishCard key={item.voice_memo_id} item={item} />)}
        </section>

        <section className="rule lane">
          <h2 className="h2">
            Listen next <AiMark label="Suggested by AI" />
          </h2>
          {suggestions.length === 0 && asking && (
            <p className="hint" role="status">
              Finding a conversation for you…
            </p>
          )}
          {suggestions.length === 0 && !asking && (
            <p className="hint">
              {gists && gists.with_gist === 0
                ? "Nothing to suggest yet. The AI recommends from short summaries of your conversations, and none are summarised."
                : "Nothing waiting. A suggestion appears here after you finish a review."}
            </p>
          )}
          {gists && gists.remaining > 0 && (
            <div className="acts" style={{ marginTop: 8 }}>
              <button className="btn outline small" onClick={summarise} disabled={summarising}>
                <AiMark /> {summarising ? `Summarising… ${gists.remaining} left` : `Summarise ${plural(gists.remaining, "conversation")}`}
              </button>
              {summarising && (
                <button className="btn quiet small" onClick={() => (stopSummarising.current = true)}>
                  Stop
                </button>
              )}
              {!summarising && gists.with_gist > 0 && (
                <span className="hint">
                  {gists.with_gist} of {gists.total} summarised
                </span>
              )}
            </div>
          )}
          {suggestions.slice(0, 3).map((s) => (
            <div className="card ai lcard" key={s.id}>
              <div className="t">
                <span className="pip next" aria-hidden="true" />
                {s.recommended_conversation_title ?? s.suggested_direction ?? "A direction to listen for"}
              </div>
              {s.recommended_conversation_id !== null && <div className="m">{meta(s.recommended_conversation_id)}</div>}
              <p className="why">{s.rationale}</p>
              <div className="m">Suggested {shortDate(s.created_at)}.</div>
              <div className="acts">
                {s.recommended_conversation_id !== null && (
                  <button className="btn small" onClick={() => openSuggestion(s)}>
                    Open
                  </button>
                )}
                <button className="btn quiet small" onClick={() => dismissSuggestion(s)}>
                  Not useful
                </button>
              </div>
            </div>
          ))}
        </section>
      </div>

      <div className="desk2">
        <section className="rule">
          <h2 className="h2">
            All conversations {conversations && <span className="n">{matches.length}</span>}
          </h2>
          <input
            className="inp"
            type="search"
            placeholder="Search by title"
            aria-label="Search conversations by title"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setShown(LIST_PAGE);
            }}
          />
          {!conversations && !error && <p className="hint" style={{ marginTop: 12 }}>Loading…</p>}
          <ul className="convlist" style={{ marginTop: 8 }}>
            {matches.slice(0, shown).map((c) => (
              <li key={c.id}>
                <Link className="convrow" to={`/read/${c.id}`}>
                  <b>{c.title}</b>
                  {reflected.has(c.id) && <span className="tag kept">Reflected</span>}
                  <span className="hint">
                    {[
                      shortDate(c.start_time),
                      c.duration_seconds ? minutes(c.duration_seconds) : "",
                      c.num_participants ? plural(c.num_participants, "participant") : "",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
          {conversations && matches.length === 0 && <p className="hint">No conversation title matches "{search}".</p>}
          {matches.length > shown && (
            <button className="btn outline small" style={{ marginTop: 12 }} onClick={() => setShown(shown + LIST_PAGE)}>
              Show {Math.min(LIST_PAGE, matches.length - shown)} more
            </button>
          )}
        </section>

        <div className="deskcol">
        <section className="rule">
          <h2 className="h2">What you want to hear next</h2>
          <ListeningInterests>
            <button className="btn outline small" onClick={suggestNextSteps} disabled={asking}>
              <AiMark /> {asking ? "Looking…" : "Suggest what to listen to"}
            </button>
          </ListeningInterests>
        </section>

        <section className="rule">
          <h2 className="h2">
            Your questions <span className="n">scratch paper, not a plan</span>
          </h2>
          {breadth?.questions.length === 0 && (
            <GuideIdeas
              emptyState
              onAdded={() => {
                loadQuestions();
                toast("Question added. It starts gathering passages as you listen.");
              }}
            />
          )}
          <ul className="qlist">
            {breadth?.questions.map((q) => (
              <li key={q.research_question_id}>
                <Link className="qrow" to={`/questions?q=${q.research_question_id}`}>
                  <b>{q.name}</b>
                  <Dots heard={q.conversation_count} total={opened} />
                  <span className="hint">{questionRowLine(q, opened)}</span>
                </Link>
              </li>
            ))}
          </ul>
          <form className="jot" onSubmit={addQuestion}>
            <input
              className="inp"
              placeholder="Jot a question. It doesn't have to be good."
              aria-label="Jot a question"
              value={jot}
              onChange={(e) => setJot(e.target.value)}
            />
            <button className="btn small">Add</button>
          </form>
          {breadth && breadth.questions.length > 0 && opened > 0 && (
            <p className="hint" style={{ marginTop: 12 }}>
              Counted over the {plural(opened, "conversation")} you've opened
              {breadth.projects_in_scope_count > 1 ? `, across ${plural(breadth.projects_in_scope_count, "project")}` : ""}.
              {breadth.unfiled.insight_count > 0 && (
                <>
                  {" "}
                  <Link to="/questions?q=none">{plural(breadth.unfiled.insight_count, "insight")}</Link>{" "}
                  {breadth.unfiled.insight_count === 1 ? "sits" : "sit"} under no question yet.
                </>
              )}
            </p>
          )}
        </section>
        </div>
      </div>
    </div>
  );
}
