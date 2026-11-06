import { useState } from "react";
import { api, ApiError } from "../api";
import type { SessionInfo } from "../types";

export function Login({ onSignedIn }: { onSignedIn: (s: SessionInfo) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.post<SessionInfo>("/api/v1/session", { username, password }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Cannot reach the monitor service");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form className="card" onSubmit={submit} aria-labelledby="login-title">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            ∿
          </span>
          <h1 id="login-title">Lab Instrument Monitor</h1>
        </div>
        <p className="muted">Read-only monitoring. Sign in with your lab account.</p>
        <label>
          Username
          <input
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
