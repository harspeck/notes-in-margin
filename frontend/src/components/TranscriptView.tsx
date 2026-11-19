import { useEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import type {
  ConversationDetail,
  CorticoHighlight,
  GuideQuestion,
  Highlight,
  HighlightSnippetSpan,
  PassageSuggestion,
  ResearchQuestion,
  Snippet,
  TeamHighlight,
} from "../lib/types";
import { clock, plural } from "../lib/format";
import { Icon } from "../lib/ui";

export type HighlightKind = "plain" | "star" | "note";

interface Props {
  conversation: ConversationDetail;
  highlights: Highlight[];
  /** Highlights made in Cortico by other people and tools. Shown, never edited. */
  corticoHighlights: CorticoHighlight[];
  /** Highlights teammates made here on this conversation. Shown, never edited. */
  teamHighlights: TeamHighlight[];
  onCreateHighlight: (spans: HighlightSnippetSpan[], kind: HighlightKind) => void;
  onUpdateHighlight: (id: number, body: { starred?: boolean; notes?: string }) => void;
  onDeleteHighlight: (id: number) => void;
  onRetryHighlightSync: (id: number) => void;
  /** The user's active questions, for filing a highlight under them. */
  researchQuestions: ResearchQuestion[];
  onSetHighlightQuestions: (id: number, researchQuestionIds: number[]) => void;
  /** Undecided passages the AI found in this conversation, shown dashed until the user decides. */
  passages: PassageSuggestion[];
  passageBusyId: number | null;
  onAcceptPassage: (suggestion: PassageSuggestion) => void;
  onDismissPassage: (suggestion: PassageSuggestion) => void;
  /** The highlight whose note editor should open, set right after "Add note". */
  editingNoteId: number | null;
  onEditNote: (id: number | null) => void;
  armedQuestionId: number | null;
  onArm: (guideQuestionId: number | null) => void;
  onDropCheckpoint: (snippetId: number) => void;
  onDeleteCheckpoint: (guideQuestionId: number) => void;
  flashSnippetId: number | null;
  /** Plays the conversation from a time. Null when it has no audio. */
  onSeek: ((seconds: number) => void) | null;
  /** Rendered after the last turn, inside the reading column. */
  children?: ReactNode;
}

interface Menu {
  spans: HighlightSnippetSpan[];
  x: number;
  y: number;
}

function highlightForWord(highlights: Highlight[], snippetId: number, wordIndex: number): Highlight | null {
  for (const h of highlights) {
    for (const s of h.snippets) {
      if (s.snippet_id === snippetId && wordIndex >= s.start_index && wordIndex < s.end_index) return h;
    }
  }
  return null;
}

/** A highlight can span several speaker turns; its margin note sits beside the last one. */
function lastSnippetIdForHighlight(h: { snippets: HighlightSnippetSpan[] }, orderIndexBySnippetId: Map<number, number>): number {
  return h.snippets.reduce((lastId, s) => {
    const a = orderIndexBySnippetId.get(s.snippet_id) ?? -1;
    const b = orderIndexBySnippetId.get(lastId) ?? -1;
    return a > b ? s.snippet_id : lastId;
  }, h.snippets[0]?.snippet_id);
}

const sectionName = (q: GuideQuestion) => q.shorthand_code ?? q.text;

const inPassage = (passages: PassageSuggestion[], snippetId: number, wordIndex: number) =>
  passages.some((p) => p.snippet_id === snippetId && wordIndex >= p.start_index && wordIndex < p.end_index);

const inTheirs = (theirs: { snippets: HighlightSnippetSpan[] }[], snippetId: number, wordIndex: number) =>
  theirs.some((h) =>
    h.snippets.some((sp) => sp.snippet_id === snippetId && wordIndex >= sp.start_index && wordIndex < sp.end_index)
  );

function CorticoNote({ highlight, onSeek }: { highlight: CorticoHighlight; onSeek: ((seconds: number) => void) | null }) {
  const by = highlight.mine ? "You made this in Cortico" : highlight.author_name ? `By ${highlight.author_name}` : null;
  return (
    <aside className="mnote cortico" data-cortico-highlight={highlight.cortico_id}>
      <div className="top">
        <span className="tag cortico">In Cortico</span>
      </div>
      {highlight.title && <p className="for">{highlight.title}</p>}
      {highlight.description && <p>{highlight.description}</p>}
      <p className="by">
        {[by, highlight.tool_id].filter(Boolean).join(" · ")}
        {onSeek && (
          <button className="linkb" onClick={() => onSeek(highlight.start_offset)}>
            Play from {clock(highlight.start_offset)}
          </button>
        )}
      </p>
    </aside>
  );
}

function TeamNote({ highlight, playFrom, onSeek }: { highlight: TeamHighlight; playFrom: number | null; onSeek: ((seconds: number) => void) | null }) {
  return (
    <aside className="mnote team" data-team-highlight={highlight.highlight_id}>
      <div className="top">
        <span className="tag team">Teammate</span>
        {highlight.starred && (
          <span className="starred" role="img" aria-label="Starred" title="Starred">
            <Icon name="star" fill />
          </span>
        )}
      </div>
      {highlight.notes && <p>{highlight.notes}</p>}
      {highlight.question_names.length > 0 && (
        <div className="mq">
          {highlight.question_names.map((name) => (
            <span className="chip" key={name} title={name}>
              <span className="t">{name}</span>
            </span>
          ))}
        </div>
      )}
      <p className="by">
        By {highlight.author.name}
        {onSeek && playFrom !== null && (
          <button className="linkb" onClick={() => onSeek(playFrom)}>
            Play from {clock(playFrom)}
          </button>
        )}
      </p>
    </aside>
  );
}

/** The questions a highlight is filed under, as removable chips, and a picker to add one. */
function HighlightQuestions({
  highlight,
  questions,
  onSet,
}: {
  highlight: Highlight;
  questions: ResearchQuestion[];
  onSet: (researchQuestionIds: number[]) => void;
}) {
  if (questions.length === 0) return null;
  // The route replaces the whole set and refuses an archived question, so only the questions
  // still active are sent back.
  const filed = questions.filter((q) => highlight.research_question_ids.includes(q.id));
  const filedIds = filed.map((q) => q.id);
  const addable = questions.filter((q) => !filedIds.includes(q.id));
  return (
    <div className="mq">
      {filed.map((q) => (
        <span className="chip" key={q.id} title={q.name}>
          <span className="t">{q.name}</span>
          <button
            className="iconb"
            aria-label={`Take this highlight out of the question "${q.name}"`}
            onClick={() => onSet(filedIds.filter((id) => id !== q.id))}
          >
            <Icon name="x" />
          </button>
        </span>
      ))}
      {addable.length > 0 && (
        <select
          className="qpick"
          aria-label="Add to a question"
          value=""
          onChange={(e) => {
            if (e.target.value) onSet([...filedIds, Number(e.target.value)]);
          }}
        >
          <option value="">Add to a question</option>
          {addable.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function FoundPassage({
  suggestion,
  questionName,
  busy,
  onAccept,
  onDismiss,
}: {
  suggestion: PassageSuggestion;
  questionName: string;
  busy: boolean;
  onAccept: () => void;
  onDismiss: () => void;
}) {
  return (
    <aside className="mnote found" data-passage={suggestion.id}>
      <div className="top">
        <span className="tag found">Passage the AI found</span>
      </div>
      <p className="for">For your question: {questionName}</p>
      {suggestion.reason && <p>{suggestion.reason}</p>}
      <div className="acts">
        <button className="btn small" onClick={onAccept} disabled={busy}>
          Add as a highlight
        </button>
        <button className="btn quiet small" onClick={onDismiss} disabled={busy}>
          Not this
        </button>
      </div>
    </aside>
  );
}

function MarginNote({
  highlight,
  questions,
  editing,
  onEdit,
  onUpdate,
  onSetQuestions,
  onDelete,
  onRetrySync,
}: {
  highlight: Highlight;
  questions: ResearchQuestion[];
  onSetQuestions: (researchQuestionIds: number[]) => void;
  editing: boolean;
  onEdit: (on: boolean) => void;
  onUpdate: (body: { starred?: boolean; notes?: string }) => void;
  onDelete: () => void;
  onRetrySync: () => void;
}) {
  const [draft, setDraft] = useState(highlight.notes ?? "");
  return (
    <aside className="mnote">
      <div className="top">
        <button
          className={"iconb" + (highlight.starred ? " on" : "")}
          aria-pressed={highlight.starred}
          aria-label={highlight.starred ? "Remove star" : "Star this highlight"}
          onClick={() => onUpdate({ starred: !highlight.starred })}
        >
          <Icon name="star" fill={highlight.starred} />
        </button>
        {highlight.notes ? "Your note" : "Your highlight"}
        <span className="sp" />
        <button className="iconb" aria-label="Remove this highlight" onClick={onDelete}>
          <Icon name="x" />
        </button>
      </div>
      {editing ? (
        <>
          <textarea
            className="inp"
            rows={3}
            autoFocus
            aria-label="Note"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="acts">
            <button
              className="btn small"
              onClick={() => {
                onUpdate({ notes: draft.trim() });
                onEdit(false);
              }}
            >
              Save
            </button>
            <button
              className="btn quiet small"
              onClick={() => {
                setDraft(highlight.notes ?? "");
                onEdit(false);
              }}
            >
              Cancel
            </button>
          </div>
        </>
      ) : (
        <button className={"body" + (highlight.notes ? "" : " empty")} onClick={() => onEdit(true)}>
          {highlight.notes || "Add a note"}
        </button>
      )}
      <HighlightQuestions highlight={highlight} questions={questions} onSet={onSetQuestions} />
      {highlight.cortico_sync?.status === "failed" && (
        <div className="syncfail" role="status">
          Not saved to Cortico yet.
          <button className="linkb" onClick={onRetrySync}>
            Try again
          </button>
        </div>
      )}
    </aside>
  );
}

export default function TranscriptView({
  conversation,
  highlights,
  corticoHighlights,
  teamHighlights,
  onCreateHighlight,
  onUpdateHighlight,
  onDeleteHighlight,
  onRetryHighlightSync,
  researchQuestions,
  onSetHighlightQuestions,
  passages,
  passageBusyId,
  onAcceptPassage,
  onDismissPassage,
  editingNoteId,
  onEditNote,
  armedQuestionId,
  onArm,
  onDropCheckpoint,
  onDeleteCheckpoint,
  flashSnippetId,
  onSeek,
  children,
}: Props) {
  const [menu, setMenu] = useState<Menu | null>(null);
  const [showOthers, setShowOthers] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  const orderIndexBySnippetId = new Map(conversation.snippets.map((s) => [s.snippet_id, s.order_index]));
  const wordCountBySnippetId = new Map(conversation.snippets.map((s) => [s.snippet_id, s.words.length]));
  const sectionsBySnippetId = new Map<number, GuideQuestion[]>();
  for (const q of conversation.guide_questions) {
    if (q.checkpoint_snippet_id === null) continue;
    sectionsBySnippetId.set(q.checkpoint_snippet_id, [...(sectionsBySnippetId.get(q.checkpoint_snippet_id) ?? []), q]);
  }
  const snippetById = new Map(conversation.snippets.map((s) => [s.snippet_id, s]));
  const teammates = teamHighlights.filter((h) => h.snippets.length > 0);
  // A teammate's highlight that was also written to Cortico comes back in both lists; the
  // teammate's copy is the one shown.
  const teamCorticoIds = new Set(teammates.filter((h) => h.cortico_id !== null).map((h) => String(h.cortico_id)));
  const inCorticoOnly = corticoHighlights.filter((h) => !teamCorticoIds.has(String(h.cortico_id)));
  const othersCount = teammates.length + inCorticoOnly.length;
  const unmarked = conversation.guide_questions.filter((q) => q.checkpoint_snippet_id === null);
  const armed = conversation.guide_questions.find((q) => q.guide_question_id === armedQuestionId) ?? null;

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest(".selmenu, .ts")) setMenu(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  /** Double-click plays from the word, so with audio it no longer selects the word. */
  function playFromWord(e: ReactMouseEvent, s: Snippet) {
    if (!onSeek) return;
    const el = (e.target as HTMLElement).closest("[data-widx]");
    if (!el) return;
    const start = s.words[Number(el.getAttribute("data-widx"))]?.start;
    // A word without a usable time plays from the start of its turn.
    const usable = typeof start === "number" && start >= s.start_offset - 1 && start <= s.end_offset + 1;
    onSeek(usable ? start : s.start_offset);
    window.getSelection()?.removeAllRanges();
    setMenu(null);
  }

  /** Where a teammate's highlight starts in the audio, from its first word. */
  function teamStart(h: TeamHighlight): number | null {
    const first = h.snippets.reduce((a, b) =>
      (orderIndexBySnippetId.get(b.snippet_id) ?? Infinity) < (orderIndexBySnippetId.get(a.snippet_id) ?? Infinity) ? b : a
    );
    const s = snippetById.get(first.snippet_id);
    if (!s) return null;
    const start = s.words[first.start_index]?.start;
    const usable = typeof start === "number" && start >= s.start_offset - 1 && start <= s.end_offset + 1;
    return usable ? start : s.start_offset;
  }

  function handleMouseUp(e: ReactMouseEvent) {
    if (onSeek && e.detail === 2) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0 || !containerRef.current) {
      setMenu(null);
      return;
    }
    const range = sel.getRangeAt(0);
    const startEl = (range.startContainer.parentElement as HTMLElement)?.closest("[data-widx]");
    const endEl = (range.endContainer.parentElement as HTMLElement)?.closest("[data-widx]");
    const startSnippetEl = startEl?.closest("[data-snippet-id]") as HTMLElement | null | undefined;
    const endSnippetEl = endEl?.closest("[data-snippet-id]") as HTMLElement | null | undefined;
    if (!startEl || !endEl || !startSnippetEl || !endSnippetEl) {
      setMenu(null);
      return;
    }

    // A highlight can cross speaker turns: walk every turn between the selection's two ends
    // (the selection may run backwards) and build one span per turn.
    const allSnippetEls = Array.from(containerRef.current.querySelectorAll("[data-snippet-id]"));
    const startPos = allSnippetEls.indexOf(startSnippetEl);
    const endPos = allSnippetEls.indexOf(endSnippetEl);
    const startWordIdx = parseInt(startEl.getAttribute("data-widx")!, 10);
    const endWordIdx = parseInt(endEl.getAttribute("data-widx")!, 10);
    const forward = startPos < endPos || (startPos === endPos && startWordIdx <= endWordIdx);
    const [loPos, hiPos] = forward ? [startPos, endPos] : [endPos, startPos];
    const firstWordIdx = forward ? startWordIdx : endWordIdx;
    const lastWordIdx = forward ? endWordIdx : startWordIdx;
    const spanEls = allSnippetEls.slice(loPos, hiPos + 1) as HTMLElement[];

    const spans: HighlightSnippetSpan[] = spanEls.map((el, i) => {
      const snippetId = parseInt(el.getAttribute("data-snippet-id")!, 10);
      return {
        snippet_id: snippetId,
        start_index: i === 0 ? firstWordIdx : 0,
        end_index: i === spanEls.length - 1 ? lastWordIdx + 1 : (wordCountBySnippetId.get(snippetId) ?? lastWordIdx + 1),
      };
    });

    const rect = range.getBoundingClientRect();
    setMenu({ spans, x: Math.max(8, rect.left + window.scrollX), y: rect.top + window.scrollY - 46 });
  }

  function choose(kind: HighlightKind) {
    if (!menu) return;
    onCreateHighlight(menu.spans, kind);
    window.getSelection()?.removeAllRanges();
    setMenu(null);
  }

  return (
    <article className="ts" ref={containerRef} onMouseUp={handleMouseUp}>
      {(unmarked.length > 0 || armed) && (
        <ul className="seclist" aria-label="Guide sections not marked yet">
          {armed && (
            <li className="armed">
              <span>Choose the line where "{sectionName(armed)}" starts, then press Start here beside it.</span>
              <button className="btn quiet small" onClick={() => onArm(null)}>
                Cancel
              </button>
            </li>
          )}
          {unmarked
            .filter((q) => q.guide_question_id !== armedQuestionId)
            .map((q) => (
              <li key={q.guide_question_id}>
                <span>
                  <b>{sectionName(q)}</b> · not marked yet
                </span>
                <button className="btn quiet small" onClick={() => onArm(q.guide_question_id)}>
                  Set where this starts
                </button>
              </li>
            ))}
        </ul>
      )}

      {(onSeek || othersCount > 0) && (
        <div className="tshint">
          {onSeek && <span>Double-click a word to play from there.</span>}
          {othersCount > 0 && (
            <label>
              <input type="checkbox" checked={showOthers} onChange={(e) => setShowOthers(e.target.checked)} />
              Show {plural(othersCount, "highlight")} from others
            </label>
          )}
        </div>
      )}

      {conversation.snippets.map((s) => {
        const speaker = s.speaker_name ?? "Unknown speaker";
        const theirs = showOthers
          ? inCorticoOnly.filter((h) => h.snippets.some((sp) => sp.snippet_id === s.snippet_id))
          : [];
        const team = showOthers ? teammates.filter((h) => h.snippets.some((sp) => sp.snippet_id === s.snippet_id)) : [];
        const teamNotes = team.filter((h) => lastSnippetIdForHighlight(h, orderIndexBySnippetId) === s.snippet_id);
        // A highlight can span several turns; its note sits beside the last one.
        const theirNotes = theirs.filter((h) => h.snippets[h.snippets.length - 1].snippet_id === s.snippet_id);
        const notes = highlights.filter(
          (h) =>
            h.snippets.some((sp) => sp.snippet_id === s.snippet_id) &&
            lastSnippetIdForHighlight(h, orderIndexBySnippetId) === s.snippet_id
        );
        const found = passages.filter((p) => p.snippet_id === s.snippet_id);
        return (
          <div key={s.snippet_id}>
            {(sectionsBySnippetId.get(s.snippet_id) ?? []).map((q) => (
              <div className="sec" key={q.guide_question_id}>
                <b>{sectionName(q)}</b>
                <span className="meta">
                  Starts at {speaker}, {clock(s.start_offset)} · you set this
                </span>
                <span className="acts">
                  <button className="btn quiet small" onClick={() => onArm(q.guide_question_id)}>
                    Move
                  </button>
                  <button className="btn quiet small" onClick={() => onDeleteCheckpoint(q.guide_question_id)}>
                    Remove
                  </button>
                </span>
              </div>
            ))}
            <div
              className={"turn" + (flashSnippetId === s.snippet_id ? " flash" : "")}
              data-snippet-id={s.snippet_id}
            >
              <div className="who">
                {speaker}
                {onSeek ? (
                  <button
                    className="time"
                    onClick={() => onSeek(s.start_offset)}
                    aria-label={`Play from ${speaker}, ${clock(s.start_offset)}`}
                  >
                    <Icon name="play" fill />
                    {clock(s.start_offset)}
                  </button>
                ) : (
                  <span className="time">{clock(s.start_offset)}</span>
                )}
                {armed && (
                  <button className="btn outline small" onClick={() => onDropCheckpoint(s.snippet_id)}>
                    Start here
                  </button>
                )}
              </div>
              <p onDoubleClick={(e) => playFromWord(e, s)}>
                {s.words.map((w, i) => {
                  const highlight = highlightForWord(highlights, s.snippet_id, i);
                  const word = (
                    <span key={i} data-widx={i}>
                      {w.word}{" "}
                    </span>
                  );
                  if (!highlight) {
                    if (found.length > 0 && inPassage(found, s.snippet_id, i))
                      return (
                        <mark key={i} className="hl found">
                          {word}
                        </mark>
                      );
                    if (team.length > 0 && inTheirs(team, s.snippet_id, i))
                      return (
                        <mark key={i} className="hl team">
                          {word}
                        </mark>
                      );
                    if (theirs.length === 0 || !inTheirs(theirs, s.snippet_id, i)) return word;
                    return (
                      <mark key={i} className="hl cortico">
                        {word}
                      </mark>
                    );
                  }
                  return (
                    <mark key={i} className={"hl" + (highlight.starred ? " star" : "")}>
                      {word}
                    </mark>
                  );
                })}
              </p>
              {(notes.length > 0 || found.length > 0 || theirNotes.length > 0 || teamNotes.length > 0) && (
                <div className="mnotes">
                  {notes.map((h) => (
                    <MarginNote
                      key={h.id}
                      highlight={h}
                      questions={researchQuestions}
                      onSetQuestions={(ids) => onSetHighlightQuestions(h.id, ids)}
                      editing={editingNoteId === h.id}
                      onEdit={(on) => onEditNote(on ? h.id : null)}
                      onUpdate={(body) => onUpdateHighlight(h.id, body)}
                      onDelete={() => onDeleteHighlight(h.id)}
                      onRetrySync={() => onRetryHighlightSync(h.id)}
                    />
                  ))}
                  {teamNotes.map((h) => (
                    <TeamNote key={h.highlight_id} highlight={h} playFrom={teamStart(h)} onSeek={onSeek} />
                  ))}
                  {theirNotes.map((h) => (
                    <CorticoNote key={h.cortico_id} highlight={h} onSeek={onSeek} />
                  ))}
                  {found.map((p) => (
                    <FoundPassage
                      key={p.id}
                      suggestion={p}
                      questionName={
                        researchQuestions.find((q) => q.id === p.research_question_id)?.name ?? "a question you archived"
                      }
                      busy={passageBusyId === p.id}
                      onAccept={() => onAcceptPassage(p)}
                      onDismiss={() => onDismissPassage(p)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}

      {children}

      {menu && (
        <div className="selmenu" style={{ left: menu.x, top: menu.y }} onMouseDown={(e) => e.preventDefault()}>
          <button onClick={() => choose("plain")}>Highlight</button>
          <button onClick={() => choose("star")}>Star</button>
          <button onClick={() => choose("note")}>Add note</button>
        </div>
      )}
    </article>
  );
}
