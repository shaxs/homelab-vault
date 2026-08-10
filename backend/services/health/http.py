"""HTTP health checker — spec §8.

"Failure = connection refused, DNS failure, or timeout. Any HTTP response counts
as success unless `health_expect_status` is set explicitly."

This matters: auth-protected services return 401/403 and are alive. Treating
non-2xx as down would paint half the catalog red permanently.
"""

import asyncio
import logging

import httpx
from django.conf import settings

from ..probe import ProbeRejected, validate_url
from .registry import register

logger = logging.getLogger(__name__)


@register("http")
async def check_http(service) -> bool:
    url = service.probe_url
    try:
        # The health URL is catalog data, but an agent-scoped token can write it.
        # Without this, a crafted health_url turns the published health status
        # into an internal port-scan oracle.
        #
        # Off the event loop (audit S2): validate_url resolves DNS with a
        # blocking getaddrinfo, and one unresolvable hostname would otherwise
        # hold the loop for the resolver's timeout — serialising every
        # concurrent check, pushing the pass past 3× interval, and firing the
        # deadman. The request-path callers keep the synchronous version; they
        # want the answer before returning.
        await asyncio.to_thread(validate_url, url)
    except ProbeRejected as exc:
        logger.warning("Health check for %s rejected: %s", service.slug, exc)
        return False

    timeout = httpx.Timeout(service.clamped_timeout_ms / 1000)
    try:
        # Redirects are NOT followed. httpx would follow them without running
        # the SSRF policy on each hop, so a service could bounce the worker to
        # a denied address. Not following is also closer to §8: "any HTTP
        # response counts as success" — and a 302 is an HTTP response.
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=timeout, verify=settings.PROBE_VERIFY_TLS
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return False

    if service.health_expect_status is not None:
        return response.status_code == service.health_expect_status
    return True
