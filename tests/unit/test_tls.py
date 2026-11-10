"""Certificate check against real TLS servers with certificates from a throwaway test CA."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from labmon.server.tls import check_tls

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
NOW = datetime.now(UTC)


def run(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("pki")
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "30", "-subj", "/CN=labmon test CA", cwd=d)  # fmt: skip
    (d / "index.txt").write_text("")
    (d / "serial").write_text("1000\n")
    (d / "ca.cnf").write_text(
        "[ca]\ndefault_ca = test\n[test]\ndir = .\ndatabase = index.txt\nserial = serial\n"
        "new_certs_dir = .\ncertificate = ca.pem\nprivate_key = ca.key\ndefault_md = sha256\n"
        "policy = any\nunique_subject = no\ncopy_extensions = copy\n[any]\ncommonName = supplied\n"
    )

    def leaf(name: str, host: str, *dates: str) -> None:
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out",
            f"{name}.csr", "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}",
            cwd=d)  # fmt: skip
        run("ca", "-batch", "-config", "ca.cnf", "-in", f"{name}.csr", "-out", f"{name}.pem",
            *dates, cwd=d)  # fmt: skip

    leaf("good", "localhost", "-days", "400")
    leaf("soon", "localhost", "-days", "10")
    leaf("other", "other.example", "-days", "400")
    leaf("expired", "localhost", "-startdate", "20240101000000Z", "-enddate", "20240201000000Z")
    return d


@contextmanager
def tls_server(pki: Path, name: str) -> Iterator[int]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki / f"{name}.pem", pki / f"{name}.key")
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass  # the client rejected our certificate: expected in failure tests

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield port
    finally:
        stop.set()
        t.join(2)
        srv.close()


def test_valid_certificate(pki: Path) -> None:
    with tls_server(pki, "good") as port:
        r = check_tls(f"https://localhost:{port}", pki / "ca.pem", NOW)
    assert r["status"] == "ok", r
    assert 395 < r["days_left"] <= 400 and r["names"] == ["localhost"]
    assert r["issuer"] == "CN=labmon test CA" and r["protocol"].startswith("TLSv1.")


def test_expiring_soon_warns(pki: Path) -> None:
    with tls_server(pki, "soon") as port:
        r = check_tls(f"https://localhost:{port}", pki / "ca.pem", NOW)
    assert r["status"] == "warn" and "renew now" in r["detail"], r


@pytest.mark.parametrize(
    ("name", "ca", "reason"),
    [
        ("expired", True, "expired"),
        ("other", True, "Hostname mismatch"),
        ("good", False, "unable to get local issuer certificate"),
    ],
)
def test_rejected_certificates_fail(pki: Path, name: str, ca: bool, reason: str) -> None:
    with tls_server(pki, name) as port:
        r = check_tls(f"https://localhost:{port}", pki / "ca.pem" if ca else None, NOW)
    assert r["status"] == "fail" and reason in r["detail"], r


def test_bad_url_and_no_listener_fail() -> None:
    assert check_tls("http://localhost", None, NOW)["status"] == "fail"
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    r = check_tls(f"https://localhost:{port}", None, NOW, timeout_s=1)
    assert r["status"] == "fail" and "no TLS handshake" in r["detail"]
