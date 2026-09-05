/* The connect screen: the server prints its API token on startup; paste it
   here. Verified with a live probe before entering the console. */

import { useState } from "react";

import { api, setAuthToken } from "../api";

export function Login({ onConnected }: { onConnected: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // An empty token is a valid connect attempt: a server started with
  // --no-auth accepts it, and the probe rejects it everywhere else.
  const connect = async () => {
    setBusy(true);
    setError(null);
    setAuthToken(token.trim());
    try {
      await api.map(); // probe: proves the token before entering
      onConnected();
    } catch (err) {
      setAuthToken(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div className="logo-plate" aria-hidden="true" />
          <span className="wordmark">NODAL</span>
        </div>
        <p className="row-sub" style={{ margin: "10px 0" }}>
          The API server printed a token on startup. Paste it to connect — or leave it blank if
          the server runs with --no-auth.
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            style={{ flex: 1 }}
            type="password"
            placeholder="API token"
            value={token}
            autoFocus
            onChange={(event) => setToken(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !busy) void connect();
            }}
          />
          <button className="primary" disabled={busy} onClick={() => void connect()}>
            CONNECT
          </button>
        </div>
        {error && (
          <div className="reject-code" style={{ marginTop: 8 }}>
            {error}
          </div>
        )}
      </div>
    </div>
  );
}
