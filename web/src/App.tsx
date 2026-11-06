import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { useOnline, useStoredState } from "./hooks";
import { DEFAULT_ZONE, zoneLabel, zoneName, type Zone } from "./time";
import type { SessionInfo } from "./types";
import { Dashboard } from "./components/Dashboard";
import { InstrumentPage } from "./components/InstrumentPage";
import { Login } from "./components/Login";

type ThemePref = "system" | "light" | "dark";

function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

/** Applies the theme preference and returns the effective theme (charts re-render on change). */
function useTheme(pref: ThemePref): string {
  const [system, setSystem] = useState(() => (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSystem(mq.matches ? "dark" : "light");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  useEffect(() => {
    if (pref === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = pref;
  }, [pref]);
  return pref === "system" ? system : pref;
}

export function App() {
  const [session, setSession] = useState<SessionInfo | null | undefined>(undefined);
  const [zone, setZone] = useStoredState<Zone>("labmon.zone", "local");
  const [themePref, setThemePref] = useStoredState<ThemePref>("labmon.theme", "system");
  const theme = useTheme(themePref);
  const online = useOnline();
  const route = useHashRoute();

  useEffect(() => {
    api
      .get<SessionInfo>("/api/v1/session")
      .then(setSession)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status !== 401) console.warn("session check failed", e.status);
        setSession(null);
      });
  }, []);

  if (session === undefined) return <p className="muted pad">Loading…</p>;
  if (session === null) return <Login onSignedIn={setSession} />;

  const displayTz = session.display_timezone || DEFAULT_ZONE;
  const tz = zoneName(zone, displayTz);
  const tzLabel = zoneLabel(zone, displayTz);
  const m = /^#\/instrument\/([^/]+)$/.exec(route);
  const nextTheme: Record<ThemePref, ThemePref> = { system: "light", light: "dark", dark: "system" };

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
      <nav className="topbar" aria-label="Main">
        <a href="#/" className="brand">
          <span className="logo" aria-hidden="true">
            ∿
          </span>
          Lab Instrument Monitor
        </a>
        <span className="badge sim" title="This installation only has simulated instruments">
          Simulator-only release
        </span>
        <div className="topnav">
          <a href="#/" aria-current={m ? undefined : "page"}>
            Fleet
          </a>
        </div>
        <span className="spacer" />
        <div className="segmented" role="group" aria-label="Time zone">
          <button type="button" aria-pressed={zone === "local"} onClick={() => setZone("local")}>
            {zoneLabel("local", displayTz)}
          </button>
          <button type="button" aria-pressed={zone === "UTC"} onClick={() => setZone("UTC")}>
            UTC
          </button>
        </div>
        <button type="button" onClick={() => setThemePref(nextTheme[themePref])} aria-label={`Theme: ${themePref}. Switch to ${nextTheme[themePref]}`} title={`Theme: ${themePref}`}>
          Theme: {themePref === "system" ? "Auto" : themePref === "light" ? "Light" : "Dark"}
        </button>
        <span className="user">{session.display_name}</span>
        <button type="button" onClick={signOut}>
          Sign out
        </button>
      </nav>
      <div id="content">
        {m ? (
          <InstrumentPage key={m[1]} id={decodeURIComponent(m[1]!)} tz={tz} tzLabel={tzLabel} online={online} theme={theme} labs={session.labs} />
        ) : (
          <Dashboard tz={tz} tzLabel={tzLabel} online={online} />
        )}
      </div>
    </>
  );
}
