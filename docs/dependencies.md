# Runtime dependency evidence

This project is MIT-licensed. Every dependency below is compatible with MIT distribution;
psycopg is LGPL-3.0 and is used as an unmodified, dynamically loaded library.

Pinned exactly in `uv.lock` (hash-locked); `web/package-lock.json` for the UI. Offline transfer:
`uv export --frozen --no-dev -o requirements.txt` then
`pip download -r requirements.txt --only-binary=:all: --platform <tag> -d wheelhouse/` on a
connected staging host, verify hashes, transfer, and install with
`pip install --no-index --find-links wheelhouse -r requirements.txt`. UI assets ship prebuilt.

| Package | Purpose | License | Platform notes |
| --- | --- | --- | --- |
| fastapi, starlette | HTTP API | MIT / BSD-3 | pure Python |
| pydantic, pydantic-core | Schemas | MIT | binary wheels for win_amd64, manylinux x86_64 |
| sqlalchemy | DB access | MIT | wheels incl. greenlet |
| psycopg[binary] | PostgreSQL driver | LGPL-3.0 | binary wheels bundle libpq; dynamic linking (LGPL) |
| alembic, mako | Migrations | MIT | pure Python |
| uvicorn, h11 | ASGI server | BSD-3 / MIT | pure Python |
| httpx, httpcore | Collector HTTP client | BSD-3 | pure Python |
| argon2-cffi(+bindings), cffi | Password hashing (Argon2id) | MIT | wheels for win_amd64 and manylinux |
| tzdata | IANA zones on Windows | Apache-2.0 | pure Python |
| react, react-dom (19.2) | UI | MIT | bundled |
| echarts, zrender (6.1) | Charts | Apache-2.0 | bundled |

Operator-supplied tools (not redistributed): **WinSW v2** (MIT, `WinSW-x64.exe`) wraps the
Windows 11 collector as a service (ADR 0007). Use v2.12.0 or a later 2.x release from the
project's official releases. Record its SHA-256 at download in the site's software register,
and verify that hash before every install.

The release bundle also carries `wheelhouse-win/` with the same hash-locked set for CPython
3.12 `win_amd64`. On 2026-11-06 all 27 distributions resolved to Windows wheels (11 MB).

Dev-only: pytest (MIT), ruff (MIT), mypy (MIT), playwright (Apache-2.0), vite (MIT),
typescript (Apache-2.0), prettier (MIT). An SBOM (CycloneDX) is an M3 release deliverable.
