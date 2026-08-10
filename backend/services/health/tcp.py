"""TCP health checker — spec §8.

"`tcp` is a few lines on top of the same registry and worth including if it
costs under an hour." Selected by a `tcp://host:port` health_url.
"""

import asyncio
import logging
from urllib.parse import urlsplit

from ..probe import ProbeRejected, address_allowed, resolve_host
from .registry import register

logger = logging.getLogger(__name__)


@register("tcp")
async def check_tcp(service) -> bool:
    parts = urlsplit(service.probe_url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        # `.port` raises on `tcp://host:99999`, and health_url is agent-
        # writable — a malformed port must be a failed check, never an
        # exception out of the checker (audit S9's class).
        logger.warning("tcp health_url for %s has an invalid port", service.slug)
        return False
    if not host or not port:
        logger.warning("tcp health_url for %s needs host and port", service.slug)
        return False

    try:
        # Off the event loop (audit S2) — same reasoning as check_http: a
        # blocking getaddrinfo on the loop serialises the whole pass.
        addresses = await asyncio.to_thread(resolve_host, host)
    except ProbeRejected:
        return False
    if not all(address_allowed(address) for address in addresses):
        logger.warning("Health check for %s rejected: address not permitted", service.slug)
        return False

    timeout = service.clamped_timeout_ms / 1000
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
