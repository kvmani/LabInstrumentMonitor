# ADR 0004 — Simulator fleet, multi-rate acquisition and instrument-reported runs

**Status** Accepted · 2026-11-06 · supersedes the single-cadence scheduler of M1

## Context

The first release ships the furnace, a universal tensile testing machine (UTM) and a fictional
vacuum dilatometer by default, plus an opt-in DSC (SPECIFICATIONS §7). The UTM samples at 5 Hz
during a test and 1 Hz while idle. Tensile and DSC views need run-scoped XY plots, and the UTS
needs specimen geometry "included in the run".

## Decision

1. **One source contract** (`labmon.sim.base.Source`): `read(t_ms)` returns one acquisition frame
   for a *nominal* sample time in milliseconds; `cadence_ms(t_ms)` returns the interval the
   source wants next. Values are pure functions of `(seed, t_ms)`; faults are scripted by time.
   Each instrument lives in its own module with its known answers in the module docstring.
2. **Independent schedules.** The collector keeps one nominal schedule per instrument mapped to
   the monotonic clock from a single origin. Stalls skip to the latest due grid point with one
   `schedule_skipped` event (no bursts). The frame ID is `<instrument>:<t_ms>`.
3. **Runs come from the instrument program.** A source reports the current `RunInfo`; the
   collector emits `run_started` (name, kind, program start/end, metadata such as geometry or
   sample mass) and `run_ended` events in the normal identity space. The server validates their
   detail and maintains a `run` projection inside the ingest transaction (idempotent:
   insert-on-conflict-do-nothing, end set once). Events stay the source of truth. Run IDs are
   `<instrument>.<run key>`; the simulator uses one key per program cycle, so repeated recipes
   always create new runs. User-created runs (RUN-01) reuse the table with `source = 'user'`.
4. **XY analyses pair raw values by frame ID**, keep acquisition order and break at invalid
   pairs. Tensile UTS is reported only for complete runs with geometry. DSC uses the profile's
   declared sign convention (*exo up* for the simulator) and segments by programmed-temperature
   slope; peak areas use a straight baseline and are labelled as estimates.
5. **Display hints are data in the profile revision** (panels, log permission, tolerance,
   analysis kind, sign convention), so the UI is driven by the immutable revision, not by code
   per instrument.
6. **Rehearsal playback** (`labmon-collector backfill`) runs the same acquisition code on a
   virtual clock with original past timestamps and marks `acquisition_started` with
   `mode = historical_playback`. It is a development and rehearsal tool, not a recovery path.

## Consequences

- Adding a simulated (or later a real) instrument means one source module, one profile and
  known-answer tests; scheduling, spooling, ingest, status, history, runs and the UI are shared.
- New event types are additive within protocol v1, but an older server rejects unknown event
  types; upgrade the central API before collectors that emit new events.
- Simulated UTM and dilatometer channels declare `expected_cadence_s = 1.0` (their slowest rate);
  freshness windows therefore stay at 5 s.
