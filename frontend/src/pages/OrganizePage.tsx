import { useCallback, useEffect, useState } from "react";
import { api, errorText } from "../lib/api";
import { CodeFamilyCard, EntryPicker } from "../components/CodeFamilyCard";
import type { CodebookPrep, OrganizeGroup, OrganizeResponse, ResearchQuestion } from "../lib/types";

function toggleId(ids: number[], id: number) {
  return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
}

function localDate() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function downloadJson(data: unknown, filename: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function useAction(onDone: () => void) {
  const [error, setError] = useState<string | null>(null);
  async function run(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      onDone();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    }
  }
  return { error, run };
}

function MergedSources({ group, onChanged }: { group: OrganizeGroup; onChanged: (notice?: string) => void }) {
  const { error, run } = useAction(() => {});
  if (group.merged_sources.length === 0) return null;

  const undo = (mergeId: number) =>
    run(async () => {
      const result = await api.undoResearchQuestionMerge(mergeId);
      const kept = result.families_kept_with_target.map((f) => `"${f.name}"`);
      onChanged(
        kept.length > 0
          ? `Undid the merge of "${result.source.name}". ${kept.join(", ")} stayed with "${result.target.name}" because they mix entries from both.`
          : `Undid the merge of "${result.source.name}".`
      );
    });

  return (
    <div className="meta">
      includes:{" "}
      {group.merged_sources.map((s, i) => (
        <span key={s.merge_id}>
          {i > 0 && ", "}
          {s.name ?? `#${s.id}`}{" "}
          <button className="link-button" onClick={() => undo(s.merge_id)}>
            (undo)
          </button>
        </span>
      ))}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

function MergeIntoSelect({
  rq,
  targets,
  onChanged,
}: {
  rq: ResearchQuestion;
  targets: ResearchQuestion[];
  onChanged: () => void;
}) {
  const { error, run } = useAction(onChanged);
  const options = targets.filter((t) => t.id !== rq.id);

  function merge(targetId: number) {
    const target = options.find((t) => t.id === targetId);
    if (!target) return;
    if (!window.confirm(`Merge "${rq.name}" into "${target.name}"? Its entries and families will read under "${target.name}". You can undo this.`))
      return;
    run(() => api.mergeResearchQuestion(rq.id, targetId));
  }

  return (
    <span>
      <select value="" onChange={(e) => merge(Number(e.target.value))} disabled={options.length === 0}>
        <option value="">Merge into…</option>
        {options.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
      {error && <span className="error-text"> {error}</span>}
    </span>
  );
}

function NewFamilyForm({ group, onDone }: { group: OrganizeGroup; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [definition, setDefinition] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const { error, run } = useAction(onDone);

  async function create() {
    const ok = await run(() =>
      api.createCodeFamily(group.rq.id, { name, definition, connection_ids: picked })
    );
    if (ok) {
      setOpen(false);
      setName("");
      setDefinition("");
      setPicked([]);
    }
  }

  if (!open)
    return (
      <button className="secondary" onClick={() => setOpen(true)}>
        New family
      </button>
    );

  return (
    <div className="organize-form">
      <input placeholder="Family name" value={name} onChange={(e) => setName(e.target.value)} />
      <textarea
        rows={2}
        placeholder="Definition: what belongs in this family"
        value={definition}
        onChange={(e) => setDefinition(e.target.value)}
      />
      <div className="meta">Entries</div>
      <EntryPicker
        entries={group.entries.map((e) => ({
          ...e,
          conversation_title:
            e.family_ids.length > 0
              ? `${e.conversation_title ?? "conversation"} (in ${e.family_ids.length} famil${e.family_ids.length === 1 ? "y" : "ies"})`
              : e.conversation_title,
        }))}
        selected={picked}
        onToggle={(id) => setPicked(toggleId(picked, id))}
      />
      {group.cap.at_cap && (
        <p className="warning-text">
          This research question is already at the {group.cap.max_per_rq}-family cap. The family will still be
          created.
        </p>
      )}
      <button className="primary" onClick={create} disabled={!name.trim() || !definition.trim()}>
        Create family
      </button>
      <button className="secondary" onClick={() => setOpen(false)} style={{ marginLeft: "0.3rem" }}>
        Cancel
      </button>
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

function MergeFamiliesModal({
  group,
  familyIds,
  onClose,
  onDone,
}: {
  group: OrganizeGroup;
  familyIds: number[];
  onClose: () => void;
  onDone: () => void;
}) {
  const chosen = group.families.filter((f) => familyIds.includes(f.id));
  const [name, setName] = useState(chosen[0]?.name ?? "");
  const [definition, setDefinition] = useState(chosen.map((f) => f.definition).join(" "));
  const { error, run } = useAction(onDone);

  async function merge() {
    const ok = await run(() => api.mergeCodeFamilies({ family_ids: familyIds, name, definition }));
    if (ok) onClose();
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>Merge {chosen.length} families</h3>
        <p className="meta">{chosen.map((f) => f.name).join(" + ")}</p>
        <div className="organize-form">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Merged family name" />
          <textarea rows={4} value={definition} onChange={(e) => setDefinition(e.target.value)} />
        </div>
        <button className="primary" onClick={merge} disabled={!name.trim() || !definition.trim()}>
          Merge
        </button>
        <button className="secondary" onClick={onClose} style={{ marginLeft: "0.3rem" }}>
          Cancel
        </button>
        {error && <p className="error-text">{error}</p>}
      </div>
    </div>
  );
}

function FocusedRQCard({
  group,
  allRqs,
  onChanged,
}: {
  group: OrganizeGroup;
  allRqs: ResearchQuestion[];
  onChanged: (notice?: string) => void;
}) {
  const [selected, setSelected] = useState<number[]>([]);
  const [merging, setMerging] = useState(false);
  const unassigned = group.entries.filter((e) => e.family_ids.length === 0);
  const liveSelected = selected.filter((id) => group.families.some((f) => f.id === id));
  const { cap } = group;

  return (
    <div className="memo-section rq-card">
      <div className="rq-card-header">
        <h2>{group.rq.name}</h2>
        <MergeIntoSelect rq={group.rq} targets={allRqs} onChanged={() => onChanged()} />
      </div>
      {group.rq.description && <p className="meta">{group.rq.description}</p>}
      <MergedSources group={group} onChanged={onChanged} />
      <p className="meta">
        {group.entry_count} filed entr{group.entry_count === 1 ? "y" : "ies"}, {unassigned.length} in no family ·{" "}
        {cap.live_count} of {cap.max_per_rq} families
      </p>
      {cap.at_cap && (
        <p className="warning-text">
          At the {cap.max_per_rq}-family cap. Merge or dismiss a family to make room.
        </p>
      )}
      {!cap.at_cap && cap.near_cap && <p className="warning-text">Near the {cap.max_per_rq}-family cap.</p>}

      {group.families.length === 0 && <p className="meta">No code families yet.</p>}
      {group.families.map((f) => (
        <CodeFamilyCard
          key={f.id}
          family={f}
          onChanged={() => onChanged()}
          addableEntries={unassigned}
          selected={liveSelected.includes(f.id)}
          onSelect={() => setSelected(toggleId(liveSelected, f.id))}
        />
      ))}

      <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.6rem", flexWrap: "wrap" }}>
        <NewFamilyForm group={group} onDone={() => onChanged()} />
        {group.families.length > 1 && (
          <button className="secondary" onClick={() => setMerging(true)} disabled={liveSelected.length < 2}>
            Merge selected{liveSelected.length > 0 ? ` (${liveSelected.length})` : ""}
          </button>
        )}
      </div>
      {merging && (
        <MergeFamiliesModal
          group={group}
          familyIds={liveSelected}
          onClose={() => setMerging(false)}
          onDone={() => {
            setSelected([]);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function OtherRQRow({
  group,
  allRqs,
  onChanged,
}: {
  group: OrganizeGroup;
  allRqs: ResearchQuestion[];
  onChanged: (notice?: string) => void;
}) {
  const { error, run } = useAction(() => onChanged());
  return (
    <div className="memo-entry">
      <div className="rq-card-header">
        <strong>{group.rq.name}</strong>
        <span className="meta">
          {group.entry_count} entr{group.entry_count === 1 ? "y" : "ies"} · {group.cap.live_count} families
        </span>
        <button className="secondary" onClick={() => run(() => api.setResearchQuestionFocus(group.rq.id, true))}>
          Focus
        </button>
        <MergeIntoSelect rq={group.rq} targets={allRqs} onChanged={() => onChanged()} />
      </div>
      <MergedSources group={group} onChanged={onChanged} />
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

function CodebookFooter() {
  const [prep, setPrep] = useState<CodebookPrep | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function prepare() {
    setBusy(true);
    setError(null);
    try {
      const result = await api.getCodebookPrep();
      setPrep(result);
      downloadJson(result, `codebook-prep-${localDate()}.json`);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const codes = prep?.research_questions.reduce((n, rq) => n + rq.codes.length, 0) ?? 0;
  const entries =
    prep?.research_questions.reduce((n, rq) => n + rq.codes.reduce((m, c) => m + c.entries.length, 0), 0) ?? 0;

  return (
    <div className="memo-section organize-footer">
      <h2>Codebook</h2>
      <p className="meta">
        Exports the starred code families of the focused research questions, with each entry and its quoted evidence,
        as a JSON file.
      </p>
      <button className="primary" onClick={prepare} disabled={busy}>
        {busy ? "Preparing…" : "Prepare codebook"}
      </button>
      {error && <p className="error-text">{error}</p>}
      {prep && (
        <div style={{ marginTop: "0.6rem" }}>
          <p className="meta">
            Downloaded {prep.research_questions.length} research question
            {prep.research_questions.length === 1 ? "" : "s"}, {codes} code{codes === 1 ? "" : "s"} and {entries}{" "}
            entr{entries === 1 ? "y" : "ies"}.
          </p>
          {prep.warnings.length > 0 && (
            <ul className="warning-text">
              {prep.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default function OrganizePage() {
  const [data, setData] = useState<OrganizeResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .getOrganize()
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch((e) => setLoadError(errorText(e)));
  }, []);

  useEffect(load, [load]);

  const changed = (message?: string) => {
    setNotice(message ?? null);
    load();
  };
  const { error: focusError, run: runFocus } = useAction(load);

  if (!data) return <p style={{ padding: "2rem" }}>{loadError ? <span className="error-text">{loadError}</span> : "Loading…"}</p>;

  const all = [...data.focused, ...data.other].sort(
    (a, b) => a.rq.display_order - b.rq.display_order || a.rq.id - b.rq.id
  );
  const allRqs = all.map((g) => g.rq);
  const [low, high] = data.focus_range;
  const inRange = data.focus_count >= low && data.focus_count <= high;

  return (
    <div className="organize-page">
      <h1>Organize</h1>
      {loadError && <p className="error-text">{loadError}</p>}
      {notice && (
        <p className="notice">
          {notice}{" "}
          <button className="link-button" onClick={() => setNotice(null)}>
            dismiss
          </button>
        </p>
      )}

      <div className="focus-bar">
        {all.map((g) => (
          <button
            key={g.rq.id}
            className={"chip" + (g.rq.focused ? " on" : "")}
            onClick={() => runFocus(() => api.setResearchQuestionFocus(g.rq.id, !g.rq.focused))}
            title={g.rq.focused ? "Unfocus" : "Focus"}
          >
            {g.rq.name}
          </button>
        ))}
        <span className={"focus-count" + (inRange ? "" : " off-range")}>
          {data.focus_count} of {low}-{high} focused
        </span>
        {focusError && <p className="error-text">{focusError}</p>}
      </div>

      {data.focused.length === 0 && (
        <p className="meta">Click research questions above to focus the {low}-{high} you'll build the codebook from.</p>
      )}
      {data.focused.map((g) => (
        <FocusedRQCard key={g.rq.id} group={g} allRqs={allRqs} onChanged={changed} />
      ))}

      {data.other.length > 0 && (
        <details className="memo-section">
          <summary>
            <h2 style={{ display: "inline" }}>Other RQs ({data.other.length})</h2>
          </summary>
          {data.other.map((g) => (
            <OtherRQRow key={g.rq.id} group={g} allRqs={allRqs} onChanged={changed} />
          ))}
        </details>
      )}

      <CodebookFooter />
    </div>
  );
}
