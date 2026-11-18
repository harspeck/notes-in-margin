import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { DESK_CHANGED_EVENT, api, errorText } from "../lib/api";
import { isHosted, useSession } from "../lib/session";
import type {
  Breadth,
  ConversationDetail,
  CorticoHighlight,
  Highlight,
  HighlightSnippetSpan,
  PassageSuggestion,
  ReadingState,
  ResearchQuestion,
  VoiceMemo,
} from "../lib/types";
import { clock, minutes, plural, shortDate, useToast } from "../lib/format";
import Notebook from "../components/Notebook";
import type { FindNote } from "../components/Notebook";
import Player from "../components/Player";
import type { PlayerHandle } from "../components/Player";
import TranscriptView from "../components/TranscriptView";
import type { HighlightKind } from "../components/TranscriptView";

const MAX_VOICE_MEMO_SECONDS = 1200;
const POSITION_SAVE_MS = 10000;
// Must match `.turn { scroll-margin-top }` in styles.css: "Pick up" scrolls the saved turn to
// this line, so the turn measured here after a resume is the same one.
const READING_LINE_PX = 200;

/** Adds passages to the list, each id once. */
const mergePassages = (prev: PassageSuggestion[], next: PassageSuggestion[]) => [
  ...prev,
  ...next.filter((n, i) => !prev.some((p) => p.id === n.id) && next.findIndex((x) => x.id === n.id) === i),
];

interface Attention {
  memo: VoiceMemo;
  pending: number;
}

/** Reflections on this conversation that are still transcribing, failed, or waiting for a decision. */
async function loadAttention(id: number): Promise<Attention[]> {
  const memos = await api.listVoiceMemosForConversation(id).catch(() => []);
  const rows = await Promise.all(
    memos.map(async (memo) => {
      if (memo.status === "transcribing" || memo.status === "failed") return { memo, pending: 0 };
      if (memo.status !== "ready_for_review" && memo.status !== "filed") return null;
      const review = await api.getReview(memo.id).catch(() => null);
      if (!review) return null;
      const pending = review.connections.filter((c) => c.status === "suggested").length;
      // Ready for review with nothing suggested means the review was never run.
      const neverReviewed = memo.status === "ready_for_review" && review.connections.length === 0;
      return pending > 0 || neverReviewed ? { memo, pending } : null;
    })
  );
  return rows.filter((r): r is Attention => r !== null);
}

function Reader({ id }: { id: number }) {
  const navigate = useNavigate();
  const toast = useToast();
  const { me } = useSession();
  const [params] = useSearchParams();
  const fromReview = params.get("review");
  const linkedSnippet = Number(params.get("snippet")) || null;
  const resume = params.get("resume") === "1";

  const [conversation, setConversation] = useState<ConversationDetail | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [corticoHighlights, setCorticoHighlights] = useState<CorticoHighlight[]>([]);
  const [researchQuestions, setResearchQuestions] = useState<ResearchQuestion[] | null>(null);
  const [breadth, setBreadth] = useState<Breadth | null>(null);
  // Undecided passages the AI found in this conversation. The transcript marks them and the
  // notebook lists them, so they live here.
  const [passages, setPassages] = useState<PassageSuggestion[]>([]);
  const [findingRqId, setFindingRqId] = useState<number | null>(null);
  const [findNotes, setFindNotes] = useState<Record<number, FindNote>>({});
  const [passageBusyId, setPassageBusyId] = useState<number | null>(null);
  const [attention, setAttention] = useState<Attention[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [panel, setPanel] = useState<"closed" | "notebook" | "reflect">("closed");
  const [armedQuestionId, setArmedQuestionId] = useState<number | null>(null);
  const [editingNoteId, setEditingNoteId] = useState<number | null>(null);
  const [flashSnippetId, setFlashSnippetId] = useState<number | null>(null);

  const playerRef = useRef<PlayerHandle>(null);
  // The turn and word being played carry `data-now`. They are marked on the page itself, so
  // playing does not render the transcript again several times a second.
  const nowMark = useRef<{ turn: Element | null; word: Element | null }>({ turn: null, word: null });
  const inviteRef = useRef<HTMLElement>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [saved, setSaved] = useState<ReadingState | null>(null);
  const headRef = useRef<HTMLDivElement>(null);
  // The reading position the server holds, so only a change is sent.
  const sentRef = useRef<{ seconds: number | null; snippetId: number | null }>({ seconds: null, snippetId: null });
  // Where the reader is now: the audio time once it has been played or moved here, and the turn on the
  // reading line once the page has scrolled. Null until then, so opening a conversation
  // without touching it never moves the saved position.
  const nowRef = useRef<{ seconds: number | null; snippetId: number | null }>({ seconds: null, snippetId: null });

  useEffect(() => {
    loadAttention(id).then(setAttention);
    // Recording the open is what puts this conversation on the desk and in the header. It
    // answers with the position saved earlier, which `?resume=1` returns to.
    api
      .setReadingPosition(id)
      .then((state) => {
        sentRef.current = { seconds: state.position_seconds, snippetId: state.position_snippet_id };
        setSaved(state);
        window.dispatchEvent(new Event(DESK_CHANGED_EVENT));
      })
      .catch(() => {});
    api
      .getConversation(id)
      .then(setConversation)
      .catch((e) => setError(errorText(e)));
    api
      .listHighlights(id)
      .then(setHighlights)
      .catch((e) => setError(errorText(e)));
    // The transcript reads without them, so a failure is not shown.
    api
      .listCorticoHighlights(id)
      .then(setCorticoHighlights)
      .catch(() => {});
    api
      .listResearchQuestions()
      .then(setResearchQuestions)
      .catch((e) => setError(errorText(e)));
  }, [id]);

  // The notebook's question rows read without it, so a failure is not shown.
  const loadBreadth = useCallback(
    () =>
      api
        .getBreadth()
        .then(setBreadth)
        .catch(() => {}),
    []
  );
  useEffect(() => {
    loadBreadth();
  }, [loadBreadth]);

  // Saved passage suggestions can only be read one question at a time, so they are read the
  // first time the notebook opens and not on every visit to a transcript.
  const passagesAsked = useRef(false);
  useEffect(() => {
    if (panel === "closed" || passagesAsked.current || !researchQuestions || researchQuestions.length === 0) return;
    passagesAsked.current = true;
    Promise.all(
      researchQuestions.map((rq) =>
        api
          .listPassageSuggestions(rq.id, id)
          .then((r) => r.suggestions)
          .catch(() => [])
      )
    ).then((lists) => setPassages((prev) => mergePassages(prev, lists.flat())));
  }, [panel, researchQuestions, id]);

  const jump = useCallback((snippetId: number) => {
    document.querySelector(`[data-snippet-id="${snippetId}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlashSnippetId(snippetId);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashSnippetId(null), 1800);
  }, []);

  // A receipt link (/read/N?snippet=ID) lands on the cited passage, once per page load.
  const linkedHandled = useRef(false);
  const loaded = conversation !== null;
  useEffect(() => {
    if (!loaded || !linkedSnippet || linkedHandled.current) return;
    linkedHandled.current = true;
    const frame = requestAnimationFrame(() => jump(linkedSnippet));
    return () => cancelAnimationFrame(frame);
  }, [loaded, linkedSnippet, jump]);

  // "Pick up" (/read/N?resume=1) returns to the saved turn and audio time, once per page
  // load. A `?snippet=` link names its own passage, so it wins.
  const resumeHandled = useRef(false);
  useEffect(() => {
    if (!loaded || !saved || !resume || linkedSnippet || resumeHandled.current) return;
    resumeHandled.current = true;
    const seconds = saved.position_seconds;
    const snippetId = saved.position_snippet_id;
    const frame = requestAnimationFrame(() => {
      if (snippetId !== null) {
        document.querySelector(`[data-snippet-id="${snippetId}"]`)?.scrollIntoView({ block: "start" });
      }
      if (seconds !== null) playerRef.current?.moveTo(seconds);
    });
    return () => cancelAnimationFrame(frame);
  }, [loaded, saved, resume, linkedSnippet]);

  // The reading position is sent when it has changed, at most once per interval, and once more
  // when the page is left.
  useEffect(() => {
    if (!loaded) return;
    let measuring: ReturnType<typeof setTimeout> | null = null;
    const measure = () => {
      measuring = null;
      const top = Array.from(document.querySelectorAll<HTMLElement>("[data-snippet-id]")).find(
        (el) => el.getBoundingClientRect().bottom > READING_LINE_PX + 4
      );
      const snippetId = Number(top?.dataset.snippetId) || null;
      if (snippetId !== null) nowRef.current.snippetId = snippetId;
    };
    const onScroll = () => {
      if (measuring === null) measuring = setTimeout(measure, 300);
    };
    // Reads only refs: when this runs on unmount the transcript and the player are already gone.
    const save = (leaving: boolean) => {
      const body: { position_seconds?: number; snippet_id?: number } = {};
      const { seconds, snippetId } = nowRef.current;
      if (seconds !== null && seconds !== Math.floor(sentRef.current.seconds ?? -1)) body.position_seconds = seconds;
      if (snippetId !== null && snippetId !== sentRef.current.snippetId) body.snippet_id = snippetId;
      if (body.position_seconds === undefined && body.snippet_id === undefined) return;
      sentRef.current = {
        seconds: body.position_seconds ?? sentRef.current.seconds,
        snippetId: body.snippet_id ?? sentRef.current.snippetId,
      };
      api.setReadingPosition(id, body, leaving).catch(() => {});
    };
    const onHide = () => save(true);
    const timer = setInterval(() => save(false), POSITION_SAVE_MS);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pagehide", onHide);
    return () => {
      clearInterval(timer);
      if (measuring !== null) clearTimeout(measuring);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pagehide", onHide);
      save(true);
    };
  }, [loaded, id]);

  // The bar under the app header grows when it wraps or shows a notice, and the notebook
  // docks directly under it, so its height is measured.
  const [headHeight, setHeadHeight] = useState(64);
  useEffect(() => {
    const el = headRef.current;
    if (!loaded || !el) return;
    const observer = new ResizeObserver(() => setHeadHeight(el.offsetHeight));
    observer.observe(el);
    return () => observer.disconnect();
  }, [loaded]);

  const reachedEnd = !!conversation?.transcript_state.reached_end_at;
  useEffect(() => {
    const el = inviteRef.current;
    if (!loaded || reachedEnd || !el) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      observer.disconnect();
      api.setTranscriptState(id, { reached_end_at: true }).catch(() => {});
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [loaded, reachedEnd, id]);

  async function guard(action: () => Promise<void>) {
    try {
      setError(null);
      await action();
    } catch (e) {
      setError(errorText(e));
    }
  }

  const createHighlight = (spans: HighlightSnippetSpan[], kind: HighlightKind) =>
    guard(async () => {
      let h = await api.createHighlight(id, { notes: "", snippets: spans });
      if (kind === "star") h = await api.updateHighlight(h.id, { starred: true });
      setHighlights((prev) => [...prev, h]);
      if (kind === "note") setEditingNoteId(h.id);
      toast(kind === "star" ? "Highlighted and starred." : "Highlighted.");
    });

  const updateHighlight = (highlightId: number, body: { starred?: boolean; notes?: string }) =>
    guard(async () => {
      const updated = await api.updateHighlight(highlightId, body);
      setHighlights((prev) => prev.map((h) => (h.id === highlightId ? updated : h)));
    });

  const retryHighlightSync = (highlightId: number) =>
    guard(async () => {
      const updated = await api.retryHighlightSync(highlightId);
      setHighlights((prev) => prev.map((h) => (h.id === highlightId ? updated : h)));
      toast(updated.cortico_sync?.status === "failed" ? "Still couldn't save it to Cortico." : "Saved to Cortico.");
    });

  const deleteHighlight = (highlightId: number) =>
    guard(async () => {
      await api.deleteHighlight(highlightId);
      setHighlights((prev) => prev.filter((h) => h.id !== highlightId));
      toast("Highlight removed.");
      loadBreadth();
    });

  const setHighlightQuestions = (highlightId: number, researchQuestionIds: number[]) =>
    guard(async () => {
      const updated = await api.setHighlightQuestions(highlightId, researchQuestionIds);
      setHighlights((prev) => prev.map((h) => (h.id === highlightId ? updated : h)));
      toast(
        updated.research_question_ids.length
          ? `Filed under ${plural(updated.research_question_ids.length, "question")}.`
          : "Taken out of its questions. The highlight stays."
      );
      loadBreadth();
    });

  /** Paid, and only ever run from the notebook's button: one model call, for this conversation. */
  async function findPassages(rqId: number) {
    setFindingRqId(rqId);
    setFindNotes((prev) => ({ ...prev, [rqId]: { text: "Claude is reading this conversation.", error: false } }));
    let note: FindNote;
    try {
      const res = await api.findPassages(rqId, id);
      setPassages((prev) => mergePassages(prev, res.suggestions));
      const times = res.suggestions.flatMap((s) => (s.time_seconds !== null ? [clock(s.time_seconds)] : []));
      if (res.failed.length) note = { text: res.failed.map((f) => f.detail).join(" "), error: true };
      else if (res.searched_conversation_ids.length === 0)
        note = { text: "This conversation has no transcript Claude could read.", error: false };
      else if (res.suggestions.length === 0)
        note = { text: "Claude found nothing new for this question in this conversation.", error: false };
      else
        note = {
          text: `Claude found ${plural(res.suggestions.length, "more passage")} in this conversation${
            times.length ? `, at ${times.join(" and ")}` : ""
          }. ${res.suggestions.length === 1 ? "It is" : "They are"} marked in the transcript as passages the AI found.`,
          error: false,
        };
    } catch (e) {
      note = { text: errorText(e), error: true };
    }
    setFindNotes((prev) => ({ ...prev, [rqId]: note }));
    setFindingRqId(null);
  }

  async function decidePassage(suggestion: PassageSuggestion, action: () => Promise<void>) {
    setPassageBusyId(suggestion.id);
    await guard(action);
    setPassageBusyId(null);
  }

  const acceptPassage = (suggestion: PassageSuggestion) =>
    decidePassage(suggestion, async () => {
      const res = await api.acceptPassageSuggestion(suggestion.id);
      setHighlights((prev) => [...prev.filter((h) => h.id !== res.highlight.id), res.highlight]);
      setPassages((prev) => prev.filter((p) => p.id !== suggestion.id));
      toast("Added as your highlight, filed under its question.");
      loadBreadth();
    });

  const dismissPassage = (suggestion: PassageSuggestion) =>
    decidePassage(suggestion, async () => {
      await api.dismissPassageSuggestion(suggestion.id);
      setPassages((prev) => prev.filter((p) => p.id !== suggestion.id));
      toast("Not added. It stays on the record.");
    });

  const dropCheckpoint = (snippetId: number) =>
    guard(async () => {
      if (armedQuestionId === null) return;
      await api.setCheckpoint(id, armedQuestionId, snippetId);
      setArmedQuestionId(null);
      setConversation(await api.getConversation(id));
      toast("Section start set.");
    });

  const deleteCheckpoint = (guideQuestionId: number) =>
    guard(async () => {
      await api.deleteCheckpoint(id, guideQuestionId);
      setConversation(await api.getConversation(id));
    });

  const renameQuestion = (rqId: number, name: string) =>
    guard(async () => {
      const rq = await api.updateResearchQuestion(rqId, { name });
      setResearchQuestions((prev) => (prev ?? []).map((r) => (r.id === rqId ? rq : r)));
      toast("Saved.");
    });

  const addQuestion = (name: string) =>
    guard(async () => {
      const rq = await api.createResearchQuestion({ name });
      questionAdded(rq);
    });

  function questionAdded(rq: ResearchQuestion) {
    setResearchQuestions((prev) => [...(prev ?? []), rq]);
    toast("Question added.");
    loadBreadth();
  }

  /** Marks the turn and the word at an audio time, or clears the marks when given null. */
  function markNow(seconds: number | null) {
    const snippets = conversation?.snippets ?? [];
    let turn: Element | null = null;
    let word: Element | null = null;
    if (seconds !== null) {
      let lo = 0;
      let hi = snippets.length - 1;
      let at = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (snippets[mid].start_offset <= seconds) {
          at = mid;
          lo = mid + 1;
        } else hi = mid - 1;
      }
      const s = snippets[at];
      if (s) {
        turn = document.querySelector(`.turn[data-snippet-id="${s.snippet_id}"]`);
        let wordIndex = -1;
        if (seconds <= s.end_offset + 0.5) {
          for (let i = 0; i < s.words.length && s.words[i].start <= seconds; i++) wordIndex = i;
        }
        if (turn && wordIndex >= 0) word = turn.querySelector(`[data-widx="${wordIndex}"]`);
      }
    }
    const prev = nowMark.current;
    if (prev.turn !== turn) prev.turn?.removeAttribute("data-now");
    if (prev.word !== word) prev.word?.removeAttribute("data-now");
    turn?.setAttribute("data-now", "");
    word?.setAttribute("data-now", "");
    nowMark.current = { turn, word };
  }

  const seek = (seconds: number) => playerRef.current?.playFrom(seconds);

  if (!conversation) {
    return error ? <p className="err" style={{ margin: 32 }}>{error}</p> : <p className="loading">Loading…</p>;
  }

  const hasAudio = conversation.has_audio && !!conversation.audio_url;
  const starred = highlights.filter((h) => h.starred).length;

  return (
    <div style={{ "--head": `${headHeight}px` } as CSSProperties}>
      <div className="readhead" ref={headRef}>
      <div className="rbar">
        <div className="rtitle">
          <b>{conversation.title}</b>
          <span>
            {[
              conversation.duration_seconds ? minutes(conversation.duration_seconds) : "",
              highlights.length ? `${plural(highlights.length, "highlight")}, ${starred} starred` : "No highlights yet",
              hasAudio ? "" : "No audio for this conversation",
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </div>
        {hasAudio && (
          <Player
            ref={playerRef}
            src={conversation.audio_url ?? ""}
            durationSeconds={conversation.duration_seconds}
            onTime={(seconds) => {
              nowRef.current.seconds = Math.floor(seconds);
              markNow(seconds);
            }}
            onEnded={() => markNow(null)}
            onError={setError}
          />
        )}
        <button
          className="btn outline small"
          aria-expanded={panel === "notebook"}
          onClick={() => setPanel(panel === "notebook" ? "closed" : "notebook")}
        >
          Notebook
        </button>
        <button className="btn small" onClick={() => setPanel("reflect")}>
          <span className="dot" />
          Reflect
        </button>
      </div>

      {fromReview && (
        <div className="backbar">
          Opened from your review.
          <Link className="btn outline small" to={`/review/${fromReview}`}>
            Back to review
          </Link>
        </div>
      )}
      {attention.map(({ memo, pending }) => (
        <div className="backbar" key={memo.id}>
          {memo.status === "transcribing" && <>Your reflection from {shortDate(memo.recorded_at)} is still transcribing.</>}
          {memo.status === "failed" && (
            <>
              Transcription failed for your reflection from {shortDate(memo.recorded_at)}. The audio is saved. Record
              again to get a transcription.
            </>
          )}
          {(memo.status === "ready_for_review" || memo.status === "filed") && (
            <>
              Your reflection from {shortDate(memo.recorded_at)}{" "}
              {pending > 0 ? `has ${plural(pending, "insight")} still undecided.` : "hasn't been reviewed yet."}
              <Link className="btn outline small" to={`/review/${memo.id}`}>
                Finish review
              </Link>
            </>
          )}
        </div>
      ))}
      {error && <p className="err" style={{ margin: "12px 24px" }}>{error}</p>}
      </div>

      <div className={"reader" + (panel === "closed" ? "" : " open")}>
        <TranscriptView
          conversation={conversation}
          highlights={highlights}
          corticoHighlights={corticoHighlights}
          onCreateHighlight={createHighlight}
          onUpdateHighlight={updateHighlight}
          onDeleteHighlight={deleteHighlight}
          onRetryHighlightSync={retryHighlightSync}
          researchQuestions={researchQuestions ?? []}
          onSetHighlightQuestions={setHighlightQuestions}
          passages={passages}
          passageBusyId={passageBusyId}
          onAcceptPassage={acceptPassage}
          onDismissPassage={dismissPassage}
          editingNoteId={editingNoteId}
          onEditNote={setEditingNoteId}
          armedQuestionId={armedQuestionId}
          onArm={setArmedQuestionId}
          onDropCheckpoint={dropCheckpoint}
          onDeleteCheckpoint={deleteCheckpoint}
          flashSnippetId={flashSnippetId}
          onSeek={hasAudio ? seek : null}
        >
          <section className="invite" ref={inviteRef}>
            <div>
              <h2>reflect on what you heard</h2>
              <p>
                {isHosted(me)
                  ? "Write it however it comes out. Your notes open beside the transcript while you write, and it gets sorted into insights afterwards."
                  : "Say it however it comes out. Your notes open beside the transcript while you talk, and it gets sorted into insights afterwards."}
              </p>
            </div>
            <button className="btn" onClick={() => setPanel("reflect")}>
              <span className="dot" />
              Reflect
            </button>
          </section>
        </TranscriptView>

        {panel !== "closed" && (
          <Notebook
            conversation={conversation}
            highlights={highlights}
            researchQuestions={researchQuestions ?? []}
            questionsLoaded={researchQuestions !== null}
            breadth={breadth}
            passages={passages}
            findingRqId={findingRqId}
            findNotes={findNotes}
            passageBusyId={passageBusyId}
            onFindPassages={findPassages}
            onAcceptPassage={acceptPassage}
            onDismissPassage={dismissPassage}
            onQuestionAdded={questionAdded}
            reflect={panel === "reflect"}
            canRecord={!isHosted(me)}
            maxDurationSeconds={MAX_VOICE_MEMO_SECONDS}
            onJump={jump}
            onRenameQuestion={renameQuestion}
            onAddQuestion={addQuestion}
            onSaved={() => loadAttention(id).then(setAttention)}
            onReviewReady={(memo, reviewError) => navigate(`/review/${memo.id}`, { state: { reviewError } })}
            onClose={() => setPanel("closed")}
          />
        )}
      </div>
    </div>
  );
}

export default function ReaderPage() {
  const id = Number(useParams().conversationId);
  if (!Number.isInteger(id)) return <p className="err" style={{ margin: 32 }}>That isn't a conversation link.</p>;
  return <Reader key={id} id={id} />;
}
