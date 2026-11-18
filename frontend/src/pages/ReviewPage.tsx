import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, audioUrlFor, errorText } from "../lib/api";
import type {
  ConnectionEvidence,
  ConversationDetail,
  ReflectionConnection,
  ResearchQuestion,
  ReviewResult,
  SetAsideItem,
  Snippet,
  VoiceMemo,
} from "../lib/types";
import { clock, plural, setAsideDate, useToast } from "../lib/format";
import type { Provenance } from "../lib/format";
import { AiMark, ProvTag } from "../lib/ui";
import ListenNext from "../components/ListenNext";

type Choice = "kept" | "edited" | "aside";

interface Decision {
  choice: Choice;
  text: string;
}

interface Added {
  key: number;
  rqId: number | null;
  text: string;
}

interface Draft {
  decisions: Record<number, Decision>;
  added: Added[];
  /** Starred highlight id to the question it goes under. */
  placed: Record<number, number>;
}

const EMPTY_DRAFT: Draft = { decisions: {}, added: [], placed: {} };
const SAVE_DELAY_MS = 800;

const asDraft = (payload: object): Draft => ({ ...EMPTY_DRAFT, ...payload });
const isEmptyDraft = (d: Draft) =>
  Object.keys(d.decisions).length === 0 && d.added.length === 0 && Object.keys(d.placed).length === 0;

// Decisions only reach the memo at "Add to memo". Until then they are a draft kept on the
// server (`/voice_memos/{id}/review_draft`). Earlier versions kept it in this browser under
// this key, and a draft found there is uploaded once.
const localDraftKey = (memoId: number) => `nim.review.${memoId}`;

function takeLocalDraft(memoId: number): Draft | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(localDraftKey(memoId)) ?? "null");
    return parsed && typeof parsed === "object" ? asDraft(parsed) : null;
  } catch {
    return null;
  }
}

function removeLocalDraft(memoId: number) {
  try {
    localStorage.removeItem(localDraftKey(memoId));
  } catch {
    // Storage is blocked, so there is no local copy to remove.
  }
}

/** The server's draft, or a draft left in this browser by an earlier version, moved to the server. */
async function loadDraft(memoId: number): Promise<{ draft: Draft; unsaved: boolean }> {
  const { payload } = await api.getReviewDraft(memoId);
  if (payload) {
    // A stale local copy would come back as new decisions once filing clears the server's.
    removeLocalDraft(memoId);
    return { draft: asDraft(payload), unsaved: false };
  }
  const local = takeLocalDraft(memoId);
  if (!local || isEmptyDraft(local)) return { draft: EMPTY_DRAFT, unsaved: false };
  try {
    await api.saveReviewDraft(memoId, local);
  } catch {
    return { draft: local, unsaved: true };
  }
  removeLocalDraft(memoId);
  return { draft: local, unsaved: false };
}

const FIT: Record<string, string> = { high: "Strong fit", medium: "Possible fit", low: "Weak fit" };

function Receipts({
  evidence,
  snippets,
  conversationId,
  memoId,
}: {
  evidence: ConnectionEvidence[];
  snippets: Map<number, Snippet>;
  conversationId: number;
  memoId: number;
}) {
  return (
    <ul className="receipts">
      {evidence.map((e, i) => {
        const snippet = snippets.get(e.snippet_id);
        return (
          <li key={e.id ?? i} className={e.source === "user" ? "yours" : "found"}>
            "{e.text || "This passage's text couldn't be loaded."}"
            <div className="src">
              {e.source === "user" ? (
                <span className="tag yours">Your highlight</span>
              ) : (
                <span className="tag found">Passage the AI found</span>
              )}
              {snippet && `${snippet.speaker_name ?? "Unknown speaker"}, ${clock(snippet.start_offset)}`}
              <Link className="linkb" to={`/read/${conversationId}?snippet=${e.snippet_id}&review=${memoId}`}>
                Open in transcript
              </Link>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function Review({ memoId }: { memoId: number }) {
  const navigate = useNavigate();
  const toast = useToast();
  const location = useLocation();
  const arrivedWithError = (location.state as { reviewError?: string | null } | null)?.reviewError ?? null;

  const [memo, setMemo] = useState<VoiceMemo | null>(null);
  const [review, setReview] = useState<ReviewResult | null>(null);
  const [conversation, setConversation] = useState<ConversationDetail | null>(null);
  const [questions, setQuestions] = useState<ResearchQuestion[]>([]);
  const [draft, setDraftState] = useState<Draft>(EMPTY_DRAFT);
  const [draftLoaded, setDraftLoaded] = useState(false);
  const [setAside, setSetAside] = useState<Map<number, SetAsideItem>>(new Map());
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [reviewError, setReviewError] = useState(arrivedWithError);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filed, setFiled] = useState(false);
  const [fixing, setFixing] = useState(false);
  const [transcription, setTranscription] = useState("");
  const [newRq, setNewRq] = useState("");
  const [newText, setNewText] = useState("");
  const audioRef = useRef<HTMLAudioElement>(null);
  const saidRef = useRef<HTMLDetailsElement>(null);

  const draftRef = useRef<Draft>(EMPTY_DRAFT);
  const dirtyRef = useRef(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Saves run one after another, and filing waits for the last one, so a late save can never
  // put a draft back after filing has cleared it.
  const saveChain = useRef<Promise<void>>(Promise.resolve());

  const flushDraft = useCallback(
    (keepalive = false) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = null;
      if (!dirtyRef.current) return saveChain.current;
      dirtyRef.current = false;
      const snapshot = draftRef.current;
      saveChain.current = saveChain.current
        .catch(() => {})
        .then(async () => {
          try {
            if (isEmptyDraft(snapshot)) await api.deleteReviewDraft(memoId, keepalive);
            else await api.saveReviewDraft(memoId, snapshot, keepalive);
          } catch (e) {
            dirtyRef.current = true;
            throw e;
          }
        });
      return saveChain.current;
    },
    [memoId]
  );

  const setDraft = useCallback(
    (update: (d: Draft) => Draft) => {
      const next = update(draftRef.current);
      draftRef.current = next;
      dirtyRef.current = true;
      setFiled(false);
      setDraftState(next);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        flushDraft().catch((e) => setError(`Your decisions couldn't be saved (${errorText(e)}). They are still on this page.`));
      }, SAVE_DELAY_MS);
    },
    [flushDraft]
  );

  const loadSetAside = useCallback(
    () =>
      api
        .listSetAside()
        .then(({ items }) => setSetAside(new Map(items.map((i) => [i.connection_id, i]))))
        // Only the dates come from here; the list itself is built from the review.
        .catch(() => {}),
    []
  );

  useEffect(() => {
    let stale = false;
    loadDraft(memoId)
      .then(({ draft: loaded, unsaved }) => {
        if (stale) return;
        draftRef.current = loaded;
        dirtyRef.current = unsaved;
        setDraftState(loaded);
        setDraftLoaded(true);
      })
      .catch((e) => setError(errorText(e)));
    loadSetAside();
    const onHide = () => {
      flushDraft(true).catch(() => {});
    };
    window.addEventListener("pagehide", onHide);
    return () => {
      stale = true;
      window.removeEventListener("pagehide", onHide);
      onHide();
    };
  }, [memoId, flushDraft, loadSetAside]);

  useEffect(() => {
    api
      .getVoiceMemo(memoId)
      .then((vm) => {
        setMemo(vm);
        setTranscription(vm.transcription_edited ?? vm.transcription_raw ?? "");
        return api.getConversation(vm.conversation_id).then(setConversation);
      })
      .catch((e) => setError(errorText(e)));
    // Resuming never asks Claude again: this only reads what was already suggested.
    api
      .getReview(memoId)
      .then(setReview)
      .catch((e) => setError(errorText(e)));
    api
      .listResearchQuestions()
      .then(setQuestions)
      .catch((e) => setError(errorText(e)));
  }, [memoId]);

  // The draft is part of the page: showing the review without it would let a new decision
  // overwrite the saved ones.
  if (!memo || !review || !draftLoaded) {
    return error ? <p className="err" style={{ margin: 32 }}>{error}</p> : <p className="loading">Loading…</p>;
  }

  const typed = memo.typed;
  const said = typed ? "wrote" : "said";
  // A typed reflection has no audio, so nothing on this page may offer to play it.
  const audioPath = typed ? null : memo.audio_path;

  const snippets = new Map((conversation?.snippets ?? []).map((s) => [s.snippet_id, s]));
  const rqName = (id: number | null) =>
    id === null ? "Doesn't fit a question yet" : (questions.find((q) => q.id === id)?.name ?? "A question you archived or merged");

  const open = review.connections.filter((c) => c.status === "suggested");
  const inMemo = review.connections.filter((c) => c.status === "accepted" || c.status === "edited");
  const asideEarlier = review.connections.filter((c) => c.status === "rejected" || c.status === "retracted");
  // Folded into another insight: neither waiting, in the memo, nor set aside.
  const mergedAway = review.connections.filter((c) => c.status === "merged").length;
  const choiceOf = (c: ReflectionConnection) => draft.decisions[c.id]?.choice ?? null;
  const live = open.filter((c) => choiceOf(c) !== "aside");
  const asideNow = open.filter((c) => choiceOf(c) === "aside");
  const waiting = live.filter((c) => choiceOf(c) === null).length;
  const placed = review.starred_unassigned.filter((h) => draft.placed[h.highlight_id] !== undefined);
  const keptCount = live.length - waiting + draft.added.length + placed.length;
  const neverReviewed = memo.status === "ready_for_review" && review.connections.length === 0;

  const groupIds: (number | null)[] = [];
  for (const id of [...live.map((c) => c.research_question_id), ...draft.added.map((a) => a.rqId)]) {
    if (!groupIds.includes(id)) groupIds.push(id);
  }
  groupIds.sort((a, b) => {
    if (a === null) return 1;
    if (b === null) return -1;
    return questions.findIndex((q) => q.id === a) - questions.findIndex((q) => q.id === b);
  });

  function decide(c: ReflectionConnection, choice: Choice | null, text = c.insight_text) {
    setDraft((d) => {
      const decisions = { ...d.decisions };
      if (choice === null) delete decisions[c.id];
      else decisions[c.id] = { choice, text };
      return { ...d, decisions };
    });
  }

  function saveEdit(c: ReflectionConnection) {
    if (!editing) return;
    const text = editing.text.trim();
    if (text && text !== c.insight_text) decide(c, "edited", text);
    setEditing(null);
  }

  function addInsight(e: FormEvent) {
    e.preventDefault();
    const text = newText.trim();
    if (!text) return;
    setDraft((d) => ({ ...d, added: [...d.added, { key: Date.now(), rqId: newRq === "" ? null : Number(newRq), text }] }));
    setNewText("");
    toast("Added to this review, marked as yours.");
  }

  async function askClaude() {
    setBusy(true);
    setError(null);
    setReviewError(null);
    try {
      setReview(await api.runReview(memoId));
    } catch (e) {
      setError(`Claude couldn't be reached (${errorText(e)}). You can still add insights by hand.`);
    } finally {
      setBusy(false);
    }
  }

  async function saveTranscription() {
    try {
      setMemo(await api.editVoiceMemoTranscription(memoId, transcription));
      setFixing(false);
      toast(typed ? "Saved." : "Transcription saved.");
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function finishLater() {
    try {
      await flushDraft();
    } catch (e) {
      setError(`Your decisions couldn't be saved (${errorText(e)}). Try again before leaving this page.`);
      return;
    }
    toast("Saved. This review waits in Finish on your desk.");
    navigate("/");
  }

  async function bringBack(c: ReflectionConnection) {
    setBusy(true);
    setError(null);
    try {
      const restored = await api.restoreConnection(c.id);
      setReview(await api.getReview(memoId));
      loadSetAside();
      setFiled(false);
      toast(restored.in_memo ? "Back in your memo." : "Brought back. It is undecided again.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function playFrom(seconds: number) {
    if (saidRef.current) saidRef.current.open = true;
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = seconds;
    audio.play().catch(() => {});
  }

  async function file() {
    setBusy(true);
    setError(null);
    const decisions: Parameters<typeof api.fileToMemo>[1] = [];
    for (const c of open) {
      const d = draft.decisions[c.id];
      if (!d) continue;
      if (d.choice === "aside") decisions.push({ connection_id: c.id, action: "reject" });
      if (d.choice === "kept") decisions.push({ connection_id: c.id, action: "accept" });
      if (d.choice === "edited") decisions.push({ connection_id: c.id, action: "edit", insight_text: d.text });
    }
    for (const a of draft.added) {
      decisions.push({ connection_id: null, action: "accept", research_question_id: a.rqId, insight_text: a.text, evidence: [] });
    }
    for (const h of placed) {
      decisions.push({
        connection_id: null,
        action: "accept",
        research_question_id: draft.placed[h.highlight_id],
        insight_text: h.notes?.trim() || h.snippets.map((s) => s.text).filter(Boolean).join(" … ") || "Starred highlight",
        evidence: h.snippets.map((s) => ({
          highlight_id: h.highlight_id,
          snippet_id: s.snippet_id,
          start_index: s.start_index,
          end_index: s.end_index,
          source: "user" as const,
        })),
      });
    }
    try {
      // Filing clears the server's draft, so nothing may be saved after it.
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = null;
      dirtyRef.current = false;
      await saveChain.current.catch(() => {});
      await api.fileToMemo(memoId, decisions);
      draftRef.current = EMPTY_DRAFT;
      setDraftState(EMPTY_DRAFT);
      setReview(await api.getReview(memoId));
      loadSetAside();
      setFiled(true);
    } catch (e) {
      dirtyRef.current = true;
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function card(c: ReflectionConnection) {
    const choice = choiceOf(c);
    const decision = draft.decisions[c.id];
    const text = decision?.text ?? c.insight_text;
    const prov: Provenance = choice === "edited" ? "both" : choice === "kept" ? "kept" : c.source === "ai" ? "ai" : "yours";
    const isEditing = editing?.id === c.id;
    const at = c.ai_metadata?.reflection_timestamp;
    return (
      <article className={"ins" + (choice === null ? " wait" : "")} key={c.id}>
        <div className="instop">
          <div className="grow">
            {isEditing ? (
              <>
                <textarea
                  className="inp"
                  rows={2}
                  autoFocus
                  aria-label="Insight"
                  value={editing.text}
                  onChange={(e) => setEditing({ id: c.id, text: e.target.value })}
                />
                <span className="acts" style={{ marginTop: 8 }}>
                  <button className="btn small" onClick={() => saveEdit(c)}>
                    Save
                  </button>
                  <button className="btn quiet small" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                  <span className="hint">Your edit is saved next to the original suggestion.</span>
                </span>
              </>
            ) : (
              <h3>{text}</h3>
            )}
            {c.ai_metadata && (
              <p className="fit">
                {[FIT[c.ai_metadata.confidence] ?? c.ai_metadata.confidence, c.ai_metadata.rationale].filter(Boolean).join(". ")}
              </p>
            )}
          </div>
          <ProvTag prov={prov} />
        </div>
        <details className="stack" open>
          <summary>
            <span>
              {c.evidence.length ? (
                <>
                  <b>{plural(c.evidence.length, "receipt")}</b> from the transcript
                </>
              ) : (
                <>
                  <b>No passage attached.</b> The AI drew this only from what you {said}.
                </>
              )}
            </span>
          </summary>
          <Receipts evidence={c.evidence} snippets={snippets} conversationId={memo!.conversation_id} memoId={memoId} />
          {audioPath && typeof at === "number" && (
            <p className="fromsaid">
              From what you said.{" "}
              <button className="linkb" onClick={() => playFrom(at)}>
                Your recording, {clock(at)}
              </button>
            </p>
          )}
        </details>
        <div className="insfoot">
          {choice === null ? (
            <button className="btn small" onClick={() => decide(c, "kept")}>
              Keep
            </button>
          ) : (
            <>
              <span className="hint">Goes into your memo when you add it.</span>
              <button className="btn quiet small" onClick={() => decide(c, null)}>
                Undo
              </button>
            </>
          )}
          <button className="btn outline small" onClick={() => setEditing({ id: c.id, text })}>
            Edit
          </button>
          <button
            className="btn quiet small"
            onClick={() => {
              decide(c, "aside");
              toast("Set aside. It stays on the record.");
            }}
          >
            Set aside
          </button>
        </div>
      </article>
    );
  }

  const decisionCount = keptCount + asideNow.length;

  return (
    <div className="wrap narrow">
      <p className="eyebrow">
        <Link to={`/read/${memo.conversation_id}`}>{conversation?.title ?? "Back to the transcript"}</Link>
      </p>
      <h1 className="h1">review your reflection</h1>
      <p className="lede">
        Claude sorted what you {said} into insights under your questions. Keep the ones that ring true, edit the ones
        that are close, and set the rest aside. Only what you keep goes into your memo.
      </p>
      {reviewError && (
        <p className="err" style={{ marginTop: 24 }}>
          Claude couldn't be reached ({reviewError}).{" "}
          {typed ? "What you wrote is saved" : "Your recording and transcription are saved"}, and you can still add
          insights by hand.
        </p>
      )}
      {error && <p className="err" style={{ marginTop: 24 }}>{error}</p>}

      <details className="said" ref={saidRef}>
        <summary>
          What you {said}
          {audioPath && memo.duration !== null ? `, ${clock(memo.duration)}` : ""}
        </summary>
        <div className="saidbody">
          {audioPath && <audio ref={audioRef} controls preload="none" src={audioUrlFor(audioPath)} />}
          {fixing ? (
            <>
              <textarea
                className="inp"
                rows={8}
                aria-label={typed ? "What you wrote" : "Transcription"}
                value={transcription}
                onChange={(e) => setTranscription(e.target.value)}
              />
              <div className="acts" style={{ marginTop: 12 }}>
                <button className="btn small" onClick={saveTranscription}>
                  Save
                </button>
                <button className="btn quiet small" onClick={() => setFixing(false)}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <>
              <p style={{ whiteSpace: "pre-wrap" }}>
                {memo.transcription_edited ?? memo.transcription_raw ?? "No transcription yet."}
              </p>
              <div className="acts" style={{ marginTop: 12 }}>
                <button className="btn outline small" onClick={() => setFixing(true)}>
                  {typed ? "Edit what you wrote" : "Fix transcription"}
                </button>
              </div>
            </>
          )}
        </div>
      </details>

      {neverReviewed && (
        <section className="qgroup">
          <h2>Not connected to your questions yet</h2>
          <div className="acts">
            <button className="btn" onClick={askClaude} disabled={busy}>
              <AiMark /> {busy ? "Connecting…" : "Connect this to my questions"}
            </button>
          </div>
        </section>
      )}

      {groupIds.map((id) => (
        <section className="qgroup" key={id ?? "none"}>
          <h2>{rqName(id)}</h2>
          {live.filter((c) => c.research_question_id === id).map(card)}
          {draft.added
            .filter((a) => a.rqId === id)
            .map((a) => (
              <article className="ins" key={a.key}>
                <div className="instop">
                  <h3>{a.text}</h3>
                  <ProvTag prov="yours" />
                </div>
                <div className="insfoot">
                  <span className="hint">Goes into your memo when you add it.</span>
                  <span className="sp" />
                  <button
                    className="btn quiet small"
                    onClick={() => setDraft((d) => ({ ...d, added: d.added.filter((x) => x.key !== a.key) }))}
                  >
                    Remove
                  </button>
                </div>
              </article>
            ))}
        </section>
      ))}

      {review.starred_unassigned.length > 0 && (
        <section className="qgroup">
          <h2>Starred, not in an insight yet</h2>
          {review.starred_unassigned.map((h) => (
            <div className="ins" key={h.highlight_id}>
              <ul className="receipts">
                {h.snippets.map((s, i) => {
                  const snippet = snippets.get(s.snippet_id);
                  return (
                    <li className="yours" key={i}>
                      "{s.text}"
                      <div className="src">
                        <span className="tag yours">Your highlight</span>
                        {snippet && `${snippet.speaker_name ?? "Unknown speaker"}, ${clock(snippet.start_offset)}`}
                        <Link className="linkb" to={`/read/${memo.conversation_id}?snippet=${s.snippet_id}&review=${memoId}`}>
                          Open in transcript
                        </Link>
                      </div>
                    </li>
                  );
                })}
              </ul>
              {h.notes && <p className="fromsaid">Your note: {h.notes}</p>}
              <label className="hint" style={{ display: "grid", gap: 4, marginTop: 12, maxWidth: 420 }}>
                Add under a question
                <select
                  className="inp"
                  value={draft.placed[h.highlight_id] ?? ""}
                  onChange={(e) =>
                    setDraft((d) => {
                      const next = { ...d.placed };
                      if (e.target.value === "") delete next[h.highlight_id];
                      else next[h.highlight_id] = Number(e.target.value);
                      return { ...d, placed: next };
                    })
                  }
                >
                  <option value="">Save for later. It stays in your starred passages.</option>
                  {questions.map((q) => (
                    <option key={q.id} value={q.id}>
                      {q.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          ))}
        </section>
      )}

      <section className="qgroup">
        <h2>Add an insight the AI missed</h2>
        <form className="form" onSubmit={addInsight}>
          <label>
            Question
            <select className="inp" value={newRq} onChange={(e) => setNewRq(e.target.value)}>
              <option value="">Doesn't fit a question yet</option>
              {questions.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Insight
            <textarea className="inp" rows={2} value={newText} onChange={(e) => setNewText(e.target.value)} />
          </label>
          <div>
            <button className="btn outline small">Add to this review</button>
          </div>
        </form>
      </section>

      {inMemo.length > 0 && (
        <section className="qgroup">
          <h2>
            Already in your memo <span>{plural(inMemo.length, "insight")}</span>
          </h2>
          {inMemo.map((c) => (
            <article className="ins" key={c.id}>
              <div className="instop">
                <div className="grow">
                  <h3>{c.insight_text}</h3>
                  <p className="fit">{rqName(c.research_question_id)}</p>
                </div>
                <ProvTag prov={c.edited_from_ai ? "both" : c.source === "ai" ? "kept" : "yours"} />
              </div>
            </article>
          ))}
        </section>
      )}

      {mergedAway > 0 && (
        <p className="hint" style={{ marginTop: 16 }}>
          {plural(mergedAway, "insight")} from this reflection {mergedAway === 1 ? "was" : "were"} merged into another
          insight. The merge can be undone from that insight on <Link to="/questions">Questions</Link>.
        </p>
      )}

      {(asideNow.length > 0 || asideEarlier.length > 0) && (
        <section className="asidebox">
          <h2 className="h2">Set aside</h2>
          <p className="hint" style={{ marginBottom: 8 }}>
            Kept on the record with the date, so anyone checking this work can see what you chose to leave out.
          </p>
          <ul>
            {asideNow.map((c) => (
              <li key={c.id}>
                <div>
                  {c.insight_text}
                  <small>Set aside in this review, not saved to the record yet</small>
                </div>
                <button className="linkb" onClick={() => decide(c, null)}>
                  Bring back
                </button>
              </li>
            ))}
            {asideEarlier.map((c) => (
              <li key={c.id}>
                <div>
                  {c.insight_text}
                  <small>
                    {setAsideDate(setAside.get(c.id)) || "Set aside earlier"}
                    {c.status === "retracted" ? ". It was in your memo, and goes back there." : ""}
                  </small>
                </div>
                <button className="linkb" onClick={() => bringBack(c)} disabled={busy}>
                  Bring back
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* The session is over once everything is decided and saved: suggest where to go next. */}
      {filed && waiting === 0 && <ListenNext />}

      <div className="decide">
        {filed ? (
          <>
            <b>
              {waiting > 0 ? `Saved. ${plural(waiting, "insight")} still to decide.` : "Added to your memo."}
            </b>
            <Link className="btn outline" to={`/read/${memo.conversation_id}`}>
              Back to the transcript
            </Link>
            <Link className="btn" to="/questions">
              View your questions
            </Link>
          </>
        ) : (
          <>
            <b>
              {waiting} to decide, {keptCount} kept{asideNow.length ? `, ${asideNow.length} set aside` : ""}
            </b>
            <button className="btn outline" onClick={finishLater}>
              Finish later
            </button>
            <button className="btn" onClick={file} disabled={busy || decisionCount === 0}>
              {keptCount > 0 ? `Add ${keptCount} to memo` : "Save what you set aside"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const memoId = Number(useParams().voiceMemoId);
  if (!Number.isInteger(memoId)) return <p className="err" style={{ margin: 32 }}>That isn't a review link.</p>;
  return <Review key={memoId} memoId={memoId} />;
}
