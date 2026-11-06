# Lab Instrument Monitor
## Implementation roadmap

Use [SPECIFICATIONS.md](SPECIFICATIONS.md) as the product contract and [AGENTS.md](AGENTS.md) as the repository workflow. The first release is simulator-only. Hardware work starts instrument by instrument after the owner identifies a source and provides interface details. Advance each gate after attaching the listed evidence. “Pending” means the acceptance work has not been performed.

| Milestone | Deliverable | Gate | Status |
| --- | --- | --- | --- |
| M0 · Project foundation | Initialize the owner-created repository (proposed name LabInstrumentMonitor), confirm the license, add short README, dependency locks, ADRs for stack and ingest identity, initial threat/data-flow model, test commands and progress ledger. Confirm Ubuntu/Python/PostgreSQL baseline from reference environments. Define adapter and API fixtures and synthetic pilot dataset. | A clean checkout can launch PostgreSQL, run the API, build the local UI and run a smoke test. Document the offline dependency collection path and unresolved site inputs. No hardware or production environment needed. | Done (simulated) — MIT license and Ubuntu 24.04 / Python 3.12 / PostgreSQL 16 baseline confirmed by the owner; evidence in TASK_PROGRESS.md |
| M1 · Synthetic vertical slice | A separate collector samples a seeded furnace profile, writes a durable SQLite outbox and uploads to PostgreSQL. The web app serves one fleet row, live status, history, gaps and CSV. Include a dev-only bootstrap and Playwright happy path. | Close browser and verify continued collection. Kill API or simulate lost ACK, then recover expected IDs exactly once in the historian. Display freshness without guessing process state. No external runtime requests. | Simulated evidence attached (TASK_PROGRESS.md); awaiting owner review |
| M2 · Reliability and chart workbench | Deliver the three default profiles (furnace, UTM and vacuum dilatometer) and opt-in DSC. Implement the shared status matrix, typed profiles, synchronized trends, run-specific XY views, axes, saved views, run annotations, diagnostics, scoped accounts, audit, rollups and durable exports. | Pass auth, fault and known-answer tests for all four profiles. The 30-day furnace plot retains the peak and dropout. Verify tensile UTS and fracture ordering, vacuum pump-down and signed displacement, and the DSC peak and sign. Run a scientist usability review, fix findings and repeat. | Simulated evidence attached — profiles, XY views, rollups (30-day 8-series p95 1.12 s), runs/annotations, roles, saved views, durable exports, diagnostics; scientist usability review open |
| M3 · Simulator-only release and commissioning workflow | Implement the profile wizard and collector provisioning, Windows and Ubuntu service packaging, local help, certificate and backup recovery runbooks, standalone HTTPS deployment and suite catalog staging plan. Rehearse no-internet installation, upgrades and rollback. | Fresh install from transferred artifacts in representative VMs. Browser sees only approved local requests. Prove startup after reboot/logoff, scope denials, 72 h synthetic soak, throughput, query latency, restore, and portal/catalog compatibility in staging. A technician commissions simulated profiles without developer help. Ship the three default instruments and the opt-in DSC in the reviewable simulator-only release. | In progress: local help, offline install/upgrade/rollback rehearsal, backup + PITR rehearsal, soak tooling and Ubuntu/Windows service packaging done with simulated evidence (Windows not yet run). Open: staging VMs, reboot/logoff, 72 h soak, profile wizard, certificates, catalog. |
| M4 · First real source, when selected | Confirm hardware and site route. Implement one read-only adapter for the selected logger or documented numeric interface. A bench thermocouple is an optional first exercise. If only a video display signal is available, assess capture and data quality separately before committing to an adapter. | Record device identity, reference agreement, measured cadence, source-fault/reconnect behavior, reboot and network-outage evidence. Attach commissioning report and sign-off. | Deferred |
| M5 · Read-only furnace and additional sources, as needed | For each selected source, review the OEM interface and institutional controls. For a furnace, deliver PV, available setpoint and state, calibration evidence, excursion metrics and PITR operations. | Prove local controller operation remains independent. Verify documented read rate and decoded values, read-only wire behavior and recovery drills. Obtain custodian and site approval before relying on the app during a long real run. | Deferred |
| M6 · Wider lab rollout | Stage the ML Server catalog entry and, where supported, deploy integration through the pinned release manifest. Add validated protocol profiles for actual user needs and reassess disk, retention and access across labs. | Pilot scientists independently complete fleet triage, incident review, a 30-day zoom/export, and profile commissioning as authorized. Resolve critical issues. Record performance and limits on the target host and leave a support handoff. | Pending |

M0–M3 constitute the simulator-first delivery. M4 and M5 are conditional follow-on work and are ordered by the lab’s needs. A separate future proposal governs commands to real machines.

## First implementation tranche

1. Commit the three planning files. Create a small, independently runnable codebase with documented local dependencies and configuration. Create a disposable PostgreSQL database and test its first migration.
2. Define the furnace profile and a deterministic source with a 1 Hz virtual clock option. Keep the ambient source as a small smoke fixture. Add immutable envelope and batch fixtures, one collector, a SQLite outbox and a CLI to inspect pending and acknowledged counts.
3. Implement batch ingestion, a central receipt, one scoped history endpoint and one instrument status endpoint. Validate duplicate submission, commit-before-acknowledgment, rejection semantics and equal readings arriving on time.
4. Serve a minimal fleet page and a live/history chart with a clearly labeled simulator, range controls, explicit gaps and basic CSV. Verify the full path in a browser. Add the UTM, vacuum dilatometer and opt-in DSC in M2 using the same contracts.
5. Record how to run it, exact validation results, current limits and next milestone in `TASK_PROGRESS.md`. Keep long-range analytics and real hardware for their gates.

## Second implementation tranche (done, simulated)

1. Adopt the MIT license, confirm the platform baseline and make CI manual-dispatch only with a local gate (`scripts/check.sh`, ADR 0005). Bake the engineering standards into [CONTRIBUTING.md](CONTRIBUTING.md).
2. Add the UTM, vacuum dilatometer and opt-in DSC simulators behind one source contract with per-instrument multi-rate scheduling, instrument-reported runs and documented known answers (ADR 0004).
3. Add migration `0002` (runs, channel display metadata), run endpoints and frame-paired tensile and DSC analyses, verified end to end through the real collector path.
4. Redesign the UI: fleet dashboard with KPIs, instrument cards and sparklines; instrument workspace with Overview (profile-driven small multiples, log/fixed axes, tolerance band, gaps), Runs & XY analysis, and Diagnostics; light and dark themes.
5. Add rehearsal playback (`labmon-collector backfill`) and fix outbox append cost so it does not grow with the backlog.

Next: M2 rollups for 30-day views, persisted missing intervals, saved views and annotations, role matrix, durable exports with manifests, then the scientist usability review.

## Test and evidence matrix

| Test layer | Required evidence |
| --- | --- |
| Unit and golden | Register decode, typed scaling, timestamp selection, coverage, extrema tie-breaks, bad sensor states, gap detection, deterministic fault scripts, spool boundaries and timing using virtual clocks. |
| Integration | Real SQLite and PostgreSQL. Crash after local persist, before central commit, after central commit before ACK, and before local dequeue. Include same-ID conflict, simultaneous retries, database outages and a profile update during backlog. Reconcile expected IDs and payloads at each boundary. |
| API/security | Scope checks for every ID-bearing endpoint including exports and diagnostics. Revoked and expired credentials, hostile paths/URLs, compressed payload expansion, CSRF, schema limits, malformed samples, device allowlist and default no writes. Validate access changes after job submission. |
| Browser and usability | Simulated offline session, stale state, race between two range selections, fresh stable value, zoom without snap-back, fixed/auto axes, 30-day spike and gaps, drill to raw, paired XY views with missing points and preserved run order, CSV parity and chart exports. Run keyboard-only and reduced-motion checks, screenshots at supported sizes, and manual chart reading by scientists. |
| Performance and soak | Pin release and fixture seed, record CPU, RAM, storage, software versions, concurrent users and ingest/replay workloads. Record p50/p95 for capture-to-visible and plot response, cold/warm results, disk growth, resource saturation and missing data. Gate against the agreed limits in §10 of the specification. |
| Offline and recovery | Fresh install with egress blocked, then baseline upgrade, compatibility rollback, corrupt transfer refusal, expired certificate, reboot, disk-pressure and restore drills. Verify a real client reaches the configured service URL. Record last sample before failure, first after recovery, recovered IDs and any loss. |
| Field acceptance | Evidence from actual logger/PC, approved wiring/map, reference measurement, custodian sign-off, documented outage tests and operational ownership. Keep redacted captures out of public git and transfer them only through approved channels. |

### Production and integration gate

Pin the running suite, portal and service baseline by immutable commits or signed artifacts. Show that the catalog entry uses the correct intranet address and can be disabled independently. Verify existing tools still open, unaffected services are not restarted, and shared production data are outside release directories. Confirm the collector can spool across a central release change. PostgreSQL major upgrades and destructive schema changes have dedicated migration plans. Normal release rollback retains all accepted data.

### Work that depends on site decisions

- Identify who operates the VM, database, backups and instrument PCs, and who signs off hardware and network changes.
- For each selected future instrument, confirm the existing logger or display-to-PC route, data format, safety boundaries, privileges and approved package transfer route.
- Agree on the sample-rate ceiling, recovery objectives and retention before raising the supported deployment size. Store measured evidence and update the specification when estimates change.
