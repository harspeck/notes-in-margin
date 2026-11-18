import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorText } from "../lib/api";
import { plural } from "../lib/format";
import { useSession } from "../lib/session";
import type { Organization, Project, Workspace } from "../lib/types";

/** Picks the projects to work in. The backend calls a saved set of projects a workspace. */
export default function WorkspacePage() {
  const { me, refresh } = useSession();
  const navigate = useNavigate();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [organizations, setOrganizations] = useState<Organization[] | null>(null);
  const [organizationId, setOrganizationId] = useState<number | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listWorkspaces()
      .then((rows) => setWorkspaces(rows.filter((w) => !w.archived)))
      .catch((e) => setError(errorText(e)));
    api
      .listOrganizations()
      .then((orgs) => {
        setOrganizations(orgs);
        setOrganizationId((current) => current ?? orgs[0]?.id ?? null);
      })
      .catch((e) => setError(errorText(e)));
  }, []);

  useEffect(() => {
    if (organizationId === null) return;
    let stale = false;
    api
      .listProjects(organizationId)
      .then((rows) => {
        if (!stale) setProjects(rows);
      })
      .catch((e) => setError(errorText(e)));
    return () => {
      stale = true;
    };
  }, [organizationId]);

  function chooseOrganization(id: number) {
    setOrganizationId(id);
    setProjects(null);
    setChosen(new Set());
  }

  function toggle(id: number) {
    setChosen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function finish(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await refresh();
      navigate("/");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function create(e: FormEvent) {
    e.preventDefault();
    if (organizationId === null || chosen.size === 0) return;
    const fallback = (projects ?? [])
      .filter((p) => chosen.has(p.id))
      .map((p) => p.name ?? "Untitled project")
      .slice(0, 2)
      .join(" and ");
    finish(() =>
      api.createWorkspace({ name: name.trim() || fallback, organization_id: organizationId, project_ids: [...chosen] })
    );
  }

  const orgName = (id: number) => organizations?.find((o) => o.id === id)?.name ?? "An organization you have projects in";

  return (
    <div className="wrap narrow">
      <h1 className="h1">choose your projects</h1>
      <p className="lede">
        Your desk shows the conversations from the projects you pick here. You can change this any time.
      </p>
      {error && <p className="err" style={{ marginTop: 24 }}>{error}</p>}

      {workspaces.length > 0 && (
        <section className="qgroup">
          <h2>Sets you've saved</h2>
          <ul className="convlist">
            {workspaces.map((w) => (
              <li key={w.id} className="convrow" style={{ cursor: "default" }}>
                <b>{w.name}</b>
                {w.id === me.active_workspace?.id ? (
                  <span className="tag kept">Open now</span>
                ) : (
                  <button className="btn outline small" disabled={busy} onClick={() => finish(() => api.activateWorkspace(w.id))}>
                    Open
                  </button>
                )}
                <span className="hint">
                  {plural(w.project_ids.length, "project")} in {orgName(w.organization_id)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="qgroup">
        <h2>{workspaces.length > 0 ? "Pick a new set" : "Pick projects"}</h2>
        {organizations === null && !error && <p className="hint">Loading your projects…</p>}
        {organizations?.length === 0 && (
          <p className="hint">Your Cortico account doesn't have a role on any project yet.</p>
        )}
        {organizations && organizations.length > 0 && (
          <form className="form" style={{ maxWidth: "none" }} onSubmit={create}>
            {organizations.length > 1 && (
              <label style={{ maxWidth: 420 }}>
                Organization
                <select className="inp" value={organizationId ?? ""} onChange={(e) => chooseOrganization(Number(e.target.value))}>
                  {organizations.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.name ?? "An organization you have projects in"} ({plural(o.project_count, "project")})
                    </option>
                  ))}
                </select>
              </label>
            )}
            {projects === null && <p className="hint">Loading projects…</p>}
            <ul className="convlist">
              {(projects ?? []).map((p) => (
                <li key={p.id}>
                  <label className="convrow" style={{ gridTemplateColumns: "auto minmax(0, 1fr)", fontSize: 15, cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      style={{ width: 18, height: 18, accentColor: "var(--black)" }}
                      checked={chosen.has(p.id)}
                      onChange={() => toggle(p.id)}
                    />
                    <b>{p.name ?? "Untitled project"}</b>
                    {(p.description || p.conversation_count !== undefined) && (
                      <span className="hint" style={{ gridColumn: 2, fontWeight: 400 }}>
                        {[
                          p.conversation_count !== undefined ? plural(p.conversation_count, "conversation") : "",
                          p.description ?? "",
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    )}
                  </label>
                </li>
              ))}
            </ul>
            <label style={{ maxWidth: 420 }}>
              Name this set (optional)
              <input className="inp" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
            </label>
            <div>
              <button className="btn" disabled={busy || chosen.size === 0}>
                {chosen.size === 0 ? "Pick at least one project" : `Work in ${plural(chosen.size, "project")}`}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
