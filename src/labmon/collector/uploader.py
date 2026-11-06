"""Upload persisted batches and post heartbeats.

Response handling (protocol v1):

=========================  ===================================================================
200 + ``committed``        dequeue (records stay in the replay cache)
422 + ``rejected``         quarantine the named identities, requeue valid siblings for a new batch
409 ``batch_conflict``     the API holds a different payload under this batch id: stop, keep data
401 / 403                  stop uploading, raise an actionable fault; collection continues
413 / other 4xx            treat as a permanent fault for this batch: stop and surface it
429 / 5xx / network        retry the *same* batch with bounded exponential backoff and jitter
=========================  ===================================================================
"""

from __future__ import annotations

import gzip
import logging
import random
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

import httpx

from labmon import __version__
from labmon.collector.outbox import Outbox, PendingBatch
from labmon.contracts.envelope import PROTOCOL_VERSION, format_utc
from labmon.sim.clock import Clock

log = logging.getLogger(__name__)

INGEST_PATH = "/api/v1/ingest/batches"
HEARTBEAT_PATH = "/api/v1/collectors/heartbeat"


class StepResult(StrEnum):
    IDLE = "idle"
    ACKED = "acked"
    REJECTED = "rejected"
    RETRY = "retry"
    FAULT = "fault"


@dataclass
class Backoff:
    base_s: float = 1.0
    cap_s: float = 60.0
    rng: random.Random | None = None
    failures: int = 0

    def next_delay(self) -> float:
        self.failures += 1
        ceiling = min(self.cap_s, self.base_s * (2 ** (self.failures - 1)))
        jitter = (self.rng or random.Random()).uniform(0, ceiling / 2)
        return float(ceiling / 2 + jitter)  # "equal jitter": never zero

    def reset(self) -> None:
        self.failures = 0


class Uploader:
    def __init__(
        self,
        outbox: Outbox,
        client: httpx.Client,
        token: str,
        clock: Clock,
        *,
        max_backoff_s: float = 60.0,
        flush_s: float = 1.0,
        compress: bool = True,
        after_commit_hook: Callable[[PendingBatch], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self.client = client
        self._token = token
        self.clock = clock
        self.backoff = Backoff(cap_s=max_backoff_s)
        self.flush_s = flush_s
        self.compress = compress
        self.fault: str | None = None
        self._after_commit_hook = after_commit_hook  # test hook only

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": f"labmon-collector/{__version__}",
        }
        if self.compress:
            h["Content-Encoding"] = "gzip"
        return h

    def step(self) -> tuple[StepResult, float]:
        """Send at most one batch; return the result and the suggested wait before the next step."""
        if self.fault is not None:
            return StepResult.FAULT, self.backoff.cap_s
        batch = self.outbox.next_batch()
        if batch is None:
            return StepResult.IDLE, self.flush_s
        attempt = batch.attempts + 1
        body = batch.payload(self.clock.now(), attempt)
        if self.compress:
            body = gzip.compress(body, compresslevel=6, mtime=0)
        try:
            resp = self.client.post(INGEST_PATH, content=body, headers=self._headers())
        except httpx.HTTPError as exc:
            self.outbox.record_attempt(batch.batch_id, f"network: {type(exc).__name__}")
            delay = self.backoff.next_delay()
            log.warning("upload of %s failed (%s); retry in %.1fs", batch.batch_id, exc, delay)
            return StepResult.RETRY, delay
        return self._handle(batch, resp)

    def _handle(self, batch: PendingBatch, resp: httpx.Response) -> tuple[StepResult, float]:
        status = resp.status_code
        payload: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}
        if status == 200 and payload.get("outcome") == "committed":
            if (
                payload.get("batch_id") != batch.batch_id
                or payload.get("payload_digest") != batch.digest
            ):
                return self._fault(batch, "ack does not match the batch sent")
            self.outbox.record_attempt(batch.batch_id, None)
            self.outbox.mark_acked(batch.batch_id, int(payload["receipt_id"]))
            self.backoff.reset()
            if self._after_commit_hook:
                self._after_commit_hook(batch)
            return StepResult.ACKED, 0.0
        if status == 422 and payload.get("outcome") == "rejected":
            rejected = [
                (int(r["spool_epoch"]), int(r["seq"]), str(r["code"]), str(r["message"]))
                for r in payload.get("rejected", [])
            ]
            self.outbox.record_attempt(batch.batch_id, "rejected")
            q, rq = self.outbox.mark_rejected(batch.batch_id, rejected, "rejected by central API")
            log.error("batch %s rejected: %d quarantined, %d requeued", batch.batch_id, q, rq)
            return StepResult.REJECTED, 0.0
        if status == 409:
            self.outbox.mark_conflict(batch.batch_id, str(payload.get("detail", "conflict")))
            self.fault = "batch_conflict"
            return StepResult.FAULT, self.backoff.cap_s
        if status in (401, 403):
            return self._fault(
                batch,
                f"authentication refused ({status}); check the collector "
                "credential and its revocation state",
            )
        if status == 429 or status >= 500:
            retry_after = resp.headers.get("Retry-After")
            self.outbox.record_attempt(batch.batch_id, f"http {status}")
            delay = self.backoff.next_delay()
            if retry_after and retry_after.isdigit():
                delay = max(delay, min(float(retry_after), self.backoff.cap_s))
            return StepResult.RETRY, delay
        return self._fault(batch, f"permanent http {status}: {str(payload)[:200]}")

    def _fault(self, batch: PendingBatch, reason: str) -> tuple[StepResult, float]:
        log.error("upload stopped: %s", reason)
        self.outbox.record_attempt(batch.batch_id, reason[:200])
        self.outbox.set_upload_fault(reason)
        self.fault = reason
        return StepResult.FAULT, self.backoff.cap_s

    def drain(self, max_steps: int = 10_000) -> list[StepResult]:
        """Upload until idle, fault or retry (tests and one-shot tools)."""
        results = []
        for _ in range(max_steps):
            res, _ = self.step()
            results.append(res)
            if res in (StepResult.IDLE, StepResult.FAULT, StepResult.RETRY):
                break
        return results

    def run(self, stop: threading.Event, retention_s: float) -> None:
        last_prune = self.clock.monotonic()
        while not stop.is_set():
            try:
                _, wait = self.step()
                if self.clock.monotonic() - last_prune > 3600:
                    self.outbox.prune_acked(timedelta(seconds=retention_s))
                    last_prune = self.clock.monotonic()
            except Exception:  # noqa: BLE001 - keep the uploader alive; data stays in the outbox
                log.exception("uploader step failed; retrying")
                wait = self.backoff.next_delay()
            if wait > 0 and self.clock.sleep(wait, stop):
                break


class Heartbeat:
    def __init__(
        self,
        outbox: Outbox,
        client: httpx.Client,
        token: str,
        clock: Clock,
        sources: Callable[[], list[dict[str, str]]],
        sampling: Callable[[], bool],
        uploader: Uploader | None = None,
    ) -> None:
        self.outbox, self.client, self._token, self.clock = outbox, client, token, clock
        self.sources, self.sampling, self.uploader = sources, sampling, uploader
        self.last_status: int | None = None

    def body(self) -> dict[str, Any]:
        c = self.outbox.counts()
        return {
            "v": PROTOCOL_VERSION,
            "collector_time": format_utc(self.clock.now()),
            "software_version": __version__,
            "spool_epoch": c.epoch,
            "next_seq": c.next_seq,
            "unacknowledged": c.unacknowledged,
            "quarantined": c.quarantined,
            "oldest_pending_capture_time": c.oldest_pending_capture_time,
            "spool_capacity": c.capacity,
            "sampling": self.sampling(),
            "upload_fault": (self.uploader.fault if self.uploader else None) or c.upload_fault,
            "sources": self.sources(),
            **self._disk(),
        }

    def _disk(self) -> dict[str, int]:
        try:
            usage = shutil.disk_usage(self.outbox.path.parent)
            size = self.outbox.path.stat().st_size
        except OSError:
            return {}
        return {
            "spool_disk_free_bytes": usage.free,
            "spool_disk_total_bytes": usage.total,
            "spool_file_bytes": size,
        }

    def send(self) -> bool:
        try:
            resp = self.client.post(
                HEARTBEAT_PATH,
                json=self.body(),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": f"labmon-collector/{__version__}",
                },
            )
            self.last_status = resp.status_code
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("heartbeat failed: %s", exc)
            self.last_status = None
            return False

    def run(self, stop: threading.Event, every_s: float) -> None:
        while not stop.is_set():
            try:
                self.send()
            except Exception:  # noqa: BLE001 - a heartbeat failure must not end the process
                log.exception("heartbeat failed")
            if self.clock.sleep(every_s, stop):
                break
