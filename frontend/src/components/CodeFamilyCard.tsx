import { useState } from "react";
import { api, errorText } from "../lib/api";
import type { CodeFamily, CodeFamilyMergeSuggestion, CodeFamilyState } from "../lib/types";

export interface PickableEntry {
  connection_id: number;
  insight_text: string;
  conversation_title?: string | null;
}

export function EntryPicker({
  entries,
  selected,
  onToggle,
}: {
  entries: PickableEntry[];
  selected: number[];
  onToggle: (connectionId: number) => void;
}) {
  if (entries.length === 0) return <p className="meta">No entries to pick from.</p>;
  return (
    <div className="entry-picker">
      {entries.map((e) => (
        <label key={e.connection_id}>
          <input
            type="checkbox"
            checked={selected.includes(e.connection_id)}
            onChange={() => onToggle(e.connection_id)}
          />{" "}
          {e.insight_text}
          {e.conversation_title && <span className="meta"> — {e.conversation_title}</span>}
        </label>
      ))}
    </div>
  );
}

function toggleId(ids: number[], id: number) {
  return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
}

export function CodeFamilyCard({
  family,
  onChanged,
  addableEntries,
  selected,
  onSelect,
  active,
  onToggleActive,
}: {
  family: CodeFamily;
  onChanged: () => void;
  /** When given, the card shows removable member chips and an "Add entries" picker. */
  addableEntries?: PickableEntry[];
  selected?: boolean;
  onSelect?: () => void;
  /** When given, the family name becomes a toggle (e.g. to highlight its members elsewhere). */
  active?: boolean;
  onToggleActive?: () => void;
}) {
  const editableMembers = addableEntries !== undefined;
  const [expanded, setExpanded] = useState(editableMembers);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(family.name);
  const [definition, setDefinition] = useState(family.definition);
  const [adding, setAdding] = useState(false);
  const [toAdd, setToAdd] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);
  const live = family.status === "suggested" || family.status === "accepted";

  async function run(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      onChanged();
    } catch (e) {
      setError(errorText(e));
    }
  }

  const save = () =>
    run(async () => {
      await api.updateCodeFamily(family.id, { name, definition });
      setEditing(false);
    });
  const accept = () => run(() => api.acceptCodeFamily(family.id));
  const dismiss = () => run(() => api.dismissCodeFamily(family.id));
  const toggleStar = () => run(() => api.starCodeFamily(family.id, !family.starred));
  const memberIds = family.members.map((m) => m.connection_id);
  const removeMember = (connectionId: number) =>
    run(() => api.setCodeFamilyMembers(family.id, memberIds.filter((id) => id !== connectionId)));
  const addMembers = () =>
    run(async () => {
      await api.setCodeFamilyMembers(family.id, [...memberIds, ...toAdd.filter((id) => !memberIds.includes(id))]);
      setToAdd([]);
      setAdding(false);
    });

  return (
    <div className={"memo-entry" + (active ? " family-active" : "")}>
      <div style={{ display: "flex", gap: "0.4rem", alignItems: "flex-start" }}>
        {onSelect && (
          <input type="checkbox" checked={!!selected} onChange={onSelect} aria-label={`Select ${family.name}`} />
        )}
        {live && (
          <button
            className={"star-toggle" + (family.starred ? " on" : "")}
            onClick={toggleStar}
            title={family.starred ? "Unstar" : family.status === "suggested" ? "Star (also accepts it)" : "Star for the codebook"}
          >
            {family.starred ? "★" : "☆"}
          </button>
        )}
        <div style={{ flex: 1 }}>
          {editing ? (
            <>
              <input style={{ width: "100%" }} value={name} onChange={(e) => setName(e.target.value)} />
              <textarea
                rows={2}
                style={{ width: "100%", marginTop: "0.3rem" }}
                value={definition}
                onChange={(e) => setDefinition(e.target.value)}
              />
              <button className="primary" onClick={save}>
                Save
              </button>
              <button className="secondary" onClick={() => setEditing(false)} style={{ marginLeft: "0.3rem" }}>
                Cancel
              </button>
            </>
          ) : (
            <>
              {onToggleActive ? (
                <button
                  className="family-name-toggle"
                  onClick={onToggleActive}
                  aria-pressed={!!active}
                  title={active ? "Stop highlighting its entries" : "Highlight its entries"}
                >
                  {family.name}
                </button>
              ) : (
                <strong>{family.name}</strong>
              )}
              <p style={{ margin: 0 }}>{family.definition}</p>
            </>
          )}
        </div>
      </div>
      <div className="meta">
        <button style={{ fontSize: "0.8rem" }} onClick={() => setExpanded(!expanded)}>
          {family.members.length} member{family.members.length === 1 ? "" : "s"} {expanded ? "▲" : "▼"}
        </button>
      </div>
      {expanded &&
        (editableMembers ? (
          <div style={{ margin: "0.3rem 0" }}>
            {family.members.map((m) => (
              <span key={m.connection_id} className="member-chip">
                {m.insight_text}
                <button onClick={() => removeMember(m.connection_id)} title="Remove from this family">
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : (
          <ul style={{ margin: "0.3rem 0" }}>
            {family.members.map((m) => (
              <li key={m.connection_id} style={{ fontSize: "0.85rem" }}>
                {m.insight_text}
              </li>
            ))}
          </ul>
        ))}
      {editableMembers && live && adding && (
        <div style={{ margin: "0.3rem 0" }}>
          <EntryPicker
            entries={addableEntries}
            selected={toAdd}
            onToggle={(id) => setToAdd(toggleId(toAdd, id))}
          />
          <button className="primary" onClick={addMembers} disabled={toAdd.length === 0}>
            Add {toAdd.length || ""}
          </button>
          <button className="secondary" onClick={() => setAdding(false)} style={{ marginLeft: "0.3rem" }}>
            Cancel
          </button>
        </div>
      )}
      {!editing && (
        <div style={{ marginTop: "0.3rem" }}>
          <button
            className="secondary"
            onClick={() => {
              setName(family.name);
              setDefinition(family.definition);
              setEditing(true);
            }}
          >
            Rename/redefine
          </button>
          {editableMembers && live && !adding && (
            <button className="secondary" onClick={() => setAdding(true)} style={{ marginLeft: "0.3rem" }}>
              Add entries
            </button>
          )}
          {family.status === "suggested" && (
            <button className="primary" onClick={accept} style={{ marginLeft: "0.3rem" }}>
              Accept
            </button>
          )}
          {family.status !== "dismissed" && (
            <button className="secondary" onClick={dismiss} style={{ marginLeft: "0.3rem" }}>
              Dismiss
            </button>
          )}
          {family.status !== "suggested" && (
            <span className="meta" style={{ fontStyle: "italic", marginLeft: "0.5rem" }}>
              {family.status}
            </span>
          )}
        </div>
      )}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

export function MergeSuggestionCard({
  merge,
  families,
  onChanged,
}: {
  merge: CodeFamilyMergeSuggestion;
  families: CodeFamily[];
  onChanged: (state?: CodeFamilyState) => void;
}) {
  const names = merge.family_ids.map((id) => families.find((f) => f.id === id)?.name ?? `#${id}`);

  async function accept() {
    onChanged(await api.acceptCodeFamilyMerge(merge.id));
  }

  async function dismiss() {
    await api.dismissCodeFamilyMerge(merge.id);
    onChanged();
  }

  return (
    <div className="memo-entry" style={{ borderLeft: "3px solid #e0b84a", paddingLeft: "0.6rem" }}>
      <div className="meta">Suggested merge: {names.join(" + ")}</div>
      <strong>{merge.proposed_name}</strong>
      <p style={{ margin: 0 }}>{merge.proposed_definition}</p>
      {merge.rationale && (
        <p className="meta" style={{ margin: "0.2rem 0" }}>
          {merge.rationale}
        </p>
      )}
      <button className="primary" onClick={accept}>
        Merge
      </button>
      <button className="secondary" onClick={dismiss} style={{ marginLeft: "0.3rem" }}>
        Keep separate
      </button>
    </div>
  );
}
