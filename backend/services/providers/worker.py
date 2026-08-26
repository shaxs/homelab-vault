"""Shared provider-worker plumbing — the loop, the signals, the beat.

**Extracted from `proxmox_worker` so a second provider worker reuses the same
SIGTERM/beat/loop discipline without inheriting the coupling that made one loop
poll every provider.** The latent bug this refactor closes (operator-named as the
highest risk of the TrueNAS chunk): `proxmox_worker`'s pass iterated
`for provider in PROVIDERS`, so the moment `TrueNASProvider` joined that tuple the
Proxmox worker would poll TrueNAS too — under `proxmox.polling.enabled`, on
`proxmox.poll_interval_s`, and beating it into the heartbeat table as `"proxmox"`.
That violates §7c/§5a's independent-death, independent-cadence contract: TrueNAS
would have no heartbeat of its own and would stop being polled the instant Proxmox
polling was switched off.

The fix is structural, not a guard: **each worker is scoped to exactly one
provider by name**, gates on *its own* `*.polling.enabled` / `*.poll_interval_s`,
and beats under *its own* worker key. This module owns only what is genuinely
provider-agnostic — the signal handling, the cadence loop, and the beat. What a
worker *does* per pass is its command's own `run_pass`, which selects its single
provider via `provider_for(name)` and never iterates the registry.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Callable

from django.utils import timezone

from ..db import releases_connections
from ..heartbeat import beat
from ..settings_store import get_setting

logger = logging.getLogger("services.providers.worker")


def run_worker(
    *,
    worker_name: str,
    interval_key: str,
    run_pass: Callable[[], object],
    once: bool = False,
    sleeper: Callable[[float], None] | None = None,
) -> None:
    """Run `run_pass` on a cadence, beating as `worker_name`. Never raises out.

    Provider-agnostic on purpose: what makes this the Proxmox worker or the
    TrueNAS worker is the three arguments — the key it beats under, the settings
    key it reads its cadence from, and the pass it runs — never a shared
    `PROVIDERS` iteration. A bad pass costs one cycle, never the worker: the beat
    still happens (so the deadman does not mistake a failing poll for a dead
    process), and the interval carried on the beat is the one this pass ran under.
    """
    stopping = _Stopping()
    started_at = timezone.now()
    sleeper = sleeper or stopping.wait
    logger.info("%s started", worker_name)

    while not stopping.is_set():
        interval_s = _interval(interval_key)
        try:
            run_pass()
        except Exception:  # noqa: BLE001 - a bad pass must never kill the worker
            logger.exception("%s pass failed", worker_name)
        # After the work, not before: a beat written ahead of a pass that then
        # hangs would report a worker that is not working.
        _beat(worker_name, interval_s, started_at)
        if once:
            return
        sleeper(interval_s)
    logger.info("%s stopped", worker_name)


@releases_connections
def _interval(interval_key: str) -> int:
    return int(get_setting(interval_key))


@releases_connections
def _beat(worker_name: str, interval_s: int, started_at):
    return beat(worker_name, interval_s=interval_s, started_at=started_at)


class _Stopping:
    """SIGINT/SIGTERM into a flag, with a sleep that wakes early on a signal."""

    def __init__(self):
        self._stop = False
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def _handle(self, *_):
        self._stop = True

    def is_set(self) -> bool:
        return self._stop

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
