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

**Scoped to one provider by name** (TN2). The loop/signal/beat plumbing moved to
`providers/worker.py`; this command keeps only its own pass, which polls
**`"proxmox"` alone** via `provider_for` rather than iterating `PROVIDERS`. A pass
that looped the registry would poll TrueNAS under Proxmox's settings the moment a
second provider was registered — see `providers/worker.py` for the full note.

A disabled or unconfigured provider **still beats.** The worker is running and
the deadman should not think otherwise; "polling is off" is a configuration
state, not a stalled process.
"""

import logging

from django.core.management.base import BaseCommand

from services import enrichment
from services.db import releases_connections
from services.heartbeat import PROXMOX_WORKER
from services.providers import provider_for
from services.providers.poll import poll_provider
from services.providers.worker import run_worker
from services.settings_store import get_setting

logger = logging.getLogger("services.proxmox_worker")


class Command(BaseCommand):
    help = "Poll the Proxmox cluster and refresh service links."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Run one pass and exit (for tests)."
        )

    def handle(self, *args, **options):
        run_worker(
            worker_name=PROXMOX_WORKER,
            interval_key="proxmox.poll_interval_s",
            run_pass=run_pass,
            once=options["once"],
        )


@releases_connections
def run_pass() -> list[dict]:
    """One poll-and-refresh for the Proxmox provider. Returns a summary list.

    Scoped to `"proxmox"` — not `for provider in PROVIDERS`. The registry now
    carries a second provider (TrueNAS) with its own worker; a pass that looped
    the registry would poll it here, under `proxmox.polling.enabled`, and beat it
    as `"proxmox"`. Enrichment (`refresh_links`) is Proxmox's alone: TrueNAS is a
    storage provider with no guests and no links.
    """
    if not get_setting("proxmox.polling.enabled"):
        # Read live, every pass. Switching polling on takes effect on the next
        # cycle with nothing restarted.
        logger.debug("proxmox polling disabled in settings")
        return []

    provider = provider_for(PROXMOX_WORKER)
    if provider is None or not provider.is_configured():
        return []

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
    return [summary]
