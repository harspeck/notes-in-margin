import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { CodeFamilyCard, EntryPicker } from "../components/CodeFamilyCard";
import type {
  CodebookPrep,
  CodeFamily,
  OrganizeEntry,
  OrganizeGroup,
  OrganizeResponse,
  ResearchQuestion,
} from "../lib/types";

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

function CodebookPanel() {
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
    <div className="organize-codebook">
      <button
        className="primary"
        onClick={prepare}
        disabled={busy}
        title="Downloads the starred code families of the focused research questions, with each entry and its quoted evidence, as JSON."
      >
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

function RQSidebarRow({
  group,
  selected,
  onSelect,
  onToggleFocus,
}: {
  group: OrganizeGroup;
  selected: boolean;
  onSelect: () => void;
  onToggleFocus: () => void;
}) {
  const { rq } = group;
  return (
    <li className={"rq-row" + (selected ? " selected" : "")}>
      <button
        className={"rq-focus-toggle" + (rq.focused ? " on" : "")}
        onClick={onToggleFocus}
        aria-pressed={rq.focused}
        aria-label={rq.focused ? `Unfocus ${rq.name}` : `Focus ${rq.name}`}
        title={rq.focused ? "Unfocus" : "Focus"}
      >
        {rq.focused ? "●" : "○"}
      </button>
      <button className="rq-row-name" onClick={onSelect} title={rq.name} aria-current={selected ? "true" : undefined}>
        {rq.name}
      </button>
      <span className="rq-row-count" title={`${group.entry_count} filed entries`}>
        {group.entry_count}
      </span>
    </li>
  );
}

function RQSidebar({
  data,
  selectedId,
  onSelect,
  onChanged,
}: {
  data: OrganizeResponse;
  selectedId: number | undefined;
  onSelect: (id: number) => void;
  onChanged: () => void;
}) {
  const { error, run } = useAction(onChanged);
  const [low, high] = data.focus_range;
  const inRange = data.focus_count >= low && data.focus_count <= high;
  const row = (g: OrganizeGroup) => (
    <RQSidebarRow
      key={g.rq.id}
      group={g}
      selected={g.rq.id === selectedId}
      onSelect={() => onSelect(g.rq.id)}
      onToggleFocus={() => run(() => api.setResearchQuestionFocus(g.rq.id, !g.rq.focused))}
    />
  );

  return (
    <aside className="organize-col organize-sidebar">
      <div className="organize-sidebar-header">
        <h2>RQs</h2>
        <span className={"focus-count" + (inRange ? "" : " off-range")}>
          {data.focus_count} of {low}-{high} focused
        </span>
      </div>
      {error && <p className="error-text">{error}</p>}
      {data.focused.length === 0 && (
        <p className="meta">Focus the {low}-{high} research questions you'll build the codebook from.</p>
      )}
      <ul className="rq-list">{data.focused.map(row)}</ul>
      {data.other.length > 0 && (
        <>
          <div className="rq-divider">Other</div>
          <ul className="rq-list">{data.other.map(row)}</ul>
        </>
      )}
      <CodebookPanel />
    </aside>
  );
}

function AddToFamilySelect({ entry, families, onChanged }: { entry: OrganizeEntry; families: CodeFamily[]; onChanged: () => void }) {
  const { error, run } = useAction(onChanged);
  const options = families.filter((f) => !entry.family_ids.includes(f.id));
  if (options.length === 0) return null;

  function add(familyId: number) {
    const family = options.find((f) => f.id === familyId);
    if (!family) return;
    run(() =>
      api.setCodeFamilyMembers(family.id, [...family.members.map((m) => m.connection_id), entry.connection_id])
    );
  }

  return (
    <span>
      <select className="add-to-family" value="" onChange={(e) => add(Number(e.target.value))}>
        <option value="">+ add to family</option>
        {options.map((f) => (
          <option key={f.id} value={f.id}>
            {f.name}
          </option>
        ))}
      </select>
      {error && <span className="error-text"> {error}</span>}
    </span>
  );
}

function InsightCard({
  entry,
  families,
  activeFamilyId,
  onToggleFamily,
  onChanged,
}: {
  entry: OrganizeEntry;
  families: CodeFamily[];
  activeFamilyId: number | null;
  onToggleFamily: (id: number) => void;
  onChanged: () => void;
}) {
  const memberOf = families.filter((f) => entry.family_ids.includes(f.id));
  const active = activeFamilyId !== null && entry.family_ids.includes(activeFamilyId);
  return (
    <div className={"insight" + (active ? " family-active" : "")}>
      <p className="insight-text">{entry.insight_text}</p>
      {entry.evidence.map((e, i) => (
        <blockquote key={i} className="evidence-quote">
          {e.starred && <span className="evidence-star" title="Starred highlight">★ </span>}
          {entry.conversation_id != null ? (
            <Link to={`/?conversation=${entry.conversation_id}&snippet=${e.snippet_id}`}>
              "{e.text || `snippet ${e.snippet_id}`}"
            </Link>
          ) : (
            `"${e.text || `snippet ${e.snippet_id}`}"`
          )}{" "}
          <span className={"badge " + (e.source === "user" ? "yours" : "ai")}>
            {e.source === "user" ? "yours" : "AI-suggested"}
          </span>
        </blockquote>
      ))}
      <div className="meta">
        {entry.conversation_id != null ? (
          <Link to={`/?conversation=${entry.conversation_id}`}>{entry.conversation_title ?? `Conversation ${entry.conversation_id}`}</Link>
        ) : (
          "Unknown conversation"
        )}
        {entry.evidence.length === 0 && " · no quoted evidence"}
      </div>
      <div className="insight-families">
        {memberOf.map((f) => (
          <button
            key={f.id}
            className={"family-tag" + (f.id === activeFamilyId ? " on" : "")}
            onClick={() => onToggleFamily(f.id)}
            aria-pressed={f.id === activeFamilyId}
            title="Highlight this family's entries"
          >
            {f.name}
          </button>
        ))}
        <AddToFamilySelect entry={entry} families={families} onChanged={onChanged} />
      </div>
    </div>
  );
}

function StarredNotCited({ group }: { group: OrganizeGroup }) {
  if (group.starred_unassigned.length === 0) return null;
  return (
    <section className="starred-not-cited">
      <h3>Starred, not yet cited</h3>
      <p className="meta">Starred highlights from these conversations that no filed insight cites yet.</p>
      {group.starred_unassigned.map((conv) => (
        <div key={conv.conversation_id} className="starred-conversation">
          <Link to={`/?conversation=${conv.conversation_id}`}>{conv.conversation_title ?? `Conversation ${conv.conversation_id}`}</Link>
          {conv.highlights.map((h) => (
            <div key={h.highlight_id} className="starred-highlight">
              <div>★ {h.notes || <span className="meta">(no margin note)</span>}</div>
              {h.snippets.map((s, i) => (
                <blockquote key={i} className="evidence-quote">
                  <Link to={`/?conversation=${conv.conversation_id}&snippet=${s.snippet_id}`}>
                    "{s.text || `snippet ${s.snippet_id}`}"
                  </Link>
                </blockquote>
              ))}
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

function RQDetail({
  group,
  allRqs,
  activeFamilyId,
  onToggleFamily,
  onChanged,
}: {
  group: OrganizeGroup;
  allRqs: ResearchQuestion[];
  activeFamilyId: number | null;
  onToggleFamily: (id: number) => void;
  onChanged: (notice?: string) => void;
}) {
  const unassigned = group.entries.filter((e) => e.family_ids.length === 0);
  return (
    <>
      <div className="rq-detail-header">
        <h1>{group.rq.name}</h1>
        <MergeIntoSelect rq={group.rq} targets={allRqs} onChanged={() => onChanged()} />
      </div>
      {group.rq.description && <p className="meta">{group.rq.description}</p>}
      <MergedSources group={group} onChanged={onChanged} />
      <p className="meta">
        {group.entry_count} filed insight{group.entry_count === 1 ? "" : "s"}, {unassigned.length} in no family
      </p>
      {group.entries.length === 0 && <p className="meta">No insights filed under this research question yet.</p>}
      {group.entries.map((e) => (
        <InsightCard
          key={e.connection_id}
          entry={e}
          families={group.families}
          activeFamilyId={activeFamilyId}
          onToggleFamily={onToggleFamily}
          onChanged={() => onChanged()}
        />
      ))}
      <StarredNotCited group={group} />
    </>
  );
}

function FamiliesPanel({
  group,
  activeFamilyId,
  onToggleFamily,
  onChanged,
}: {
  group: OrganizeGroup;
  activeFamilyId: number | null;
  onToggleFamily: (id: number) => void;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<number[]>([]);
  const [merging, setMerging] = useState(false);
  const unassigned = group.entries.filter((e) => e.family_ids.length === 0);
  const liveSelected = selected.filter((id) => group.families.some((f) => f.id === id));
  const { cap } = group;

  return (
    <>
      <div className="organize-sidebar-header">
        <h2>Code families</h2>
        <span className={"focus-count" + (cap.at_cap ? " off-range" : "")}>
          {cap.live_count}/{cap.max_per_rq}
        </span>
      </div>
      {cap.at_cap && (
        <p className="warning-text">At the {cap.max_per_rq}-family cap. Merge or dismiss a family to make room.</p>
      )}
      {!cap.at_cap && cap.near_cap && <p className="warning-text">Near the {cap.max_per_rq}-family cap.</p>}
      {group.families.length === 0 && <p className="meta">No code families yet.</p>}
      {group.families.map((f) => (
        <CodeFamilyCard
          key={f.id}
          family={f}
          onChanged={onChanged}
          addableEntries={unassigned}
          selected={liveSelected.includes(f.id)}
          onSelect={() => setSelected(toggleId(liveSelected, f.id))}
          active={f.id === activeFamilyId}
          onToggleActive={() => onToggleFamily(f.id)}
        />
      ))}
      <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.6rem", flexWrap: "wrap" }}>
        <NewFamilyForm group={group} onDone={onChanged} />
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
    </>
  );
}

export default function OrganizePage() {
  const [data, setData] = useState<OrganizeResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeFamilyId, setActiveFamilyId] = useState<number | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

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

  if (!data) return <p style={{ padding: "2rem" }}>{loadError ? <span className="error-text">{loadError}</span> : "Loading…"}</p>;

  const groups = [...data.focused, ...data.other];
  const allRqs = [...groups]
    .sort((a, b) => a.rq.display_order - b.rq.display_order || a.rq.id - b.rq.id)
    .map((g) => g.rq);
  const requestedId = Number(searchParams.get("rq"));
  // A requested RQ that was merged away is no longer a group, so this falls back to the default.
  const selected = groups.find((g) => g.rq.id === requestedId) ?? groups[0];
  const activeFamily = selected?.families.some((f) => f.id === activeFamilyId) ? activeFamilyId : null;
  const toggleFamily = (id: number) => setActiveFamilyId(activeFamily === id ? null : id);
  const select = (id: number) => {
    setActiveFamilyId(null);
    setSearchParams({ rq: String(id) });
  };

  return (
    <div className="organize-page">
      <RQSidebar data={data} selectedId={selected?.rq.id} onSelect={select} onChanged={load} />
      <main className="organize-col organize-main">
        {loadError && <p className="error-text">{loadError}</p>}
        {notice && (
          <p className="notice">
            {notice}{" "}
            <button className="link-button" onClick={() => setNotice(null)}>
              dismiss
            </button>
          </p>
        )}
        {selected ? (
          <RQDetail
            key={selected.rq.id}
            group={selected}
            allRqs={allRqs}
            activeFamilyId={activeFamily}
            onToggleFamily={toggleFamily}
            onChanged={changed}
          />
        ) : (
          <p className="meta">No research questions yet.</p>
        )}
      </main>
      <aside className="organize-col organize-families">
        {selected && (
          <FamiliesPanel
            key={selected.rq.id}
            group={selected}
            activeFamilyId={activeFamily}
            onToggleFamily={toggleFamily}
            onChanged={() => changed()}
          />
        )}
      </aside>
    </div>
  );
}
