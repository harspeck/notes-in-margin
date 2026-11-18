import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import DeskPage from "./pages/DeskPage";
import ReaderPage from "./pages/ReaderPage";
import ReviewPage from "./pages/ReviewPage";
import QuestionsPage from "./pages/QuestionsPage";
import MemoPage from "./pages/MemoPage";
import OrganizePage from "./pages/OrganizePage";
import SettingsPage from "./pages/SettingsPage";
import SignInPage from "./pages/SignInPage";
import WorkspacePage from "./pages/WorkspacePage";
import ActivityPage from "./pages/ActivityPage";
import { DESK_CHANGED_EVENT, SIGNED_OUT_EVENT, api, errorStatus, errorText } from "./lib/api";
import { SessionContext, isHosted } from "./lib/session";
import type { DeskLastConversation, Me } from "./lib/types";
import { ToastProvider } from "./lib/ui";

/** Old links were /?conversation=N&snippet=ID; the transcript now lives at /read/N. */
function Home() {
  const [params] = useSearchParams();
  const conversation = params.get("conversation");
  if (conversation) {
    const snippet = params.get("snippet");
    return <Navigate to={`/read/${conversation}${snippet ? `?snippet=${snippet}` : ""}`} replace />;
  }
  return <DeskPage />;
}

function Header({ me, onSignOut }: { me: Me | null; onSignOut: () => void }) {
  const location = useLocation();
  const [last, setLast] = useState<DeskLastConversation | null>(null);
  const inConversation = location.pathname.startsWith("/read/") || location.pathname.startsWith("/review/");
  const ready = me !== null && (!isHosted(me) || me.active_workspace !== null);
  const workspaceId = me?.active_workspace?.id ?? null;
  // The chip is the conversation opened last, which the server knows. It is re-read when the
  // reader records an open and when the set of projects changes.
  useEffect(() => {
    if (!ready) return;
    let stale = false;
    const sync = () =>
      api
        .getDesk()
        .then((desk) => {
          if (!stale) setLast(desk.last_conversation);
        })
        .catch(() => {});
    sync();
    window.addEventListener(DESK_CHANGED_EVENT, sync);
    return () => {
      stale = true;
      window.removeEventListener(DESK_CHANGED_EVENT, sync);
    };
  }, [ready, workspaceId]);
  return (
    <header className="app">
      <Link className="brand" to="/">
        Notes in the Margin
      </Link>
      {ready && (
        <nav className="appnav" aria-label="App">
          <NavLink to="/" end>
            Desk
          </NavLink>
          <NavLink to="/questions">Questions</NavLink>
          <NavLink to="/codebook">Codebook</NavLink>
          {last && (
            <>
              <span className="sep" aria-hidden="true" />
              <Link
                className="conv"
                to={`/read/${last.conversation_id}?resume=1`}
                aria-current={inConversation ? "page" : undefined}
              >
                <span className="pip read" aria-hidden="true" />
                <span className="t">{last.title}</span>
              </Link>
            </>
          )}
        </nav>
      )}
      <span className="grow" />
      {ready && (
        <>
          {me.active_workspace && (
            <NavLink className="applink" to="/projects" title="Change which projects you're working in">
              {me.active_workspace.name}
            </NavLink>
          )}
          <NavLink className="applink" to="/activity">
            Activity
          </NavLink>
          <NavLink className="applink" to="/memo">
            Memo
          </NavLink>
          <NavLink className="applink" to="/settings">
            Settings
          </NavLink>
        </>
      )}
      {me && isHosted(me) && (
        <button className="applink" onClick={onSignOut}>
          Sign out
        </button>
      )}
    </header>
  );
}

type Gate = { status: "loading" } | { status: "signedOut" } | { status: "error"; message: string } | { status: "ready"; me: Me };

export default function App() {
  const [gate, setGate] = useState<Gate>({ status: "loading" });

  const refresh = useCallback(
    () =>
      api
        .me()
        .then((me) => setGate({ status: "ready", me }))
        .catch((e) =>
          setGate(errorStatus(e) === 401 ? { status: "signedOut" } : { status: "error", message: errorText(e) })
        ),
    []
  );

  useEffect(() => {
    refresh();
    const signedOut = () => setGate({ status: "signedOut" });
    window.addEventListener(SIGNED_OUT_EVENT, signedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, signedOut);
  }, [refresh]);

  async function signOut() {
    await api.logout().catch(() => {});
    setGate({ status: "signedOut" });
  }

  const me = gate.status === "ready" ? gate.me : null;
  const needsProjects = me !== null && isHosted(me) && me.active_workspace === null;

  return (
    <ToastProvider>
      <Header me={me} onSignOut={signOut} />
      {gate.status === "loading" && <p className="loading">Loading…</p>}
      {gate.status === "error" && (
        <div className="wrap narrow">
          <p className="err">The app couldn't reach its server ({gate.message}).</p>
          <button className="btn outline" onClick={refresh}>
            Try again
          </button>
        </div>
      )}
      {gate.status === "signedOut" && <SignInPage onSignedIn={refresh} />}
      {me && (
        <SessionContext.Provider value={{ me, refresh }}>
          {needsProjects ? (
            <WorkspacePage />
          ) : (
            // Keyed by the set of projects, so every screen reloads its data when that changes.
            <Routes key={me.active_workspace?.id ?? "all"}>
              <Route path="/" element={<Home />} />
              <Route path="/read/:conversationId" element={<ReaderPage />} />
              <Route path="/review/:voiceMemoId" element={<ReviewPage />} />
              <Route path="/questions" element={<QuestionsPage />} />
              <Route path="/codebook" element={<OrganizePage />} />
              <Route path="/organize" element={<Navigate to="/codebook" replace />} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="/projects" element={<WorkspacePage />} />
              <Route path="/memo" element={<div className="legacy"><MemoPage /></div>} />
              <Route path="/settings" element={<div className="legacy"><SettingsPage /></div>} />
            </Routes>
          )}
        </SessionContext.Provider>
      )}
    </ToastProvider>
  );
}
