import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { useOnline, useStoredState } from "./hooks";
import { DEFAULT_ZONE, zoneLabel, zoneName, type Zone } from "./time";
import type { SessionInfo } from "./types";
import { Fleet } from "./components/Fleet";
import { InstrumentPage } from "./components/InstrumentPage";
import { Login } from "./components/Login";

function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const [session, setSession] = useState<SessionInfo | null | undefined>(undefined);
  const [zone, setZone] = useStoredState<Zone>("labmon.zone", "local");
  const online = useOnline();
  const route = useHashRoute();

  useEffect(() => {
    api
      .get<SessionInfo>("/api/v1/session")
      .then(setSession)
      .catch((e) => setSession(e instanceof ApiError && e.status === 401 ? null : null));
  }, []);

  if (session === undefined) return <p className="muted pad">Loading…</p>;
  if (session === null) return <Login onSignedIn={setSession} />;

  const displayTz = session.display_timezone || DEFAULT_ZONE;
  const tz = zoneName(zone, displayTz);
  const tzLabel = zoneLabel(zone, displayTz);
  const m = /^#\/instrument\/([^/]+)$/.exec(route);

  const signOut = async () => {
    try {
      await api.del("/api/v1/session");
    } finally {
      setSession(null);
    }
  };

  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <nav className="top">
        <a href="#/" className="brand">
          Lab Instrument Monitor
        </a>
        <span className="badge sim" title="This installation only has simulated instruments">
          Simulator-only release
        </span>
        <span className="spacer" />
        <span role="group" aria-label="Time zone" className="zone">
          <button type="button" className={zone === "local" ? "chip active" : "chip"} aria-pressed={zone === "local"} onClick={() => setZone("local")}>
            {zoneLabel("local", displayTz)}
          </button>
          <button type="button" className={zone === "UTC" ? "chip active" : "chip"} aria-pressed={zone === "UTC"} onClick={() => setZone("UTC")}>
            UTC
          </button>
        </span>
        <span className="sub">{session.display_name}</span>
        <button type="button" onClick={signOut}>
          Sign out
        </button>
      </nav>
      <div id="content">
        {m ? (
          <InstrumentPage key={m[1]} id={decodeURIComponent(m[1]!)} tz={tz} tzLabel={tzLabel} online={online} />
        ) : (
          <Fleet tz={tz} tzLabel={tzLabel} online={online} />
        )}
      </div>
    </>
  );
}
