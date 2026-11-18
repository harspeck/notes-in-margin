import { useState } from "react";
import type { FormEvent } from "react";
import { api, errorStatus, errorText } from "../lib/api";

export default function SignInPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      onSignedIn();
    } catch (err) {
      const status = errorStatus(err);
      setError(
        status === 401 || status === 403 || status === 422
          ? "Cortico didn't accept that email and password."
          : `Sign-in didn't go through (${errorText(err)}).`
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="wrap narrow">
      <h1 className="h1">sign in</h1>
      <p className="lede">
        Use your Cortico account. Your password is sent to Cortico once to sign you in, and it isn't stored here.
      </p>
      <form className="form" style={{ marginTop: 32, maxWidth: 420 }} onSubmit={submit}>
        {error && <p className="err" role="alert">{error}</p>}
        <label>
          Email
          <input
            className="inp"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label>
          Password
          <input
            className="inp"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <div>
          <button className="btn" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </div>
      </form>
    </div>
  );
}
