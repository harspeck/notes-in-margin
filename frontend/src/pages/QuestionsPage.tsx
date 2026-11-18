import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { buildQuestions } from "../lib/questions";
import type { QuestionView, QuestionsData } from "../lib/questions";
import type {
  Breadth,
  CodeFamily,
  ConnectionEvidence,
  InsightMergeNudge,
  MemoEntry,
  MergeNudge,
  QuestionHighlight,
  QuestionHighlights,
  QuestionMergeNudge,
  ResearchQuestion,
  SetAsideItem,
} from "../lib/types";
import { breadthSentence, breadthWord, plural, questionRowLine, setAsideDate, shortDate, useToast } from "../lib/format";
import { AiMark, Dots, ProvTag } from "../lib/ui";
import GuideIdeas from "../components/GuideIdeas";
import { InsightNudge, QuestionNudge } from "../components/MergeNudges";
import QuestionLevers from "../components/QuestionLevers";

const UNMAPPED = "none";
const UNMAPPED_NAME = "Doesn't fit a question yet";

/** Other insights an insight can be merged into, under the question that holds them. */
interface MergeTargetGroup {
  key: string;
  label: string;
  entries: MemoEntry[];
}

interface ReceiptGroup {
  conversationId: number;
  title: string;
  items: ConnectionEvidence[];
}

/** An insight's passages by the conversation each comes from. After an insight merge they
 * come from more than the reflection's own conversation. */
function receiptsByConversation(entry: MemoEntry): ReceiptGroup[] {
  const groups: ReceiptGroup[] = [];
  for (const e of entry.evidence) {
    const conversationId = e.conversation_id ?? entry.conversation_id;
    let group = groups.find((g) => g.conversationId === conversationId);
    if (!group) {
      group = {
        conversationId,
        title: e.conversation_title ?? (conversationId === entry.conversation_id ? entry.conversation_title : "Another conversation"),
        items: [],
      };
      groups.push(group);
    }
    group.items.push(e);
  }
  return groups;
}

function InsightCard({
  entry,
  codes,
  viewRqId,
  questions,
  mergeTargets,
  onChanged,
  onError,
}: {
  entry: MemoEntry;
  codes: CodeFamily[];
  /** The question this card is shown under; null in "Doesn't fit a question yet". */
  viewRqId: number | null;
  questions: ResearchQuestion[];
  mergeTargets: MergeTargetGroup[];
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<string | null>(null);
  const [mode, setMode] = useState<"aside" | "move" | "merge" | null>(null);
  const [moveTo, setMoveTo] = useState("");
  const [mergeInto, setMergeInto] = useState("");
  const [busy, setBusy] = useState(false);
  const groups = receiptsByConversation(entry);
  const found = entry.evidence.filter((e) => e.source === "ai").length;
  const targets = mergeTargets
    .map((g) => ({ ...g, entries: g.entries.filter((e) => e.connection_id !== entry.connection_id) }))
    .filter((g) => g.entries.length > 0);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    const text = (draft ?? "").trim();
    if (!text || text === entry.insight_text) {
      setDraft(null);
      return;
    }
    await run(async () => {
      await api.editMemoEntry(entry.connection_id, { insight_text: text });
      setDraft(null);
      onChanged();
      toast("Saved. The earlier wording is kept.");
    });
  }

  const setAside = () =>
    run(async () => {
      await api.retractMemoEntry(entry.connection_id);
      onChanged();
      toast("Set aside. It stays on the record, and you can bring it back.");
    });

  const undoMerge = (mergeId: number) =>
    run(async () => {
      await api.undoInsightMerge(mergeId);
      onChanged();
      toast("Merge undone. Both insights are back.");
    });

  const move = () =>
    run(async () => {
      const target = moveTo === UNMAPPED ? null : Number(moveTo);
      const res = await api.moveMemoEntry(entry.connection_id, target);
      const name = target === null ? UNMAPPED_NAME : (questions.find((q) => q.id === target)?.name ?? "that question");
      const left = res.removed_from_family_ids;
      const leftNames = codes.filter((f) => left.includes(f.id)).map((f) => `"${f.name}"`);
      setMode(null);
      onChanged();
      toast(
        `Moved to "${name}".` +
          (left.length
            ? ` It left ${plural(left.length, "code")} of the old question${leftNames.length === left.length ? ` (${leftNames.join(", ")})` : ""}.`
            : "")
      );
    });

  const merge = () =>
    run(async () => {
      const res = await api.mergeMemoEntry(entry.connection_id, Number(mergeInto));
      setMode(null);
      onChanged();
      toast(`Merged. ${plural(res.target.evidence_count, "highlight")} now point to one insight.`, {
        label: "Undo",
        run: () => undoMerge(res.merge.id),
      });
    });

  return (
    <article className="ins" data-insight={entry.connection_id}>
      <div className="instop">
        {draft === null ? (
          <h3>{entry.insight_text}</h3>
        ) : (
          <div className="grow">
            <textarea
              className="inp"
              rows={2}
              autoFocus
              aria-label="Insight"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
            <span className="acts" style={{ marginTop: 8 }}>
              <button className="btn small" onClick={save} disabled={busy}>
                Save
              </button>
              <button className="btn quiet small" onClick={() => setDraft(null)}>
                Cancel
              </button>
              <span className="hint">Your edit is saved next to the earlier version.</span>
            </span>
          </div>
        )}
        <ProvTag prov={entry.edited_from_ai ? "both" : entry.source === "ai" ? "kept" : "yours"} />
      </div>
      <details className="stack">
        <summary>
          <span>
            {entry.evidence.length ? (
              <>
                <b>{plural(entry.evidence.length, "highlight")}</b> from {plural(groups.length, "conversation")} point
                {entry.evidence.length === 1 ? "s" : ""} here
                {found ? `, ${found} of them found by the AI` : ""}
              </>
            ) : (
              <>
                <b>No passage attached.</b> This came only from what you said about {entry.conversation_title}.
              </>
            )}
          </span>
        </summary>
        {groups.map((g) => (
          <div key={g.conversationId}>
            <h4 className="rconv">
              {g.title} <span>{plural(g.items.length, "passage")}</span>
            </h4>
            <ul className="receipts">
              {g.items.map((e, i) => (
                <li key={e.id ?? i} className={e.source === "user" ? "yours" : "found"}>
                  "{e.text || "This passage's text couldn't be loaded."}"
                  <div className="src">
                    {e.source === "user" ? (
                      <span className="tag yours">Your highlight</span>
                    ) : (
                      <span className="tag found">Passage the AI found</span>
                    )}
                    <Link className="linkb" to={`/read/${g.conversationId}?snippet=${e.snippet_id}`}>
                      Open in transcript
                    </Link>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </details>
      {entry.merged_from.length > 0 && (
        <ul className="merged">
          {entry.merged_from.map((m) => (
            <li key={m.merge_id}>
              Merged in{m.merged_at ? ` ${shortDate(m.merged_at)}` : ""}: "{m.insight_text}"{" "}
              <button className="linkb" onClick={() => undoMerge(m.merge_id)} disabled={busy}>
                Undo merge
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="insfoot">
        {codes.length ? (
          codes.map((f) => (
            <span className="chip" key={f.id}>
              Code: {f.name}
            </span>
          ))
        ) : (
          <span className="hint">Not in a code yet</span>
        )}
        <span className="hint">Filed {shortDate(entry.filed_at)}</span>
        <span className="sp" />
        {mode === "aside" && (
          <>
            <span className="hint">It leaves your memo and moves to Set aside below, where you can bring it back.</span>
            <button className="btn small" onClick={setAside} disabled={busy}>
              Set aside
            </button>
          </>
        )}
        {mode === "move" && (
          <>
            <select
              className="inp pick"
              autoFocus
              aria-label="Move this insight to"
              value={moveTo}
              onChange={(e) => setMoveTo(e.target.value)}
            >
              <option value="">Move to…</option>
              {questions
                .filter((q) => q.id !== viewRqId)
                .map((q) => (
                  <option key={q.id} value={q.id}>
                    {q.name}
                  </option>
                ))}
              {viewRqId !== null && <option value={UNMAPPED}>{UNMAPPED_NAME}</option>}
            </select>
            <button className="btn small" onClick={move} disabled={busy || moveTo === ""}>
              Move
            </button>
          </>
        )}
        {mode === "merge" && (
          <>
            <select
              className="inp pick"
              autoFocus
              aria-label="Merge this insight into"
              value={mergeInto}
              onChange={(e) => setMergeInto(e.target.value)}
            >
              <option value="">Merge into…</option>
              {targets.map((g) => (
                <optgroup key={g.key} label={g.label}>
                  {g.entries.map((e) => (
                    <option key={e.connection_id} value={e.connection_id}>
                      {e.insight_text}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <button className="btn small" onClick={merge} disabled={busy || mergeInto === ""}>
              Merge
            </button>
            <span className="hint">Its highlights move to the insight you pick. This wording stays on the record.</span>
          </>
        )}
        {mode !== null ? (
          <button className="btn quiet small" onClick={() => setMode(null)}>
            Cancel
          </button>
        ) : (
          <>
            <button className="btn quiet small" onClick={() => setDraft(entry.insight_text)}>
              Edit
            </button>
            <button className="btn quiet small" onClick={() => setMode("move")}>
              Move
            </button>
            {targets.length > 0 && (
              <button className="btn quiet small" onClick={() => setMode("merge")}>
                Merge into…
              </button>
            )}
            <button className="btn quiet small" onClick={() => setMode("aside")}>
              Set aside
            </button>
          </>
        )}
      </div>
    </article>
  );
}

/** Highlights the user filed under the question that no filed insight cites yet. */
function FiledHighlights({
  rqId,
  stamp,
  activeIds,
  onChanged,
  onError,
}: {
  rqId: number;
  /** Changes whenever the page reloads, so this list reloads with it. */
  stamp: number;
  activeIds: Set<number>;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<QuestionHighlights | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .listQuestionHighlights(rqId)
      .then((r) => {
        if (!stale) setData(r);
      })
      .catch((e) => {
        if (!stale) onError(errorText(e));
      });
    return () => {
      stale = true;
    };
  }, [rqId, stamp, onError]);

  async function remove(h: QuestionHighlight) {
    if (!data) return;
    setBusyId(h.highlight_id);
    try {
      // The route replaces the whole set and refuses an archived question, so only the
      // questions still active are sent back.
      const rest = h.research_question_ids.filter((id) => id !== data.research_question_id && activeIds.has(id));
      await api.setHighlightQuestions(h.highlight_id, rest);
      setData({ ...data, highlights: data.highlights.filter((x) => x.highlight_id !== h.highlight_id) });
      onChanged();
      toast("Removed from this question. The highlight stays in its conversation.");
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusyId(null);
    }
  }

  if (!data || data.highlights.length === 0) return null;
  return (
    <section data-section="filed-highlights">
      <h2 className="h2" style={{ marginTop: 32 }}>
        Highlights filed here, no insight yet <span className="n">{data.highlights.length}</span>
      </h2>
      <div className="ins">
        <ul className="receipts">
          {data.highlights.map((h) => (
            <li className="yours" key={h.highlight_id}>
              "{h.text || "This highlight's text couldn't be loaded."}"
              {h.notes && <p className="fromsaid">Your note: {h.notes}</p>}
              <div className="src">
                <span className="tag yours">Your highlight</span>
                {[h.starred ? "Starred" : "", h.conversation_title].filter(Boolean).join(" · ")}
                <Link
                  className="linkb"
                  to={`/read/${h.conversation_id}${h.snippets[0] ? `?snippet=${h.snippets[0].snippet_id}` : ""}`}
                >
                  Open in transcript
                </Link>
                <button className="linkb" onClick={() => remove(h)} disabled={busyId !== null}>
                  Remove from this question
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** What was set aside under one question: taken out of the memo here, or left out at review. */
function SetAsideList({
  items,
  onChanged,
  onError,
}: {
  items: SetAsideItem[];
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const toast = useToast();
  const [busyId, setBusyId] = useState<number | null>(null);
  // A rejected insight goes back to its reflection's review, not into the memo, so the way
  // there is shown after the row itself has left this list.
  const [sentToReview, setSentToReview] = useState<number | null>(null);

  async function bringBack(item: SetAsideItem) {
    setBusyId(item.connection_id);
    try {
      const restored = await api.restoreConnection(item.connection_id);
      if (restored.in_memo) {
        setSentToReview(null);
        toast("Back in your memo.");
      } else {
        setSentToReview(restored.voice_memo_id);
      }
      onChanged();
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusyId(null);
    }
  }

  if (items.length === 0 && sentToReview === null) return null;
  return (
    <section className="asidebox">
      <h2 className="h2">
        Set aside <span className="n">{items.length}</span>
      </h2>
      <p className="hint" style={{ marginBottom: 8 }}>
        Kept on the record. Bring one back any time.
      </p>
      {sentToReview !== null && (
        <p className="backnote" role="status">
          Sent back to its reflection's review, where it is undecided again. It isn't in your memo until you keep it
          there. <Link to={`/review/${sentToReview}`}>Open that review</Link>
        </p>
      )}
      <ul>
        {items.map((item) => (
          <li key={item.connection_id}>
            <div>
              {item.insight_text}
              <small>
                {[
                  setAsideDate(item),
                  item.kind === "rejected" ? "left out at review, so it goes back to that review" : "taken out of your memo",
                  item.conversation_title,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </small>
            </div>
            <button className="linkb" onClick={() => bringBack(item)} disabled={busyId !== null}>
              Bring back
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function QuestionDetail({
  view,
  all,
  breadth,
  stamp,
  mergeTargets,
  nudges,
  setAside,
  onChanged,
  onError,
}: {
  view: QuestionView;
  all: QuestionView[];
  breadth: Breadth | null;
  stamp: number;
  mergeTargets: MergeTargetGroup[];
  nudges: InsightMergeNudge[];
  setAside: SetAsideItem[];
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const toast = useToast();
  const { rq, group, entries } = view;
  const [rename, setRename] = useState<string | null>(null);
  const [mergeTarget, setMergeTarget] = useState("");
  const liveFamilies = group.families.filter((f) => f.status === "suggested" || f.status === "accepted");
  const familiesByEntry = (connectionId: number) =>
    liveFamilies.filter((f) => f.members.some((m) => m.connection_id === connectionId));
  const heard = breadth?.questions.find((q) => q.research_question_id === rq.id);
  const opened = breadth?.opened_conversation_count ?? 0;
  const questions = all.map((q) => q.rq);

  async function run(action: () => Promise<unknown>, done?: string) {
    try {
      await action();
      onChanged();
      if (done) toast(done);
    } catch (e) {
      onError(errorText(e));
    }
  }

  async function saveName() {
    const name = (rename ?? "").trim();
    setRename(null);
    if (name && name !== rq.name) await run(() => api.updateResearchQuestion(rq.id, { name }), "Reworded.");
  }

  return (
    <>
      <p className="eyebrow">Question · not precious, change it any time</p>
      <div className="qhead">
        {rename === null ? (
          <>
            <h1 className="h1 md">{rq.name}</h1>
            <button className="btn outline small" onClick={() => setRename(rq.name)}>
              Reword
            </button>
          </>
        ) : (
          <form
            className="qrename"
            onSubmit={(e) => {
              e.preventDefault();
              saveName();
            }}
          >
            <input
              className="inp"
              autoFocus
              aria-label="Question"
              style={{ fontSize: 20, height: 52 }}
              value={rename}
              onChange={(e) => setRename(e.target.value)}
            />
            <button className="btn small">Save</button>
            <button type="button" className="btn quiet small" onClick={() => setRename(null)}>
              Cancel
            </button>
          </form>
        )}
      </div>
      {rq.description && <p className="hint" style={{ marginTop: 8 }}>{rq.description}</p>}
      {heard && breadth && (
        <div className="breadth">
          <Dots heard={heard.conversation_count} total={opened} />
          <span>
            <b>{breadthWord(heard.conversation_count, opened)}.</b> {breadthSentence(heard, breadth)}
          </span>
        </div>
      )}

      <QuestionLevers
        rq={rq}
        hasInsights={entries.length > 0}
        liveFamilies={liveFamilies}
        openedConversationIds={breadth?.opened_conversation_ids ?? []}
        onChanged={onChanged}
      />

      {nudges.map((n) => (
        <InsightNudge key={n.id} nudge={n} onChanged={onChanged} onError={onError} />
      ))}

      <h2 className="h2" style={{ marginTop: 8 }}>
        Insights <span className="n">{entries.length}</span>
      </h2>
      {entries.length === 0 && (
        <p className="hint" style={{ marginBottom: 16 }}>
          No insights yet. They arrive when you reflect on a conversation that touches this.
        </p>
      )}
      {entries.map((e) => (
        <InsightCard
          key={e.connection_id}
          entry={e}
          codes={familiesByEntry(e.connection_id)}
          viewRqId={rq.id}
          questions={questions}
          mergeTargets={mergeTargets}
          onChanged={onChanged}
          onError={onError}
        />
      ))}

      <FiledHighlights
        rqId={rq.id}
        stamp={stamp}
        activeIds={new Set(questions.map((q) => q.id))}
        onChanged={onChanged}
        onError={onError}
      />

      {liveFamilies.length > 0 && (
        <p className="hint" style={{ marginTop: 24 }}>
          <b style={{ color: "var(--black)" }}>Codes this question feeds.</b>{" "}
          {liveFamilies.map((f) => `${f.name} (${f.members.length})`).join(", ")}.{" "}
          <Link to={`/codebook?rq=${rq.id}`}>Open the codebook draft</Link>
        </p>
      )}

      <SetAsideList items={setAside} onChanged={onChanged} onError={onError} />

      <section className="asidebox">
        <h2 className="h2">Tidy up</h2>
        {group.merged_sources.length > 0 && (
          <ul className="merged" style={{ marginBottom: 12 }}>
            {group.merged_sources.map((m) => (
              <li key={m.merge_id}>
                "{m.name ?? "An earlier question"}" was merged into this one.{" "}
                <button className="linkb" onClick={() => run(() => api.undoResearchQuestionMerge(m.merge_id), "Merge undone.")}>
                  Undo merge
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="acts">
          <select
            className="inp pick"
            aria-label="Merge this question into another"
            value={mergeTarget}
            onChange={(e) => setMergeTarget(e.target.value)}
          >
            <option value="">Merge into…</option>
            {all
              .filter((q) => q.rq.id !== rq.id)
              .map((q) => (
                <option key={q.rq.id} value={q.rq.id}>
                  {q.rq.name}
                </option>
              ))}
          </select>
          <button
            className="btn outline small"
            disabled={mergeTarget === ""}
            onClick={() =>
              run(() => api.mergeResearchQuestion(rq.id, Number(mergeTarget)), "Merged. Its insights moved with it.")
            }
          >
            Merge
          </button>
          <button
            className="btn quiet small"
            onClick={() => run(() => api.updateResearchQuestion(rq.id, { archived: true }), "Archived. Restore it from the list.")}
          >
            Archive
          </button>
        </div>
      </section>
    </>
  );
}

export default function QuestionsPage() {
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<QuestionsData | null>(null);
  const [breadth, setBreadth] = useState<Breadth | null>(null);
  const [nudges, setNudges] = useState<MergeNudge[]>([]);
  const [archived, setArchived] = useState<ResearchQuestion[]>([]);
  const [setAside, setSetAside] = useState<SetAsideItem[]>([]);
  const [stamp, setStamp] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [jot, setJot] = useState("");
  const [looking, setLooking] = useState(false);
  const [lookNote, setLookNote] = useState<string | null>(null);
  const [lookError, setLookError] = useState<string | null>(null);

  const load = useCallback(() => {
    // The questions still load without these three; a failure is shown beside them. A load
    // that works clears the message an earlier action left.
    let failed: string | null = null;
    const optional = <T,>(request: Promise<T>, what: string) =>
      request.catch((e) => {
        failed = `${what} couldn't be loaded (${errorText(e)}).`;
        return null;
      });
    return Promise.all([
      api.getOrganize(),
      api.getMemo(),
      api.listResearchQuestions(true),
      optional(api.listSetAside(), "The set-aside list"),
      optional(api.getBreadth(), "How widely each question was heard"),
      optional(api.listMergeNudges(), "The merge suggestions"),
    ])
      .then(([organize, memo, rqs, aside, heard, merges]) => {
        setData(buildQuestions(organize, memo));
        setArchived(rqs.filter((r) => r.archived && r.merged_into === null));
        if (aside) setSetAside(aside.items);
        if (heard) setBreadth(heard);
        if (merges) setNudges(merges);
        setStamp((n) => n + 1);
        setError(failed);
      })
      .catch((e) => setError(errorText(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!data) return error ? <p className="err" style={{ margin: 32 }}>{error}</p> : <p className="loading">Loading…</p>;

  const requested = searchParams.get("q");
  const unmappedAside = setAside.filter((i) => i.research_question_id === null);
  // The holding place stays reachable while it only holds set-aside insights, so they can be brought back.
  const hasUnmapped = data.unmapped.length > 0 || unmappedAside.length > 0;
  const showUnmapped = requested === UNMAPPED && hasUnmapped;
  // A requested question that was merged or archived away falls back to the first one.
  const selected = showUnmapped ? null : (data.questions.find((q) => String(q.rq.id) === requested) ?? data.questions[0] ?? null);
  const select = (q: string) => setSearchParams({ q });

  const opened = breadth?.opened_conversation_count ?? 0;
  const breadthById = new Map((breadth?.questions ?? []).map((q) => [q.research_question_id, q]));
  const questionNudges = nudges.filter((n): n is QuestionMergeNudge => n.kind === "questions");
  const insightNudges = nudges.filter((n): n is InsightMergeNudge => n.kind === "insights");
  const mergeTargets: MergeTargetGroup[] = [
    ...data.questions.map((q) => ({ key: String(q.rq.id), label: q.rq.name, entries: q.entries })),
    { key: UNMAPPED, label: UNMAPPED_NAME, entries: data.unmapped },
  ];
  const mergedSources = data.questions.flatMap((q) => q.group.merged_sources.map((m) => ({ ...m, into: q.rq.name })));
  const insightCount = data.unmapped.length + data.questions.reduce((n, q) => n + q.entries.length, 0);

  async function addQuestion(e: FormEvent) {
    e.preventDefault();
    const name = jot.trim();
    if (!name) return;
    try {
      const rq = await api.createResearchQuestion({ name });
      setJot("");
      await load();
      select(String(rq.id));
      toast("Question added. It starts gathering passages as you listen.");
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function restore(rq: ResearchQuestion) {
    try {
      await api.updateResearchQuestion(rq.id, { archived: false });
      await load();
      toast("Restored.");
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function undoQuestionMerge(mergeId: number) {
    try {
      await api.undoResearchQuestionMerge(mergeId);
      await load();
      toast("Merge undone.");
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function lookForMerges() {
    setLooking(true);
    setLookError(null);
    setLookNote(null);
    try {
      const created = await api.generateMergeNudges();
      await load();
      const forQuestions = created.filter((n) => n.kind === "questions").length;
      const under = [...new Set(created.filter((n) => n.kind === "insights").map((n) => n.research_question_name ?? UNMAPPED_NAME))];
      setLookNote(
        created.length === 0
          ? "Claude found nothing alike enough to merge."
          : [
              forQuestions ? `${plural(forQuestions, "pair")} of questions, shown here.` : "",
              under.length
                ? `${plural(created.length - forQuestions, "pair")} of insights, shown inside ${under.map((n) => `"${n}"`).join(", ")}.`
                : "",
            ]
              .filter(Boolean)
              .join(" ")
      );
    } catch (err) {
      setLookError(errorText(err));
    } finally {
      setLooking(false);
    }
  }

  return (
    <div className="wrap">
      <div className="qlay">
        <aside className="qside" aria-label="Your questions">
          <div>
            <h2 className="h2">
              Your questions <span className="n">scratch paper</span>
            </h2>
            <ul className="qlist">
              {data.questions.map((q) => {
                const heard = breadthById.get(q.rq.id);
                const ideas = insightNudges.filter((n) => n.research_question_id === q.rq.id).length;
                return (
                  <li key={q.rq.id}>
                    <button
                      className="qrow"
                      aria-current={selected?.rq.id === q.rq.id ? "true" : undefined}
                      onClick={() => select(String(q.rq.id))}
                    >
                      <b>{q.rq.name}</b>
                      {heard && <Dots heard={heard.conversation_count} total={opened} />}
                      <span className="hint">
                        {heard ? questionRowLine(heard, opened) : plural(q.entries.length, "insight")}
                        {ideas ? ` · ${plural(ideas, "merge idea")}` : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
              {hasUnmapped && (
                <li>
                  <button className="qrow" aria-current={showUnmapped ? "true" : undefined} onClick={() => select(UNMAPPED)}>
                    <b>{UNMAPPED_NAME}</b>
                    <span className="hint">{plural(data.unmapped.length, "insight")} waiting for a question</span>
                  </button>
                </li>
              )}
            </ul>
            <form className="jot" onSubmit={addQuestion}>
              <input
                className="inp"
                placeholder="Jot a question"
                aria-label="Jot a question"
                value={jot}
                onChange={(e) => setJot(e.target.value)}
              />
              <button className="btn small">Add</button>
            </form>
          </div>

          <div className="mergelook">
            <div className="acts">
              <button
                className="btn outline small"
                onClick={lookForMerges}
                disabled={looking || (data.questions.length < 2 && insightCount < 2)}
              >
                <AiMark /> {looking ? "Looking…" : "Look for things to merge"}
              </button>
            </div>
            <p className="hint">Look-alike questions and insights. Nothing merges until you say so.</p>
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
            {questionNudges.map((n) => (
              <QuestionNudge key={n.id} nudge={n} onChanged={load} onError={setError} onMerged={(id) => select(String(id))} />
            ))}
          </div>

          <GuideIdeas
            emptyState={data.questions.length === 0}
            onAdded={(rq) => {
              load().then(() => select(String(rq.id)));
              toast("Question added. It starts gathering passages as you listen.");
            }}
          />

          {(mergedSources.length > 0 || archived.length > 0) && (
            <ul className="merged">
              {mergedSources.map((m) => (
                <li key={`m${m.merge_id}`}>
                  "{m.name ?? "An earlier question"}" Merged into {m.into}.{" "}
                  <button className="linkb" onClick={() => undoQuestionMerge(m.merge_id)}>
                    Undo merge
                  </button>
                </li>
              ))}
              {archived.map((rq) => (
                <li key={rq.id}>
                  "{rq.name}" Archived.{" "}
                  <button className="linkb" onClick={() => restore(rq)}>
                    Restore
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>
        <main>
          {error && <p className="err">{error}</p>}
          {showUnmapped && (
            <>
              <p className="eyebrow">Holding place</p>
              <h1 className="h1 md">{UNMAPPED_NAME}</h1>
              <p className="lede" style={{ marginBottom: 24 }}>
                Insights stay here until you move them. Nothing forces them into a question.
              </p>
              {insightNudges
                .filter((n) => n.research_question_id === null)
                .map((n) => (
                  <InsightNudge key={n.id} nudge={n} onChanged={load} onError={setError} />
                ))}
              {data.unmapped.map((e) => (
                <InsightCard
                  key={e.connection_id}
                  entry={e}
                  codes={[]}
                  viewRqId={null}
                  questions={data.questions.map((q) => q.rq)}
                  mergeTargets={mergeTargets}
                  onChanged={load}
                  onError={setError}
                />
              ))}
              <SetAsideList
                items={unmappedAside}
                onChanged={load}
                onError={setError}
              />
            </>
          )}
          {selected && (
            <QuestionDetail
              key={selected.rq.id}
              view={selected}
              all={data.questions}
              breadth={breadth}
              stamp={stamp}
              mergeTargets={mergeTargets}
              nudges={insightNudges.filter((n) => n.research_question_id === selected.rq.id)}
              setAside={setAside.filter((i) => i.research_question_id === selected.rq.id)}
              onChanged={load}
              onError={setError}
            />
          )}
          {!selected && !showUnmapped && (
            <p className="lede">No questions yet. Jot one on the left, or start from one in your conversation guide.</p>
          )}
        </main>
      </div>
    </div>
  );
}
