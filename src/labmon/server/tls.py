"""TLS certificate check of the served HTTPS endpoint, as a client sees it (standard library only).

``check_tls`` performs a real handshake against the configured public URL and validates the chain
and the host name, using the site CA bundle if one is given. It reports the certificate's subject,
issuer, names and days until expiry. An expired, untrusted or wrong-host certificate fails
verification, which is the same thing browsers and collectors would see. Nothing is written.

Results are cached in-process for a few minutes, so the Diagnostics page can show certificate
health without a handshake on every poll.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WARN_DAYS = 30
CACHE_S = 300.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _names(cert: dict[str, Any], key: str) -> str:
    parts = []
    for rdn in cert.get(key, ()):
        for k, v in rdn:
            if k in ("commonName", "organizationName"):
                parts.append(f"{'CN' if k == 'commonName' else 'O'}={v}")
    return ", ".join(parts)


def check_tls(
    url: str, ca_file: Path | None, now: datetime, timeout_s: float = 5.0
) -> dict[str, Any]:
    """Handshake with ``url`` (``https://host[:port]``). Returns a JSON-able result with
    ``status`` ok | warn | fail and a human ``detail``."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return {"status": "fail", "url": url, "detail": "check URL must be https://host[:port]"}
    host, port = parts.hostname, parts.port or 443
    ctx = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                peer = tls.getpeercert()
                version = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return {
            "status": "fail",
            "url": url,
            "detail": f"certificate rejected: {exc.verify_message or exc.reason}",
        }
    except (OSError, ssl.SSLError) as exc:
        return {
            "status": "fail",
            "url": url,
            "detail": f"no TLS handshake: {exc.__class__.__name__}",
        }
    if not peer:
        return {"status": "fail", "url": url, "detail": "server presented no certificate"}
    cert: dict[str, Any] = dict(peer)
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)
    days = (not_after - now).total_seconds() / 86400
    sans = [v for k, v in cert.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    status = "ok" if days >= WARN_DAYS else "warn"
    detail = f"valid for {host}, expires {not_after:%Y-%m-%d} ({days:.0f} days)" + (
        "" if status == "ok" else f" — renew now (warning below {WARN_DAYS} days)"
    )
    return {
        "status": status,
        "url": url,
        "detail": detail,
        "subject": _names(cert, "subject"),
        "issuer": _names(cert, "issuer"),
        "names": sans,
        "not_after": not_after.isoformat().replace("+00:00", "Z"),
        "days_left": round(days, 1),
        "protocol": version,
    }


def cached_check(url: str, ca_file: Path | None, now: datetime) -> dict[str, Any]:
    key = (url, str(ca_file or ""))
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    result = check_tls(url, ca_file, now)
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result
