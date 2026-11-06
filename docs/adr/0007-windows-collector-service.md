# ADR 0007 — Windows 11 collector as a WinSW-wrapped service from an offline wheelhouse

**Status** Proposed (not yet run on Windows) · 2026-11-06

## Context

Instrument PCs commonly run Windows 11 (SPECIFICATIONS: collector on Windows 11 and Ubuntu). The
collector must start at boot without a logged-in user, restart after a crash, stop cleanly so
the SQLite spool closes, and install without internet access. Code and state stay apart, as on
the central host (ADR 0006).

## Decision

- **Service wrapper: WinSW v2 (MIT), supplied by the site.** It is a single executable that runs
  `labmon-collector run` as a Windows service. It provides restart-on-failure, delayed automatic
  start and rolling logs. On stop it sends Ctrl+C, which the collector already handles (SIGINT:
  stop acquisition, finish the current upload step, close the spool), and kills the process
  after 30 s. The definition is in `deploy/windows/labmon-collector.xml`.
- **Code:** `%ProgramFiles%\labmon\releases\<version>\venv`, built in place behind an
  `.installing` marker. `%ProgramFiles%\labmon\current` is a directory junction, switched while
  the service is stopped. Rollback: `install-collector.ps1 -Activate <previous>`.
- **State:** `%ProgramData%\labmon\collector` holds the config, token, spool and logs. The
  service runs as `NT AUTHORITY\LocalService` with Modify rights on that directory only. The
  token file has inheritance removed and is readable only by LocalService and Administrators.
  This replaces the POSIX 0600 check, which the collector enforces only on POSIX.
- **Offline packages:** the release bundle carries `wheelhouse-win/`. It is the same
  hash-locked requirement set downloaded for CPython 3.12 `win_amd64` and installed with
  `pip --no-index --require-hashes`. All 27 locked distributions have Windows wheels (11 MB,
  checked 2026-11-06). CPython 3.12 for Windows comes from the site's approved installer.

## Alternatives considered

- **NSSM:** public domain and widely used, but unmaintained since 2017.
- **pywin32 service class:** adds a platform-specific dependency and service code to this
  repository. It would also need its own tests on Windows.
- **Task Scheduler "at startup":** no stop signal and weaker restart semantics, and the task is
  not visible as a service to operators.

## Consequences

- WinSW is an operator-supplied binary. It is not redistributed in the bundle. The site records
  its version and SHA-256 at download (docs/dependencies.md).
- `install-collector.ps1` and the XML are covered only by static checks
  (`tests/unit/test_deploy_assets.py`). The M3 gate needs a Windows 11 VM rehearsal: install,
  reboot without logon, kill the process, stop/start, token ACL, rollback, and spool survival.
- The collector's signal handling and spool lock (msvcrt) already have Windows code paths, but no
  test has run them on Windows.
