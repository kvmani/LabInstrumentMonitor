# ADR 0008 — Commissioning drafts, immutable revisions and collector-confirmed activation

**Status** Accepted for the simulator-only release · 2026-11-10

## Context

SPECIFICATIONS §8 and ONB-01 require a commissioning wizard. Interrupted wizards must resume. A
preview shows readings with quality and units before activation. Publishing creates an immutable
profile revision. Activation is shown only after the collector acknowledges the applied revision,
and a failed apply leaves the active profile in service. Collectors read a static `collector.toml`.
They do not fetch configuration from the server, and v1 adds no remote-configuration channel to
the edge.

## Decision

- **Drafts** live in `commissioning_draft` (migration 0007, expand-only). There is at most one
  open draft per instrument id. A draft never changes an instrument, a profile or an assignment.
- **Preview** runs the same deterministic simulator and the same range rule the collector uses
  (`labmon.sim.preview.range_checked`, a single implementation shared with
  `collector.acquisition`). It then runs a scripted dropout drill. Readings the source itself
  flags as scripted scenario faults do not fail the preview; out-of-range readings do. Nothing
  is stored as telemetry.
- **Publish** needs a complete draft and a passing preview of exactly the drafted profile and
  seed (a later change makes the preview stale). It writes `profile_revision` rN+1 and keeps
  every earlier revision. For a new instrument it also creates the instrument, its channels and
  the collector assignment. The wizard refuses a profile-type change (the channel set would
  change) and a move to another collector; both stay explicit operator actions.
- **Activation** is evidence, not a flag. The collector already sends a heartbeat listing its
  sources. Each entry now also names the `profile_revision_id` the collector runs (additive; older
  collectors omit it and simply never show as applied). Verify reports `awaiting_collector` until
  the heartbeat names the published revision, then `awaiting_samples`, then `receiving` once
  samples carrying that revision are committed. The previous revision keeps ingesting until the
  collector is reconfigured, so a failed apply leaves it in service.
- **Provisioning** in the web UI is for administrators only. A package is a ZIP with
  `collector.toml` for the current assignments and, only on explicit request, a new credential
  that revokes the previous ones. It is served `no-store`, the token exists only in that
  response, and the audit log records the issue but never the token. Production packages require
  an `https://` monitor URL. The CLI and the API share `server/provisioning.py`.
- **Roles:** custodian or administrator commission; administrator provisions. Out-of-scope ids
  answer 404.

## Consequences

- Applying a revision is an operator step: download the package, install it, restart the
  collector. A pull-based configuration channel would remove that step, but it needs its own
  security review and is deferred.
- Hardware modes (device selection, endpoint allowlist, editable maps) are not part of v1. The
  wizard's Map step is read-only for simulator profiles.
- Evidence: integration tests for every rule above, and a browser test in which a custodian
  commissions a dilatometer and the real collector activates it after the administrator's
  package is installed. All **simulated**.
