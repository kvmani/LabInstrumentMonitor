# ADR 0006 — Offline release bundle, versioned installs and code-only rollback

**Status** Accepted for the simulator-only release · 2026-11-06

## Decision

- A release is a tarball built on a connected host of the target platform: hash-locked
  wheelhouse, prebuilt UI, deployment assets, SBOM, `RELEASE.json`, `SHA256SUMS`.
- `deploy/install.sh` installs offline only (`--no-index --require-hashes`) into
  `/opt/labmon/releases/<version>` (built in place behind an `.installing` marker because
  virtualenvs embed absolute paths) and activates by atomically replacing the
  `/opt/labmon/current` symlink. Rollback is `--activate <previous>`.
- Configuration and state never live in a release: `/etc/labmon`, `/var/lib/labmon`.
- Migrations are expand-only; a code rollback never touches data. `labmon-server doctor` is the
  read-only preflight after every install and upgrade.
- Logical backups (`labmon-server backup`) with checksums, counts and a status file; restores
  are verified only into scratch databases (`restore-check`).
- systemd units run as the unprivileged `labmon` user with a read-only filesystem except
  `/var/lib/labmon`.

## Consequences

- The rehearsal found that renaming a built virtualenv breaks its entry points; fixed by
  building in place.
- Windows collector packaging: ADR 0007. PITR: docs/operations/pitr.md (rehearsed on
  disposable clusters). Releases keep a copy of `deploy/` so `--activate` works from the active
  release.
