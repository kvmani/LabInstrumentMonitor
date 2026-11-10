# ML Server portal catalog: staging plan

**Status: plan, not yet staged.** The portal (`kvmani/ml_server`) and the deployment suite
(`kvmani/ml_server_deploy`) are separate projects with their own baselines and release process
(AGENTS.md). This page lists the change each one needs and the staging checks. Nothing here
changes those repositories. Each change goes to its owner as a separate, reviewed change.

## How the portal links services today

Read on 2026-11-10 from the default branches:

- The landing page renders a reviewed, data-driven list, `tool_catalog()` in
  `src/ml_server/catalog.py`. Each card has `id`, `name`, `summary`, `detail`, `category`, `tags`,
  `state`, `owner`, `href`, `icon` and `internal`. Each `href` comes from an environment variable
  with a loopback fallback, for example `os.getenv("UNIT_CONVERTER_URL", "http://127.0.0.1:5065")`.
- The suite generates those variables into `shared/config/ml-platform.env` from each manifest
  service's `public_url_env`. That file survives upgrades and rollbacks.
- The suite's `health_check.sh` asserts that `/api/catalog` advertises the intranet address and
  **no** loopback address. Suite release v1.4.0 failed exactly because every link pointed at
  127.0.0.1.

The monitor is **not** a suite service. It has its own installer, systemd units, PostgreSQL,
nginx/TLS and release cadence (ADR 0006), and possibly its own host. The portal only links to it.

## Proposed changes

**1 · Portal (`kvmani/ml_server`), one card**, shown only when its URL is configured, so it can be
disabled on its own without a portal release:

```python
monitor_url = os.getenv("LAB_INSTRUMENT_MONITOR_URL", "").strip()
...
*(
    [
        {
            "id": "lab-instrument-monitor",
            "name": "Lab Instrument Monitor",
            "summary": "Live status and history of lab instruments (read-only).",
            "detail": (
                "Fleet status, synchronized trends, run-specific XY analysis and durable "
                "exports. The first release monitors simulated instruments only."
            ),
            "category": "Laboratory",
            "tags": ["instruments", "monitoring", "furnace", "tensile", "dilatometer", "DSC"],
            "state": "active",
            "owner": "LabInstrumentMonitor",
            "href": monitor_url,
            "icon": "monitor-mark.svg",  # new icon, supplied with the portal change
            "internal": False,
        }
    ]
    if monitor_url
    else []
),
```

There is deliberately no loopback fallback. An unset variable hides the card instead of
publishing a dead link. The portal's tests should cover both cases (variable set → card with
exactly that `href`; unset → no card).

**2 · Suite (`kvmani/ml_server_deploy`), configuration only.** Add
`LAB_INSTRUMENT_MONITOR_URL=https://monitor.lab.example` to `shared/config/ml-platform.env`
through the suite's documented seeding, or as an operator edit recorded in its runbook. The
monitor must **not** be added as a manifest service: the suite must never install, start,
restart or roll back the monitor's units.

**3 · Monitor (this repository): nothing to change.** It is reached by a plain top-level link to
`/`, has its own session cookie on its own origin, and needs no portal credentials.

## Staging checks (record results in TASK_PROGRESS.md and the suite's release notes)

| Check | Pass when |
| --- | --- |
| Pinned baselines | Portal and suite commits or signed artifacts, and the monitor release version, are recorded before and after |
| Card present | `/api/catalog` lists `lab-instrument-monitor` with `href` = the configured HTTPS URL and no loopback address. The suite's `health_check.sh` passes |
| Card disabled | Removing the variable and restarting only the portal hides the card. The monitor's health is unchanged |
| Link works from a desk | From a client workstation (not the server), the card opens the monitor over trusted HTTPS with no certificate warning, and sign-in works |
| Independence: suite change | A suite upgrade and rollback restart no `labmon-*` unit (`systemctl show labmon-api -p ActiveEnterTimestamp` unchanged). Collectors keep uploading |
| Independence: monitor change | A monitor upgrade and rollback (`deploy/install.sh --activate`) restart no `ml-platform-*` unit. The portal stays up |
| No shared state | No monitor data or config lives in the suite's `shared/`, and none of the suite's lives in `/etc/labmon` or `/var/lib/labmon` |
| Browser requests | With the monitor open from the portal, the browser makes only same-origin requests to the monitor (the e2e suite already asserts this for the monitor alone) |
| Spool across a central change | While the portal and suite change, and during a monitor release change, collectors spool and then drain with no gap (`labmon-collector status`, history) |

## Rollback

Unset `LAB_INSTRUMENT_MONITOR_URL` and restart the portal. To remove the card entirely, revert the
portal change in the portal's own release. Neither touches the monitor or its data.
