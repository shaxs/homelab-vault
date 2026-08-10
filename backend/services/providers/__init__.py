"""Location providers — v1 §7a.

The seam is defined now with one implementation. A provider enumerates guests
with their addresses and a stable foreign ID, returns a location string, and
declares whether it is configured. Adding Docker, TrueNAS or Unifi later is a
new module in this package and **zero changes to the enrichment path**.

That is extensibility at the data layer; it does not imply runtime-defined
providers. Adding a provider is adding code, which is normal for self-hosted
software.
"""

from .base import (
    Guest,
    Infrastructure,
    Node,
    Provider,
    ProviderStatus,
    ProviderUnavailable,
)
from .proxmox import ProxmoxProvider

#: Every provider the build knows about. Enrichment iterates this, so a new
#: module lands by being added here and nowhere else.
PROVIDERS: tuple[Provider, ...] = (ProxmoxProvider(),)


def configured_providers() -> list[Provider]:
    """Those with credentials. An unconfigured provider is not an error (§7d)."""
    return [p for p in PROVIDERS if p.is_configured()]


__all__ = [
    "Guest",
    "Infrastructure",
    "Node",
    "Provider",
    "ProviderStatus",
    "ProviderUnavailable",
    "ProxmoxProvider",
    "PROVIDERS",
    "configured_providers",
]
