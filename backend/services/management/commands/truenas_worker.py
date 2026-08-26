"""`python manage.py truenas_worker` — TN2 (§7c, §6a, §5a).

**A separate process from `proxmox_worker`, scoped to `"truenas"` alone.** This is
the concrete reason the loop/beat plumbing moved to `providers/worker.py`: a
single worker looping `PROVIDERS` would poll TrueNAS under Proxmox's
`polling.enabled`/`poll_interval_s` and beat it as `"proxmox"`, so TrueNAS would
have no heartbeat of its own and would stop being polled whenever Proxmox polling
was switched off. Independent death and independent cadence (§7c/§5a) require an
independent worker. Compose gains a fifth service.

**Its own settings pair** (§6a): `truenas.polling.enabled` gates the poll,
`truenas.poll_interval_s` sets the cadence — both read live, per cycle, never
snapshotted at startup.

**No enrichment.** TrueNAS is a storage provider: `guests()` is empty and there
are no `service_links` to refresh. The pass is a poll and nothing more; the poll
itself (`poll_provider`) caches the `report()` payload and forwards any newly-
firing native alerts to Discord.

A disabled or unconfigured provider **still beats** — "polling is off" is a
configuration state, not a stalled process, and the deadman must not fire for a
worker that is running perfectly.
"""

import logging

from django.core.management.base import BaseCommand

from services.db import releases_connections
from services.heartbeat import TRUENAS_WORKER
from services.providers import provider_for
from services.providers.poll import poll_provider
from services.providers.worker import run_worker
from services.settings_store import get_setting

logger = logging.getLogger("services.truenas_worker")


class Command(BaseCommand):
    help = "Poll TrueNAS for pool, disk, system and alert state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Run one pass and exit (for tests)."
        )

    def handle(self, *args, **options):
        run_worker(
            worker_name=TRUENAS_WORKER,
            interval_key="truenas.poll_interval_s",
            run_pass=run_pass,
            once=options["once"],
        )


@releases_connections
def run_pass() -> list[dict]:
    """One poll of the TrueNAS provider. Returns a one-item summary list.

    Scoped to `"truenas"` via `provider_for`, and gated on *its own*
    `truenas.polling.enabled` — never the Proxmox switch. No `refresh_links`:
    there are no guests to link.
    """
    if not get_setting("truenas.polling.enabled"):
        logger.debug("truenas polling disabled in settings")
        return []

    provider = provider_for(TRUENAS_WORKER)
    if provider is None or not provider.is_configured():
        return []

    snapshot = poll_provider(provider)
    if not snapshot.ok:
        logger.warning("provider %s unusable: %s", provider.name, snapshot.detail)
    else:
        logger.info("provider %s report cached", provider.name)
    return [{"provider": provider.name, "ok": snapshot.ok}]
