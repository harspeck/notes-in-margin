import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, errorText } from "../lib/api";
import type { Breadth, ConversationDetail, Highlight, PassageSuggestion, ResearchQuestion, VoiceMemo } from "../lib/types";
import { clock, questionRowLine } from "../lib/format";
import { AiMark, Icon } from "../lib/ui";
import GuideIdeas from "./GuideIdeas";
import PassageItem from "./PassageItem";

const POLL_INTERVAL_MS = 700;
const MAX_TYPED_CHARS = 20000;

const PROMPTS = (
  <ol className="prompts">
    <li>What surprised you?</li>
    <li>What connects to your research questions?</li>
    <li>What would you want to hear more about?</li>
  </ol>
);

/** The one AI review of a new reflection, spoken or typed. It is best-effort: the review page
 * opens either way, so a failure comes back as text and never throws. */
async function connectToQuestions(memo: VoiceMemo): Promise<string | null> {
  try {
    await api.runReview(memo.id);
    return null;
  } catch (e) {
    return errorText(e);
  }
}

// Unsent typed reflections, by conversation. The notebook unmounts when it closes, so the text
// is held here and, where storage is allowed, in localStorage across reloads.
const typedDrafts = new Map<number, string>();
const typedDraftKey = (conversationId: number) => `nim.reflection.${conversationId}`;

function readTypedDraft(conversationId: number): string {
  const held = typedDrafts.get(conversationId);
  if (held !== undefined) return held;
  try {
    return localStorage.getItem(typedDraftKey(conversationId)) ?? "";
  } catch {
    return "";
  }
}

function writeTypedDraft(conversationId: number, text: string) {
  typedDrafts.set(conversationId, text);
  try {
    if (text) localStorage.setItem(typedDraftKey(conversationId), text);
    else localStorage.removeItem(typedDraftKey(conversationId));
  } catch {
    // Without storage the text lasts as long as this tab stays open.
  }
}

interface ReflectProps {
  conversationId: number;
  /** The reflection is saved and readable. `reviewError` is set when Claude could not be reached. */
  onReviewReady: (memo: VoiceMemo, reviewError: string | null) => void;
  onSaved: () => void;
}

interface WriterProps extends ReflectProps {
  /** False in the hosted app, where typing is the only way to reflect. */
  canRecord: boolean;
  onRecordInstead: () => void;
}

function Writer({ conversationId, canRecord, onReviewReady, onSaved, onRecordInstead }: WriterProps) {
  const [text, setText] = useState(() => readTypedDraft(conversationId));
  const [phase, setPhase] = useState<"idle" | "saving" | "connecting">("idle");
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  function change(value: string) {
    setText(value);
    writeTypedDraft(conversationId, value);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setPhase("saving");
    setError(null);
    let memo: VoiceMemo;
    try {
      memo = await api.createTypedReflection(conversationId, text.trim());
    } catch (err) {
      setError(`Saving your reflection failed (${errorText(err)}). What you wrote is still here.`);
      setPhase("idle");
      return;
    }
    writeTypedDraft(conversationId, "");
    onSaved();
    setPhase("connecting");
    const reviewError = await connectToQuestions(memo);
    if (aliveRef.current) onReviewReady(memo, reviewError);
  }

  if (phase !== "idle") {
    return (
      <div className="rec">
        <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>Working on your reflection</h3>
        <ol className="steps">
          <li className={phase === "saving" ? "run" : "done"}>
            <div>{phase === "saving" ? "Saving what you wrote" : "Reflection saved"}</div>
          </li>
          <li className={phase === "connecting" ? "run" : ""}>
            <div>
              Connecting what you wrote to your questions
              <span>
                <AiMark /> Your review opens when it's ready.
              </span>
            </div>
          </li>
        </ol>
      </div>
    );
  }

  return (
    <form className="rec write" onSubmit={submit}>
      {error && <p className="err" role="alert">{error}</p>}
      {PROMPTS}
      <label>
        Write your reflection
        <textarea
          className="inp"
          rows={8}
          maxLength={MAX_TYPED_CHARS}
          placeholder="However it comes out. Nothing needs to be in order."
          value={text}
          onChange={(e) => change(e.target.value)}
        />
      </label>
      <div className="acts">
        <button className="btn" disabled={!text.trim()}>
          <AiMark /> Done, connect this to my questions
        </button>
      </div>
      {canRecord ? (
        <p className="hint">
          <button type="button" className="linkb" onClick={onRecordInstead}>
            Record it instead
          </button>{" "}
          What you've written stays here.
        </p>
      ) : (
        <p className="hint">Recording isn't available in the hosted app yet, so reflections are written here.</p>
      )}
    </form>
  );
}

interface RecorderProps extends ReflectProps {
  maxDurationSeconds: number;
  onTypeInstead: () => void;
}

type Phase = "idle" | "recording" | "paused" | "saving" | "transcribing" | "connecting";

function Recorder({ conversationId, maxDurationSeconds, onReviewReady, onSaved, onTypeInstead }: RecorderProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [unsaved, setUnsaved] = useState(false);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const blobRef = useRef<Blob | null>(null);
  const elapsedRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const frameRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  function stopTimer() {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
  }

  function stopMeter(close: boolean) {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    frameRef.current = null;
    if (close) {
      audioContextRef.current?.close().catch(() => {});
      audioContextRef.current = null;
      analyserRef.current = null;
    }
    setLevel(0);
  }

  function runMeter() {
    const analyser = analyserRef.current;
    if (!analyser) return;
    const data = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sumSquares = 0;
      for (const v of data) sumSquares += ((v - 128) / 128) ** 2;
      // Speech is quiet as a root mean square, so it is scaled up to fill the meter.
      setLevel(Math.min(Math.sqrt(sumSquares / data.length) * 3.5, 1));
      frameRef.current = requestAnimationFrame(tick);
    };
    tick();
  }

  function releaseMicrophone() {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearInterval(timerRef.current);
      if (pollRef.current) clearInterval(pollRef.current);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      audioContextRef.current?.close().catch(() => {});
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  function startTimer() {
    stopTimer();
    timerRef.current = setInterval(() => {
      elapsedRef.current += 1;
      setElapsed(elapsedRef.current);
      if (elapsedRef.current >= maxDurationSeconds) finish();
    }, 1000);
  }

  async function start() {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.start();
      recorderRef.current = recorder;
      elapsedRef.current = 0;
      setElapsed(0);
      setPhase("recording");
      startTimer();
      const AudioContextClass =
        window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const audioContext = new AudioContextClass();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      // Analysis only: not connected to the speakers, so there is no echo.
      audioContext.createMediaStreamSource(stream).connect(analyser);
      audioContextRef.current = audioContext;
      analyserRef.current = analyser;
      runMeter();
    } catch {
      setError("Couldn't reach the microphone. Check this browser's permission for it.");
    }
  }

  function pause() {
    recorderRef.current?.pause();
    stopTimer();
    stopMeter(false);
    setPhase("paused");
  }

  function resume() {
    recorderRef.current?.resume();
    startTimer();
    runMeter();
    setPhase("recording");
  }

  function discard() {
    stopTimer();
    stopMeter(true);
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = null;
      recorder.stop();
    }
    releaseMicrophone();
    blobRef.current = null;
    setUnsaved(false);
    setError(null);
    setPhase("idle");
  }

  function finish() {
    stopTimer();
    stopMeter(true);
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    recorder.onstop = () => {
      blobRef.current = new Blob(chunksRef.current, { type: "audio/webm" });
      releaseMicrophone();
      save();
    };
    recorder.stop();
  }

  async function connect(memo: VoiceMemo) {
    setPhase("connecting");
    const reviewError = await connectToQuestions(memo);
    if (aliveRef.current) onReviewReady(memo, reviewError);
  }

  async function save() {
    if (!blobRef.current) return;
    setPhase("saving");
    setError(null);
    setUnsaved(false);
    let memo: VoiceMemo;
    try {
      memo = await api.uploadVoiceMemo(conversationId, blobRef.current, elapsedRef.current);
    } catch (e) {
      setError(`Saving the recording failed (${errorText(e)}). It is still held in this tab.`);
      setUnsaved(true);
      setPhase("idle");
      return;
    }
    onSaved();
    // The recording is saved as soon as the upload returns. Transcription runs in the
    // background, so this polls for progress.
    setPhase("transcribing");
    setProgress(memo.transcription_progress);
    // One request at a time, and nothing after the answer that ended the poll: two overlapping
    // answers would each start the paid review.
    let asking = false;
    let settled = false;
    pollRef.current = setInterval(async () => {
      if (asking || settled) return;
      asking = true;
      const latest = await api.getVoiceMemo(memo.id).catch(() => null);
      asking = false;
      if (!latest || !aliveRef.current || settled) return;
      setProgress(latest.transcription_progress);
      if (latest.status === "ready_for_review" || latest.status === "failed") {
        settled = true;
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
      }
      if (latest.status === "ready_for_review") connect(latest);
      if (latest.status === "failed") {
        setError(
          `Transcription failed: ${latest.transcription_error ?? "unknown error"}. The audio is saved, and you can record again to get a transcription.`
        );
        setPhase("idle");
      }
    }, POLL_INTERVAL_MS);
  }

  const step = (at: Phase) => {
    const order: Phase[] = ["saving", "transcribing", "connecting"];
    const now = order.indexOf(phase);
    const mine = order.indexOf(at);
    return mine < now ? "done" : mine === now ? "run" : "";
  };

  return (
    <div className="rec">
      {error && <p className="err" role="alert">{error}</p>}
      {phase === "idle" && unsaved && (
        <div className="acts">
          <button className="btn" onClick={save}>
            Try saving again
          </button>
          <button className="btn quiet small" onClick={discard}>
            Discard
          </button>
        </div>
      )}
      {phase === "idle" && !unsaved && (
        <>
          {PROMPTS}
          <div className="recgo">
            <button className="recbtn" onClick={start} aria-label="Start recording">
              <Icon name="mic" />
            </button>
            <div>
              <b>Record your reflection</b>
              <span className="hint">Recording stops at {clock(maxDurationSeconds)}.</span>
            </div>
          </div>
          <p className="hint" style={{ marginTop: 12 }}>
            <button className="linkb" onClick={onTypeInstead}>
              Type it instead
            </button>
          </p>
        </>
      )}
      {(phase === "recording" || phase === "paused") && (
        <>
          <div className={"timer" + (phase === "paused" ? " paused" : "")}>
            <i />
            <span>{clock(elapsed)}</span>
          </div>
          <div className="meter" aria-hidden="true">
            <i style={{ width: `${Math.round(level * 100)}%` }} />
          </div>
          <p className="hint" style={{ margin: "4px 0 14px" }}>
            {phase === "paused"
              ? "Paused."
              : level > 0.04
                ? "Picking up your voice. Stream of consciousness is fine."
                : "Quiet. Check your microphone if you are speaking."}
          </p>
          <div className="acts">
            <button className="btn" onClick={finish}>
              Finish
            </button>
            <button className="btn outline small" onClick={phase === "paused" ? resume : pause}>
              {phase === "paused" ? "Resume" : "Pause"}
            </button>
            <button className="btn quiet small" onClick={discard}>
              Discard
            </button>
          </div>
        </>
      )}
      {(phase === "saving" || phase === "transcribing" || phase === "connecting") && (
        <>
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>Working on your reflection</h3>
          <ol className="steps">
            <li className={step("saving")}>
              <div>
                {phase === "saving" ? "Saving your recording" : "Recording saved"}
                <span>{clock(elapsed)}</span>
              </div>
            </li>
            <li className={step("transcribing")}>
              <div>
                Transcribing{phase === "transcribing" ? ` ${Math.round(progress * 100)}%` : ""}
                <span>On this computer. Nothing is sent anywhere.</span>
              </div>
            </li>
            <li className={step("connecting")}>
              <div>
                Connecting what you said to your questions
                <span>
                <AiMark /> Your review opens when it's ready.
              </span>
              </div>
            </li>
          </ol>
        </>
      )}
    </div>
  );
}

export interface FindNote {
  text: string;
  error: boolean;
}

interface Props {
  conversation: ConversationDetail;
  highlights: Highlight[];
  researchQuestions: ResearchQuestion[];
  /** False until the questions have been read, so an empty list isn't taken for "no questions". */
  questionsLoaded: boolean;
  breadth: Breadth | null;
  /** Undecided passages the AI found in this conversation, for any question. */
  passages: PassageSuggestion[];
  /** The question "Find more passages" is running for. */
  findingRqId: number | null;
  /** What the last "Find more passages" said, by question. */
  findNotes: Record<number, FindNote>;
  passageBusyId: number | null;
  onFindPassages: (researchQuestionId: number) => void;
  onAcceptPassage: (suggestion: PassageSuggestion) => void;
  onDismissPassage: (suggestion: PassageSuggestion) => void;
  onQuestionAdded: (rq: ResearchQuestion) => void;
  reflect: boolean;
  /** False where the backend can't store or transcribe audio (the hosted, signed-in mode): reflections are typed. */
  canRecord: boolean;
  maxDurationSeconds: number;
  onJump: (snippetId: number) => void;
  onRenameQuestion: (id: number, name: string) => void;
  onAddQuestion: (name: string) => void;
  onReviewReady: (memo: VoiceMemo, reviewError: string | null) => void;
  onSaved: () => void;
  onClose: () => void;
}

export default function Notebook({
  conversation,
  highlights,
  researchQuestions,
  questionsLoaded,
  breadth,
  passages,
  findingRqId,
  findNotes,
  passageBusyId,
  onFindPassages,
  onAcceptPassage,
  onDismissPassage,
  onQuestionAdded,
  reflect,
  canRecord,
  maxDurationSeconds,
  onJump,
  onRenameQuestion,
  onAddQuestion,
  onReviewReady,
  onSaved,
  onClose,
}: Props) {
  const [jot, setJot] = useState("");
  // Writing is the only way to reflect where audio can't be stored, and it reopens when
  // something unsent is waiting.
  const [typing, setTyping] = useState(() => !canRecord || readTypedDraft(conversation.id) !== "");
  const snippetById = new Map(conversation.snippets.map((s) => [s.snippet_id, s]));
  const sorted = [...highlights].sort((a, b) => Number(b.starred) - Number(a.starred));

  function quote(h: Highlight) {
    return h.snippets
      .map((sp) => {
        const words = snippetById.get(sp.snippet_id)?.words ?? [];
        return words
          .slice(sp.start_index, sp.end_index)
          .map((w) => w.word)
          .join(" ");
      })
      .filter(Boolean)
      .join(" … ");
  }

  function submitJot(e: FormEvent) {
    e.preventDefault();
    if (!jot.trim()) return;
    onAddQuestion(jot.trim());
    setJot("");
  }

  return (
    <aside className="nb" aria-label={reflect ? "Reflect" : "Notebook"}>
      <div className="nbhead">
        <h2>{reflect ? "Reflect" : "Notebook"}</h2>
        <button className="btn quiet small" onClick={onClose} aria-label="Close notebook">
          <Icon name="x" />
        </button>
      </div>
      <div className="nbbody">
        <section>
          <h3>Your notes on this conversation</h3>
          {sorted.length === 0 && (
            <p className="hint">None yet. Select words in the transcript to highlight, star or add a note.</p>
          )}
          {sorted.map((h) => {
            const first = snippetById.get(h.snippets[0]?.snippet_id);
            return (
              <button className="nnote" key={h.id} onClick={() => h.snippets[0] && onJump(h.snippets[0].snippet_id)}>
                "{quote(h)}"
                <span>
                  {[h.starred ? "Starred" : "", first?.speaker_name ?? "", h.notes ?? ""].filter(Boolean).join(" · ")}
                </span>
              </button>
            );
          })}
        </section>
        <section>
          <h3>Your questions</h3>
          <p className="hint" style={{ marginBottom: 4 }}>
            Scratch paper. Reword one here, or jot a new one, without leaving the transcript. "Find more passages"
            looks through this conversation only.
          </p>
          {questionsLoaded && researchQuestions.length === 0 && <GuideIdeas emptyState onAdded={onQuestionAdded} />}
          <ul className="nbq">
            {researchQuestions.map((rq) => {
              const heard = breadth?.questions.find((q) => q.research_question_id === rq.id);
              const found = passages.filter((p) => p.research_question_id === rq.id);
              const note = findNotes[rq.id];
              return (
              <li key={rq.id}>
                <textarea
                  rows={2}
                  defaultValue={rq.name}
                  aria-label="Question"
                  onBlur={(e) => {
                    const name = e.target.value.trim();
                    if (name && name !== rq.name) onRenameQuestion(rq.id, name);
                    else e.target.value = rq.name;
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      e.currentTarget.blur();
                    }
                  }}
                />
                {heard && breadth && <span className="hint">{questionRowLine(heard, breadth.opened_conversation_count)}</span>}
                <div className="acts">
                  <button
                    className="btn outline small"
                    onClick={() => onFindPassages(rq.id)}
                    disabled={findingRqId !== null}
                    aria-label={`Find more passages for "${rq.name}" in this conversation`}
                  >
                    <AiMark /> {findingRqId === rq.id ? "Looking…" : "Find more passages"}
                  </button>
                </div>
                {note && (
                  <p className={note.error ? "err" : "hint"} role={note.error ? "alert" : "status"}>
                    {note.text}
                  </p>
                )}
                {found.length > 0 && (
                  <ul className="receipts">
                    {found.map((p) => (
                      <PassageItem
                        key={p.id}
                        suggestion={p}
                        showConversation={false}
                        busy={passageBusyId === p.id}
                        onAccept={() => onAcceptPassage(p)}
                        onDismiss={() => onDismissPassage(p)}
                        onShow={() => onJump(p.snippet_id)}
                      />
                    ))}
                  </ul>
                )}
              </li>
              );
            })}
          </ul>
          <form className="jot" onSubmit={submitJot}>
            <input
              className="inp"
              placeholder="Jot a question"
              aria-label="Jot a question"
              value={jot}
              onChange={(e) => setJot(e.target.value)}
            />
            <button className="btn small">Add</button>
          </form>
        </section>
      </div>
      {reflect &&
        (typing ? (
          <Writer
            conversationId={conversation.id}
            canRecord={canRecord}
            onReviewReady={onReviewReady}
            onSaved={onSaved}
            onRecordInstead={() => setTyping(false)}
          />
        ) : (
          <Recorder
            conversationId={conversation.id}
            maxDurationSeconds={maxDurationSeconds}
            onReviewReady={onReviewReady}
            onSaved={onSaved}
            onTypeInstead={() => setTyping(true)}
          />
        ))}
    </aside>
  );
}
