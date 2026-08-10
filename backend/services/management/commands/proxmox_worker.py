"""`python manage.py proxmox_worker` — spec §7c.

**Its own process, not a second loop inside `health_worker`** (§7c). Proxmox
being slow must never delay health checks, and the two must be able to die
independently — which the deadman relies on (§5a). Compose gains a fourth
service.

**This is V1.3's consumer** (standing rule 2). Registering against the heartbeat
contract is one call — `beat("proxmox", interval_s=...)` — and changes nothing
in the deadman: it reads rows keyed by worker name and multiplies whatever
interval the worker last declared by three, so it never needs to know which
settings key drives which worker. The interval is re-read from the settings
table every cycle, so the stall threshold follows a settings change with nothing
restarted.

**Two settings values, and this worker is what proves the wire carries** (§6a):
`proxmox.polling.enabled` gates the poll, `proxmox.poll_interval_s` sets the
cadence. Both are read live, per cycle, never snapshotted at startup — a value
captured above the loop satisfies "the worker reads the setting" and is still
wrong.

A disabled or unconfigured provider **still beats.** The worker is running and
the deadman should not think otherwise; "polling is off" is a configuration
state, not a stalled process.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from services import enrichment
from services.db import releases_connections
from services.heartbeat import PROXMOX_WORKER, beat
from services.providers import PROVIDERS
from services.providers.poll import poll_provider
from services.settings_store import get_setting

logger = logging.getLogger("services.proxmox_worker")


class Command(BaseCommand):
    help = "Poll configured location providers and refresh service links."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Run one pass and exit (for tests)."
        )

    def handle(self, *args, **options):
        _run(once=options["once"])


def _run(once: bool = False, sleeper=None):
    stopping = _Stopping()
    started_at = timezone.now()
    sleeper = sleeper or stopping.wait
    logger.info("proxmox_worker started")

    while not stopping.is_set():
        interval_s = _interval()
        try:
            run_pass()
        except Exception:  # noqa: BLE001 - a bad pass must never kill the worker
            logger.exception("proxmox pass failed")
        # After the work, not before: a beat written ahead of a pass that then
        # hangs would report a worker that is not working. The interval carried
        # here is the one this pass actually ran under.
        _beat(interval_s, started_at)
        if once:
            return
        sleeper(interval_s)
    logger.info("proxmox_worker stopped")


@releases_connections
def run_pass() -> list[dict]:
    """One poll-and-refresh across every provider. Returns per-provider summaries."""
    if not get_setting("proxmox.polling.enabled"):
        # Read live, every pass. Switching polling on takes effect on the next
        # cycle with nothing restarted.
        logger.debug("proxmox polling disabled in settings")
        return []

    summaries = []
    for provider in PROVIDERS:
        if not provider.is_configured():
            continue
        snapshot = poll_provider(provider)
        summary = enrichment.refresh_links(provider.name)
        summary["ok"] = snapshot.ok
        if not snapshot.ok:
            logger.warning("provider %s unusable: %s", provider.name, snapshot.detail)
        else:
            logger.info(
                "provider %s: %s linked (%s new, %s refreshed)",
                provider.name,
                summary["linked"],
                summary["matched"],
                summary["refreshed"],
            )
        summaries.append(summary)
    return summaries


@releases_connections
def _interval() -> int:
    return int(get_setting("proxmox.poll_interval_s"))


@releases_connections
def _beat(interval_s: int, started_at):
    return beat(PROXMOX_WORKER, interval_s=interval_s, started_at=started_at)


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
