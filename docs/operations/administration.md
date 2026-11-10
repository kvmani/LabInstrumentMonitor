# Administration (operators)

`labmon-server admin` runs on the central host with database access. It has no web endpoint, so
it works before any user exists and is unreachable from browsers. Every change is written to the
audit log with actor `cli:<OS user>`. Set `LABMON_DATABASE_URL` (and `LABMON_ENV=production`) in
the operator shell first.

## Roles

| Role | May |
| --- | --- |
| viewer, auditor | read instruments, history, runs, annotations, exports of their labs; personal saved views |
| scientist | as viewer, plus create and correct run records and annotations |
| custodian | as scientist, plus share saved views as lab templates and commission instruments (wizard) |
| administrator | as custodian, plus add collectors, download collector packages and credentials, revoke collectors (web UI) |

Instrument control does not exist in v1 for any role.

## Common tasks

```bash
labmon-server admin lab-add metals "Metals lab"
labmon-server admin user-add dana --display-name "Dana Rao"          # prompts for the password
labmon-server admin grant dana metals scientist                        # replaces an existing grant
labmon-server admin grant-revoke dana metals
labmon-server admin user-password dana                                 # also ends her sessions
labmon-server admin user-disable dana                                  # blocks sign-in, ends sessions
labmon-server admin user-list
```

Passwords are never accepted as arguments (they would land in shell history); use the prompt or
`--password-stdin` from a protected file or secret store.

## Commissioning in the web UI (custodian, administrator)

**Commission** runs the six-step wizard (SPECIFICATIONS §8, ADR 0008): identify, connect
(profile and seed), map (documented channels), preview (simulator plus a dropout drill), approve
(compare and publish an immutable revision) and verify. Drafts are stored on the server and
resume after an interruption. Publishing needs a passing preview of exactly the drafted profile
and seed. Re-commissioning an existing instrument publishes revision N+1 and keeps the earlier
revisions. Changing the profile type or moving the instrument to another collector is refused.

After publishing, an administrator opens **Collectors**:

- **Configuration** downloads a ZIP with `collector.toml` for the collector's current
  assignments. The installed credential stays valid.
- **New credential** adds a new `collector.token` and revokes the previous credentials at once.
  The archive is the only copy of the token; the server keeps only its digest. Production
  packages require an `https://` monitor URL.
- **Revoke** stops uploads at once. Run `labmon-collector flush` first when retiring a PC.

Install the files through the approved channel, restrict the token file (Linux 0600; Windows,
see deployment.md §6) and restart the collector. The wizard's Verify step shows **Active** only
after the collector reports the new revision in its heartbeat and its first samples are
committed. Print or download the commissioning record. Every step is audited with actor
`user:<name>`.

## Collectors and simulated instruments (CLI)

```bash
labmon-server admin collector-add col-m1 --lab metals --name "Metals bay PC"
labmon-server admin instrument-enrol utm-m1 --lab metals --name "UTM M1" \
    --profile sim-utm --seed 5 --collector col-m1                     # simulated only in v1
labmon-server admin collector-token col-m1 --out /secure/col-m1.token  # 0600, revokes old tokens
labmon-server admin collector-config col-m1 --server-url https://monitor.lab.example \
    --out-dir /secure/col-m1/
labmon-server admin collector-revoke col-m1                            # stops uploads at once
```

Transfer `collector.toml` and the token file to the instrument PC through the approved channel;
the token file must stay owner-readable only (the collector refuses group/other-readable files).
After rotating a token, restart the collector; the spool keeps every reading meanwhile.

To retire a collector host, stop the collector service, then upload what is left and confirm:

```bash
labmon-collector flush --config collector.toml --timeout 300   # exit 0: every record acknowledged
labmon-collector status --config collector.toml                # capacity 0/…
```

`flush` acquires nothing and takes the spool lock, so it refuses to run beside the service.
Only revoke the credential after `flush` succeeded.
Profiles available: `sim-furnace`, `sim-utm`, `sim-dilatometer`, `sim-dsc` (opt-in) and
`sim-ambient` (smoke fixture).
