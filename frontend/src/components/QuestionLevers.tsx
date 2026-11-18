import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { plural, useToast } from "../lib/format";
import type { CodeFamily, CodeFamilyState, PassageSuggestion, QuestionGap, ResearchQuestion } from "../lib/types";
import PassageItem from "./PassageItem";
import { AiMark } from "../lib/ui";

// Mirrors `find_passages.max_conversations_per_run` in backend/config.yaml.
const MAX_FIND_CONVERSATIONS = 3;

const GAP_KIND: Record<QuestionGap["kind"], string> = {
  perspective: "A perspective you haven't heard",
  counter_view: "A counter view",
  thin_spot: "A thin spot",
};

interface Progress {
  at: number;
  total: number;
  title: string | null;
}

const byNewest = <T extends { id: number }>(items: T[]) => [...items].sort((a, b) => b.id - a.id);
const mergeById = <T extends { id: number }>(prev: T[], next: T[]) =>
  byNewest([...next, ...prev.filter((p) => !next.some((n) => n.id === p.id))]);

/** The three things a question can send the AI to do. Each runs only on its own click. */
export default function QuestionLevers({
  rq,
  hasInsights,
  liveFamilies,
  openedConversationIds,
  onChanged,
}: {
  rq: ResearchQuestion;
  hasInsights: boolean;
  liveFamilies: CodeFamily[];
  /** From `GET /research_questions/breadth`, to fill the search list when the desk has fewer than three. */
  openedConversationIds: number[];
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<"find" | "miss" | "theme" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [passages, setPassages] = useState<PassageSuggestion[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [findNote, setFindNote] = useState<string | null>(null);
  const [gaps, setGaps] = useState<QuestionGap[]>([]);
  const [missNote, setMissNote] = useState<string | null>(null);
  const [themes, setThemes] = useState<CodeFamilyState | null>(null);
  const [actingId, setActingId] = useState<number | null>(null);
  const aliveRef = useRef(true);

  // What an earlier visit left undecided. Reading it asks the AI nothing.
  useEffect(() => {
    aliveRef.current = true;
    api
      .listPassageSuggestions(rq.id)
      .then((r) => aliveRef.current && setPassages((prev) => mergeById(prev, r.suggestions)))
      .catch((e) => aliveRef.current && setError(errorText(e)));
    api
      .listQuestionGaps(rq.id)
      .then((r) => aliveRef.current && setGaps((prev) => mergeById(prev, r.gaps)))
      .catch((e) => aliveRef.current && setError(errorText(e)));
    return () => {
      aliveRef.current = false;
    };
  }, [rq.id]);

  /** The conversations opened most recently: the last one, then the desk's Keep reading
   * (newest first), then any other opened conversation. */
  async function conversationsToSearch() {
    const desk = await api.getDesk();
    const picked = new Map<number, string | null>();
    if (desk.last_conversation) picked.set(desk.last_conversation.conversation_id, desk.last_conversation.title);
    for (const item of desk.keep_reading) if (!picked.has(item.conversation_id)) picked.set(item.conversation_id, item.title);
    for (const id of [...openedConversationIds].reverse()) if (!picked.has(id)) picked.set(id, null);
    return [...picked].slice(0, MAX_FIND_CONVERSATIONS).map(([id, title]) => ({ id, title }));
  }

  async function findPassages() {
    setBusy("find");
    setError(null);
    setFindNote(null);
    try {
      const conversations = await conversationsToSearch();
      if (conversations.length === 0) {
        setFindNote("Open a conversation first. This looks in the conversations you opened most recently.");
        return;
      }
      let found = 0;
      let searched = 0;
      let stopped: string | null = null;
      for (const [i, c] of conversations.entries()) {
        // One request per conversation keeps each inside the server's time limit, and
        // leaving the question stops the run before the next paid call.
        if (!aliveRef.current) return;
        setProgress({ at: i + 1, total: conversations.length, title: c.title });
        try {
          const res = await api.findPassages(rq.id, c.id);
          if (!aliveRef.current) return;
          found += res.suggestions.length;
          searched += res.searched_conversation_ids.length;
          setPassages((prev) => mergeById(prev, res.suggestions));
          if (res.failed.length) {
            stopped = res.failed.map((f) => f.detail).join(" ");
            break;
          }
        } catch (e) {
          stopped = errorText(e);
          break;
        }
      }
      if (!aliveRef.current) return;
      if (stopped) {
        setError(`Looking for passages stopped after ${plural(searched, "conversation")}: ${stopped}`);
      }
      setFindNote(
        found
          ? `Claude found ${plural(found, "passage")} in the ${plural(searched, "conversation")} it read. Nothing is a highlight until you add it.`
          : stopped
            ? null
            : `Claude read ${plural(searched, "conversation")} you opened recently and found nothing new for this question.`
      );
      if (searched) onChanged();
    } catch (e) {
      if (aliveRef.current) setError(errorText(e));
    } finally {
      if (aliveRef.current) {
        setProgress(null);
        setBusy(null);
      }
    }
  }

  async function whatsMissing() {
    setBusy("miss");
    setError(null);
    setMissNote(null);
    try {
      const res = await api.runWhatsMissing(rq.id);
      if (!aliveRef.current) return;
      setGaps((prev) => mergeById(prev, res.gaps));
      setMissNote(res.gaps.length ? null : "Claude had nothing new to point out for this question.");
    } catch (e) {
      if (aliveRef.current) setError(errorText(e));
    } finally {
      if (aliveRef.current) setBusy(null);
    }
  }

  async function suggestThemes() {
    setBusy("theme");
    setError(null);
    try {
      const before = new Set(liveFamilies.map((f) => f.id));
      const state = await api.synthesizeCodeFamilies(rq.id);
      if (!aliveRef.current) return;
      setThemes({ ...state, families: state.families.filter((f) => f.status === "suggested" && !before.has(f.id)) });
      onChanged();
    } catch (e) {
      if (aliveRef.current) setError(errorText(e));
    } finally {
      if (aliveRef.current) setBusy(null);
    }
  }

  async function decide(id: number, action: () => Promise<void>) {
    setActingId(id);
    try {
      await action();
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setActingId(null);
    }
  }

  const acceptPassage = (s: PassageSuggestion) =>
    decide(s.id, async () => {
      await api.acceptPassageSuggestion(s.id);
      setPassages((prev) => prev.filter((p) => p.id !== s.id));
      onChanged();
      toast("Added as your highlight, filed under this question.");
    });

  const dismissPassage = (s: PassageSuggestion) =>
    decide(s.id, async () => {
      await api.dismissPassageSuggestion(s.id);
      setPassages((prev) => prev.filter((p) => p.id !== s.id));
      toast("Not added. It stays on the record.");
    });

  const listenNext = (gap: QuestionGap, conversationId: number) =>
    decide(gap.id, async () => {
      const res = await api.gapListenNext(gap.id, [conversationId]);
      setGaps((prev) => prev.map((g) => (g.id === gap.id ? res.gap : g)));
      toast("Added to Listen next on your desk.");
    });

  const dismissGap = (gap: QuestionGap) =>
    decide(gap.id, async () => {
      await api.dismissQuestionGap(gap.id);
      setGaps((prev) => prev.filter((g) => g.id !== gap.id));
      toast("Set aside. It stays on the record.");
    });

  const asking = (which: typeof busy, label: string) => (
    <>
      <AiMark /> {busy === which ? "Looking…" : label}
    </>
  );

  return (
    <>
      <div className="levers">
        <h2 className="h2">Send the AI to look</h2>
        <div className="acts">
          <button className="btn outline small" onClick={findPassages} disabled={busy !== null}>
            {asking("find", "Find more passages")}
          </button>
          <button className="btn outline small" onClick={whatsMissing} disabled={busy !== null}>
            {asking("miss", "What am I missing?")}
          </button>
          <button className="btn outline small" onClick={suggestThemes} disabled={busy !== null || !hasInsights}>
            {asking("theme", "Suggest themes")}
          </button>
          <Link className="btn quiet small" to={`/codebook?rq=${rq.id}`}>
            Open the codebook draft
          </Link>
        </div>
        <span className="aihint">
          Nothing changes until you keep something.
          {hasInsights ? "" : " Suggest themes needs at least one insight."}
        </span>
      </div>
      {error && (
        <p className="err" role="alert">
          {error}
        </p>
      )}

      {(passages.length > 0 || progress || findNote) && (
        <div className="leverout" data-lever="passages">
          <span className="tag ai">Suggested by AI</span>
          <h3>
            {passages.length
              ? `${plural(passages.length, "passage")} that might belong here`
              : progress
                ? "Looking for passages"
                : "No passages waiting"}
          </h3>
          {progress && (
            <p className="hint" role="status">
              Reading conversation {progress.at} of {progress.total}
              {progress.title ? `, ${progress.title}` : ""}. Each one is its own request to Claude.
            </p>
          )}
          {findNote && !progress && (
            <p className="hint" role="status">
              {findNote}
            </p>
          )}
          <ul className="receipts">
            {passages.map((s) => (
              <PassageItem
                key={s.id}
                suggestion={s}
                busy={actingId === s.id}
                onAccept={() => acceptPassage(s)}
                onDismiss={() => dismissPassage(s)}
              />
            ))}
          </ul>
        </div>
      )}

      {(gaps.length > 0 || missNote) && (
        <div className="leverout" data-lever="gaps">
          <span className="tag ai">Suggested by AI</span>
          <h3>What your insights don't cover yet</h3>
          {missNote && (
            <p className="hint" role="status">
              {missNote}
            </p>
          )}
          <ul>
            {gaps.map((gap) => (
              <li key={gap.id} className="gap">
                <span className="chip">{GAP_KIND[gap.kind] ?? "Something missing"}</span>
                <p>{gap.observation}</p>
                {gap.conversations.length > 0 && (
                  <ul className="gapconvs" aria-label="Conversations this names">
                    {gap.conversations.map((c) => (
                      <li key={c.conversation_id}>
                        <b>{c.title ?? "A conversation you haven't opened"}</b>
                        <Link className="linkb" to={`/read/${c.conversation_id}`}>
                          Open
                        </Link>
                        {c.in_listen_next ? (
                          <span className="hint">On your Listen next list</span>
                        ) : (
                          <button
                            className="btn outline small"
                            onClick={() => listenNext(gap, c.conversation_id)}
                            disabled={actingId === gap.id}
                          >
                            Add to Listen next
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <div className="acts">
                  <button className="btn quiet small" onClick={() => dismissGap(gap)} disabled={actingId === gap.id}>
                    Not useful
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {themes && (
        <div className="leverout" data-lever="themes">
          <span className="tag ai">Suggested by AI</span>
          <h3>
            {themes.families.length
              ? `${plural(themes.families.length, "code")} suggested`
              : themes.withheld_new_families
                ? "Nothing added: this question is at its cap of codes"
                : "Nothing new to suggest"}
          </h3>
          <ul>
            {themes.families.map((f) => (
              <li key={f.id}>
                <b>{f.name}.</b> {f.definition}
              </li>
            ))}
          </ul>
          <p className="hint" style={{ marginTop: 8 }}>
            Keep or set them aside in the <Link to={`/codebook?rq=${rq.id}`}>codebook draft</Link>.
          </p>
        </div>
      )}
    </>
  );
}
