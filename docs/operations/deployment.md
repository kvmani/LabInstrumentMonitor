# Deployment, upgrade, rollback and backup (Ubuntu 24.04 central host)

Simulator-only release procedure. Evidence so far is **simulated** (a rehearsal inside a
temporary root on the development container, see `TASK_PROGRESS.md`); staging on a
representative VM and field acceptance are still required (ROADMAP M3).

## 1 · Build (connected staging host, same platform)

```bash
scripts/check.sh --e2e && scripts/check.sh --slow     # record the results for this commit
scripts/build-release.sh dist                         # dist/labmon-<version>.tar.gz + .sha256
```

The bundle holds a hash-locked wheelhouse (dependencies + `labmon` wheel), the prebuilt web UI,
`deploy/` assets, `requirements.txt` with hashes, `SBOM.json` (CycloneDX), `RELEASE.json`
(version, commit, schema head) and `SHA256SUMS`. Transfer it through the approved channel and
check `sha256sum -c labmon-<version>.tar.gz.sha256` on arrival.

## 2 · Prerequisites (once)

PostgreSQL 16 (site-managed), `python3.12-venv`, nginx with the site certificate, a `labmon`
system user, and a database role for the application. Use a separate owner role for
migrations if the site policy requires it.

## 3 · Install or upgrade

```bash
tar -xzf labmon-<version>.tar.gz
sudo labmon-<version>/deploy/install.sh labmon-<version>            # first time: edit
sudoedit /etc/labmon/labmon.env                                      # database URL, then:
sudo labmon-<version>/deploy/install.sh --migrate labmon-<version>
sudo -u labmon env $(cat /etc/labmon/labmon.env | xargs) /opt/labmon/current/venv/bin/labmon-server doctor
sudo install -m 0644 labmon-<version>/deploy/nginx/labmon.conf /etc/nginx/sites-available/labmon
```

The installer never downloads (`pip --no-index --require-hashes`), refuses a bundle whose
checksums fail, installs into `/opt/labmon/releases/<version>` behind an `.installing` marker,
switches `/opt/labmon/current` atomically, restarts `labmon-api` and `labmon-worker`, and waits
for `/health/ready`. State stays outside releases: `/etc/labmon` and `/var/lib/labmon`.
Create the first operator account with `labmon-server admin` (see administration.md).

## 4 · Rollback (code only)

```bash
sudo /opt/labmon/current/deploy/install.sh --activate <previous-version>   # or the bundle's copy
```

Migrations are expand-only, so the previous release runs on the newer schema and **no data is
restored or deleted**. Never restore a backup over newer data as part of a code rollback.

## 5 · Backups and restore verification

`labmon-backup.timer` runs `labmon-server backup` daily (custom-format dump, checksum, row
counts, 14-day retention, status file). Doctor and the Diagnostics page report freshness
(warning after 26 h). Verify restores regularly on a scratch database — never the live one:

```bash
labmon-server restore-check /var/lib/labmon/backups/labmon-<stamp>.dump \
    --scratch-admin-url postgresql+psycopg://postgres@127.0.0.1:5432/postgres
```

Before relying on the system during multi-day furnace runs, add base backups with WAL
archiving (point-in-time recovery) as a site procedure and rehearse it (SPECIFICATIONS §10).

## 6 · Collector (instrument PC)

Linux: install the same bundle, place `collector.toml` and the 0600 token under
`/etc/labmon/collector/`, and enable `deploy/systemd/labmon-collector.service`. A Windows 11
service wrapper is still open (M3).
