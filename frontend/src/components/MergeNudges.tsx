import { useState } from "react";
import { api, errorText } from "../lib/api";
import { plural, useToast } from "../lib/format";
import type { InsightMergeNudge, QuestionMergeNudge } from "../lib/types";

interface NudgeProps {
  onChanged: () => void;
  onError: (message: string) => void;
}

/** Two look-alike questions, with the wording the AI suggests for the merged one. */
export function QuestionNudge({
  nudge,
  onChanged,
  onError,
  onMerged,
}: NudgeProps & { nudge: QuestionMergeNudge; onMerged: (targetId: number) => void }) {
  const toast = useToast();
  const [keep, setKeep] = useState(nudge.default_target_id ?? nudge.items[0]?.id);
  const [useWording, setUseWording] = useState(false);
  const [busy, setBusy] = useState(false);
  if (nudge.items.length < 2) return null;

  async function undo(mergeId: number, reworded: boolean) {
    try {
      await api.undoResearchQuestionMerge(mergeId);
      onChanged();
      toast(reworded ? "Merge undone. The new wording stays until you reword it." : "Merge undone.");
    } catch (e) {
      onError(errorText(e));
    }
  }

  async function merge() {
    setBusy(true);
    const reworded = useWording && !!nudge.suggested_wording;
    try {
      const res = await api.acceptMergeNudge(nudge.id, { target_id: keep, apply_suggested_wording: reworded });
      onMerged(res.target_id);
      onChanged();
      toast("Merged. Its insights and highlights moved with it.", {
        label: "Undo",
        run: () => undo(res.merge.id, reworded),
      });
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function keepSeparate() {
    setBusy(true);
    try {
      await api.dismissMergeNudge(nudge.id);
      onChanged();
      toast("Kept separate. It won't be suggested again.");
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="nudge" style={{ margin: 0 }}>
      <span className="tag ai">Suggested by AI</span>
      <h3>Two of your questions look alike</h3>
      <fieldset>
        <legend>Which one stays?</legend>
        {nudge.items.map((q) => (
          <label key={q.id}>
            <input type="radio" name={`keep-question-${nudge.id}`} checked={keep === q.id} onChange={() => setKeep(q.id)} />
            <span>
              {q.name}{" "}
              <span className="hint">
                {plural(q.insight_count, "insight")}, {plural(q.highlight_count, "highlight")}
              </span>
            </span>
          </label>
        ))}
      </fieldset>
      {nudge.reason && (
        <p className="hint" style={{ marginBottom: 12 }}>
          {nudge.reason}
        </p>
      )}
      {nudge.suggested_wording && (
        <label className="check">
          <input type="checkbox" checked={useWording} onChange={(e) => setUseWording(e.target.checked)} />
          <span>Use the suggested wording: "{nudge.suggested_wording}"</span>
        </label>
      )}
      <div className="acts">
        <button className="btn small" onClick={merge} disabled={busy}>
          Merge
        </button>
        <button className="btn quiet small" onClick={keepSeparate} disabled={busy}>
          Keep separate
        </button>
      </div>
    </div>
  );
}

/** Two insights under one question that say nearly the same thing. */
export function InsightNudge({ nudge, onChanged, onError }: NudgeProps & { nudge: InsightMergeNudge }) {
  const toast = useToast();
  const [keep, setKeep] = useState(nudge.default_target_id ?? nudge.items[0]?.id);
  const [busy, setBusy] = useState(false);
  if (nudge.items.length < 2) return null;
  const preview = nudge.merged_preview;

  async function undo(mergeId: number) {
    try {
      await api.undoInsightMerge(mergeId);
      onChanged();
      toast("Merge undone. Both insights are back.");
    } catch (e) {
      onError(errorText(e));
    }
  }

  async function merge() {
    setBusy(true);
    try {
      const res = await api.acceptMergeNudge(nudge.id, { target_id: keep });
      onChanged();
      toast(
        preview
          ? `Merged. ${plural(preview.evidence_count, "highlight")} from ${plural(preview.conversation_count, "conversation")} now point to one insight.`
          : "Merged into one insight.",
        { label: "Undo", run: () => undo(res.merge.id) }
      );
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function keepSeparate() {
    setBusy(true);
    try {
      await api.dismissMergeNudge(nudge.id);
      onChanged();
      toast("Kept separate. It won't be suggested again.");
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="nudge">
      <span className="tag ai">Suggested by AI</span>
      <h3>These two insights say nearly the same thing. Merge them?</h3>
      <fieldset>
        <legend>Which wording stays?</legend>
        {nudge.items.map((i) => (
          <label key={i.id}>
            <input type="radio" name={`keep-insight-${nudge.id}`} checked={keep === i.id} onChange={() => setKeep(i.id)} />
            <span>
              {i.insight_text}{" "}
              <span className="hint">
                {[plural(i.evidence_count, "highlight"), i.conversation_title].filter(Boolean).join(" · ")}
              </span>
            </span>
          </label>
        ))}
      </fieldset>
      <p className="hint" style={{ marginBottom: 12 }}>
        {nudge.reason ? `${nudge.reason} ` : ""}
        {preview
          ? `Merged, it would have ${plural(preview.evidence_count, "highlight")} from ${plural(preview.conversation_count, "conversation")}. `
          : ""}
        You can reword it after.
      </p>
      <div className="acts">
        <button className="btn small" onClick={merge} disabled={busy}>
          Merge
        </button>
        <button className="btn quiet small" onClick={keepSeparate} disabled={busy}>
          Keep separate
        </button>
      </div>
    </div>
  );
}
