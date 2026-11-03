import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, audioUrlFor } from "../lib/api";
import { CodeFamilyCard, MergeSuggestionCard } from "../components/CodeFamilyCard";
import type {
  CodeFamilyState,
  MemoEntry,
  MemoResponse,
  NextStepSuggestion,
} from "../lib/types";

function formatTimestamp(seconds: number) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function EntryView({ entry, onChanged }: { entry: MemoEntry; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(entry.insight_text);

  async function save() {
    await api.editMemoEntry(entry.connection_id, { insight_text: text });
    setEditing(false);
    onChanged();
  }

  async function removeEvidence(idx: number) {
    const next = entry.evidence.filter((_, i) => i !== idx);
    await api.editMemoEntry(entry.connection_id, { evidence: next });
    onChanged();
  }

  async function retract() {
    await api.retractMemoEntry(entry.connection_id);
    onChanged();
  }

  return (
    <div className="memo-entry">
      {editing ? (
        <>
          <textarea rows={2} style={{ width: "100%" }} value={text} onChange={(e) => setText(e.target.value)} />
          <button className="primary" onClick={save}>
            Save
          </button>
          <button className="secondary" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </>
      ) : (
        <p style={{ margin: 0 }}>
          {entry.insight_text}
          {entry.edited_from_ai && <span style={{ fontSize: "0.7rem", color: "#999" }}> (edited from AI)</span>}
        </p>
      )}
      <div className="meta">
        <Link to={`/?conversation=${entry.conversation_id}`}>{entry.conversation_title}</Link>
        {" · "}
        {new Date(entry.filed_at).toLocaleString()}
        {" · "}
        {entry.voice_memo_audio_path && (
          <a
            href={`${audioUrlFor(entry.voice_memo_audio_path)}${entry.reflection_timestamp != null ? `#t=${Math.floor(entry.reflection_timestamp)}` : ""}`}
            target="_blank"
            rel="noreferrer"
          >
            {entry.reflection_timestamp != null
              ? `recording at ${formatTimestamp(entry.reflection_timestamp)}`
              : "recording"}
          </a>
        )}
      </div>
      <div className="meta">
        Evidence:
        {entry.evidence.map((e, i) => (
          <div key={i} style={{ marginLeft: "0.5rem" }}>
            <Link to={`/?conversation=${entry.conversation_id}&snippet=${e.snippet_id}`}>"{e.text || `snippet ${e.snippet_id}`}"</Link>{" "}
            ({e.source === "user" ? "yours" : "AI-suggested"}
            {e.starred ? ", starred" : ""})
            <button style={{ fontSize: "0.7rem" }} onClick={() => removeEvidence(i)}>
              ×
            </button>
          </div>
        ))}
      </div>
      <div style={{ marginTop: "0.3rem" }}>
        <button className="secondary" onClick={() => setEditing(true)}>
          Edit
        </button>
        <button className="secondary" onClick={retract} style={{ marginLeft: "0.3rem" }}>
          Retract
        </button>
      </div>
    </div>
  );
}

function NextStepView({ suggestion, onChanged }: { suggestion: NextStepSuggestion; onChanged: () => void }) {
  async function accept() {
    await api.acceptNextStep(suggestion.id);
    onChanged();
  }
  async function dismiss() {
    await api.dismissNextStep(suggestion.id);
    onChanged();
  }

  return (
    <div className="memo-entry">
      {suggestion.suggested_direction && <p style={{ margin: 0 }}>{suggestion.suggested_direction}</p>}
      {suggestion.recommended_conversation_id != null && (
        <p style={{ margin: 0 }}>
          Listen to:{" "}
          <Link to={`/?conversation=${suggestion.recommended_conversation_id}`}>
            <em>{suggestion.recommended_conversation_title}</em>
          </Link>
        </p>
      )}
      <div className="meta">{suggestion.rationale}</div>
      {suggestion.status === "suggested" ? (
        <div style={{ marginTop: "0.3rem" }}>
          <button className="primary" onClick={accept}>
            Accept
          </button>
          <button className="secondary" onClick={dismiss} style={{ marginLeft: "0.3rem" }}>
            Dismiss
          </button>
        </div>
      ) : (
        <div className="meta" style={{ fontStyle: "italic" }}>
          {suggestion.status}
        </div>
      )}
    </div>
  );
}

function CodeFamiliesSection({ researchQuestionId }: { researchQuestionId: number }) {
  const [state, setState] = useState<CodeFamilyState | null>(null);
  const [synthesizing, setSynthesizing] = useState(false);

  function refresh(next?: CodeFamilyState) {
    if (next) setState(next);
    else api.getCodeFamilyState(researchQuestionId).then(setState);
  }

  useEffect(() => refresh(), [researchQuestionId]);

  async function synthesize(overrideCap = false) {
    setSynthesizing(true);
    try {
      setState(await api.synthesizeCodeFamilies(researchQuestionId, overrideCap));
    } finally {
      setSynthesizing(false);
    }
  }

  const cap = state?.cap;
  return (
    <div style={{ marginTop: "0.75rem" }}>
      <button className="secondary" onClick={() => synthesize()} disabled={synthesizing}>
        {synthesizing ? "Synthesizing…" : "Synthesize code families"}
      </button>
      {cap && cap.live_count > 0 && (
        <span className="meta" style={{ marginLeft: "0.5rem" }}>
          {cap.live_count} of {cap.max_per_rq} families
        </span>
      )}
      {cap?.at_cap && (
        <p className="meta" style={{ margin: "0.4rem 0" }}>
          {state!.withheld_new_families > 0
            ? `At the ${cap.max_per_rq}-family cap — ${state!.withheld_new_families} new theme${state!.withheld_new_families > 1 ? "s were" : " was"} held back. `
            : `At the ${cap.max_per_rq}-family cap, so synthesis only attaches entries to existing families. `}
          Merge or dismiss a family to make room, or{" "}
          <button className="secondary" style={{ padding: "0.1rem 0.5rem" }} onClick={() => synthesize(true)} disabled={synthesizing}>
            synthesize past the cap
          </button>
        </p>
      )}
      {state?.merge_suggestions.map((m) => (
        <MergeSuggestionCard key={m.id} merge={m} families={state.families} onChanged={refresh} />
      ))}
      {state?.families.map((f) => (
        <CodeFamilyCard key={f.id} family={f} onChanged={() => refresh()} />
      ))}
    </div>
  );
}

export default function MemoPage() {
  const [memo, setMemo] = useState<MemoResponse | null>(null);
  const [nextSteps, setNextSteps] = useState<NextStepSuggestion[]>([]);
  const [generatingNextSteps, setGeneratingNextSteps] = useState(false);

  function refresh() {
    api.getMemo().then(setMemo);
  }

  function refreshNextSteps() {
    api.listNextSteps().then(setNextSteps);
  }

  useEffect(() => {
    refresh();
    refreshNextSteps();
  }, []);

  async function suggestNextSteps() {
    setGeneratingNextSteps(true);
    try {
      await api.generateNextSteps();
      refreshNextSteps();
    } finally {
      setGeneratingNextSteps(false);
    }
  }

  if (!memo) return <p style={{ padding: "2rem" }}>Loading…</p>;

  return (
    <div style={{ maxWidth: 800, margin: "0 auto", padding: "2rem" }}>
      <h1>Running RQ memo</h1>

      <div className="memo-section">
        <h2>What to listen to next</h2>
        <button className="primary" onClick={suggestNextSteps} disabled={generatingNextSteps}>
          {generatingNextSteps ? "Thinking…" : "Suggest next steps"}
        </button>
        {nextSteps.length === 0 && <p style={{ color: "#999" }}>No suggestions yet.</p>}
        {nextSteps.map((s) => (
          <NextStepView key={s.id} suggestion={s} onChanged={refreshNextSteps} />
        ))}
      </div>

      {memo.sections.map((section) => (
        <div className="memo-section" key={section.research_question_id}>
          <h2>{section.name}</h2>
          {section.entries.length === 0 && <p style={{ color: "#999" }}>No entries yet.</p>}
          {section.entries.map((e) => (
            <EntryView key={e.connection_id} entry={e} onChanged={refresh} />
          ))}
          <CodeFamiliesSection researchQuestionId={section.research_question_id} />
        </div>
      ))}

      {memo.unmapped.length > 0 && (
        <div className="memo-section">
          <h2>Unmapped reflections</h2>
          {memo.unmapped.map((e) => (
            <EntryView key={e.connection_id} entry={e} onChanged={refresh} />
          ))}
        </div>
      )}

      {memo.starred_unassigned.length > 0 && (
        <div className="memo-section">
          <h2>Starred, unassigned</h2>
          {memo.starred_unassigned.map((group) => (
            <div key={group.conversation_id} style={{ marginBottom: "1rem" }}>
              <Link to={`/?conversation=${group.conversation_id}`}>{group.conversation_title}</Link>
              {group.highlights.map((h) => (
                <div key={h.highlight_id} style={{ fontSize: "0.9rem", marginLeft: "1rem" }}>
                  ★ {h.notes || "(no memo text)"}
                  {h.snippets[0]?.text && <span style={{ color: "#888" }}> — "{h.snippets[0].text}"</span>}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
