import { useState } from "react";
import { api, ApiError } from "../api";
import { usePolling } from "../hooks";
import type { PanelScale } from "./TimePanels";

export interface ViewConfig {
  preset?: "15m" | "1h" | "24h" | "7d" | "30d";
  start?: string;
  end?: string;
  scales?: Record<string, PanelScale>;
}

interface SavedView {
  id: number;
  name: string;
  scope: "personal" | "lab";
  owner: string | null;
  config: ViewConfig;
}

const TEMPLATE_ROLES = new Set(["custodian", "administrator"]);

/** Saved personal views and lab templates for one instrument (HIST-01). */
export function ViewMenu(props: {
  instrumentId: string;
  role: string;
  current: () => ViewConfig;
  apply: (c: ViewConfig) => void;
}) {
  const { instrumentId, role, current, apply } = props;
  const views = usePolling<{ items: SavedView[] }>(
    (signal) => api.get(`/api/v1/instruments/${encodeURIComponent(instrumentId)}/views`, signal),
    60000,
    `views:${instrumentId}`,
  );
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = views.data?.items ?? [];

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api.post(`/api/v1/instruments/${encodeURIComponent(instrumentId)}/views`, {
        name,
        scope: shared ? "lab" : "personal",
        config: current(),
      });
      setSaving(false);
      setName("");
      views.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "request failed");
    }
  };
  const remove = async (id: number) => {
    setError(null);
    try {
      await api.del(`/api/v1/views/${id}`);
      views.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "request failed");
    }
  };

  return (
    <span className="row" data-testid="views">
      <label>
        <span className="sr-only">Saved views</span>
        <select
          aria-label="Saved views"
          value=""
          onChange={(e) => {
            const v = items.find((x) => String(x.id) === e.target.value);
            if (v) apply(v.config);
          }}
        >
          <option value="">Views…</option>
          {items.some((v) => v.scope === "lab") && (
            <optgroup label="Lab templates">
              {items
                .filter((v) => v.scope === "lab")
                .map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ))}
            </optgroup>
          )}
          {items.some((v) => v.scope === "personal") && (
            <optgroup label="My views">
              {items
                .filter((v) => v.scope === "personal")
                .map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ))}
            </optgroup>
          )}
        </select>
      </label>
      <button type="button" onClick={() => setSaving(!saving)} aria-expanded={saving}>
        Save view…
      </button>
      {saving && (
        <form className="row" onSubmit={save} aria-label="Save view">
          <input
            required
            maxLength={80}
            placeholder="View name"
            aria-label="View name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {TEMPLATE_ROLES.has(role) && (
            <label>
              <input
                type="checkbox"
                checked={shared}
                onChange={(e) => setShared(e.target.checked)}
              />{" "}
              share with lab
            </label>
          )}
          <button type="submit" className="btn-primary">
            Save
          </button>
          {items
            .filter((v) => v.scope === "personal" || TEMPLATE_ROLES.has(role))
            .map((v) => (
              <button
                key={v.id}
                type="button"
                className="btn-ghost"
                onClick={() => remove(v.id)}
                aria-label={`Delete view ${v.name}`}
              >
                ✕ {v.name}
              </button>
            ))}
        </form>
      )}
      {error && (
        <span className="error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
