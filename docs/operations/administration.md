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
| custodian, administrator | as scientist, plus share saved views as lab templates |

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

## Collectors and simulated instruments

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
Profiles available: `sim-furnace`, `sim-utm`, `sim-dilatometer`, `sim-dsc` (opt-in) and
`sim-ambient` (smoke fixture).
