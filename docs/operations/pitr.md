# Point-in-time recovery (PostgreSQL 16, central host)

Daily `labmon-server backup` dumps protect against losing the host. They lose up to a day of
readings. For multi-day furnace runs, add WAL archiving and weekly base backups. You can then
recover to any moment, for example the minute before an operator error. This is a **site
procedure**: the database team owns the PostgreSQL configuration and the archive storage.

Evidence: **simulated**. `tests/e2e/test_pitr.py` rehearses this page on disposable clusters.
It covers archiving, a base backup, writes, an accidental delete, and recovery into a separate
cluster at a target time. The restored database must keep every row committed before the target
and be at the release's schema head. It has not been staged or field-verified.

## 1 · Archive WAL (once)

Put the archive on a different disk from the data directory, and preferably on a different host.
In `postgresql.conf`:

```ini
wal_level = replica
archive_mode = on                       # needs a restart
archive_command = 'test ! -f /srv/pg-archive/%f && cp %p /srv/pg-archive/%f'
archive_timeout = 300                   # bounds data loss on a quiet system to 5 min
```

`test ! -f` refuses to overwrite a segment that is already archived. Use the site's tool, such as
pgBackRest, if one is approved. Check that archiving is healthy:

```bash
sudo -u postgres psql -c "SELECT archived_count, last_archived_wal, failed_count, last_failed_wal FROM pg_stat_archiver"
```

A growing `failed_count` means WAL piles up in `pg_wal` and the disk fills. Treat it as an alert.

## 2 · Base backups (weekly)

```bash
sudo -u postgres pg_basebackup -D /srv/pg-base/$(date -u +%Y%m%dT%H%MZ) -X stream -c fast --checkpoint=fast -P
```

Keep at least two base backups. Keep all WAL from the oldest one you keep. Delete older WAL only
together with its base backup, using `pg_archivecleanup` against the oldest retained backup's
`.backup` label. Keep the daily `labmon-server backup` dumps as well. They are independent and
`restore-check` verifies them.

## 3 · Recover to a point in time

Recover into a **new data directory or a scratch host**, never over the live cluster. Compare the
result, and switch only after a decision is recorded. An ordinary code rollback never restores data
(deployment.md §4).

1. Choose the target from the audit log or the incident report, for example
   `2026-11-06 14:02:00+00`. Recovery stops at the last transaction committed before it.
2. Stop the services that write: `sudo systemctl stop labmon-api labmon-worker`. Collectors keep
   spooling. They upload their pending records after the switch, and ingest identity makes the
   retries idempotent (ADR 0002).
3. Copy the newest base backup taken **before** the target into a new directory owned by
   `postgres` with mode 0700.
4. In that copy's `postgresql.conf`, append:

   ```ini
   port = 5433                                  # not the live port
   archive_mode = off                           # never archive into the live archive
   restore_command = 'cp /srv/pg-archive/%f %p'
   recovery_target_time = '2026-11-06 14:02:00+00'
   recovery_target_action = 'promote'
   ```

   Then create the signal file: `touch <dir>/recovery.signal`.
5. Start it with `pg_ctl -D <dir> -l <dir>/recovery.log start` and wait for
   `SELECT pg_is_in_recovery()` to return `f`.
6. Verify it with the release's tools:

   ```bash
   LABMON_DATABASE_URL=postgresql+psycopg://labmon@127.0.0.1:5433/labmon labmon-server doctor
   ```

   Check that the schema matches the release. Compare the row counts of `sample`, `run`,
   `annotation` and `audit_event` with the incident record.
7. Decide, and record the decision. Either make the recovered cluster the live one (stop the old
   one, swap the data directories or ports, start the services, run `doctor` again), or copy
   only the lost rows back. Expand-only migrations mean the current release reads the recovered
   schema.

**Data between the target and the stop is not re-sent automatically.** The collectors have
already had those records acknowledged. The records stay in each collector's replay cache for
`replay_retention_days` (default 7), but no command re-sends acknowledged records yet (open
item). The restored history therefore shows a gap for that interval. Record it in the incident
report and annotate the affected instruments.

## 4 · Rehearse

Run the drill every quarter and before a long heat treatment (SPECIFICATIONS §10). Record the base
backup age, how much WAL was replayed, the recovery duration and the row-count comparison in the
operations log. On a development machine:

```bash
.venv/bin/pytest -q tests/e2e/test_pitr.py
```
