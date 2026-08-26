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
    ProviderAlert,
    ProviderStatus,
    ProviderUnavailable,
)
from .proxmox import ProxmoxProvider
from .truenas import TrueNASProvider

#: Every provider the build knows about. Enrichment iterates this, so a new
#: module lands by being added here and nowhere else. **Each provider worker
#: still targets exactly one provider by name** — see the worker note in
#: `management/commands`; this tuple is the registry, not a "poll everything"
#: instruction.
PROVIDERS: tuple[Provider, ...] = (ProxmoxProvider(), TrueNASProvider())


def configured_providers() -> list[Provider]:
    """Those with credentials. An unconfigured provider is not an error (§7d)."""
    return [p for p in PROVIDERS if p.is_configured()]


def provider_for(name: str) -> Provider | None:
    """The single provider a worker is scoped to, by its stable `name`.

    This is what keeps each worker targeting **one** provider (see
    `providers/worker.py`): a worker names its provider and polls only that,
    instead of looping the registry and beating another provider's poll into its
    own heartbeat.
    """
    return next((p for p in PROVIDERS if p.name == name), None)


__all__ = [
    "Guest",
    "Infrastructure",
    "Node",
    "Provider",
    "ProviderAlert",
    "ProviderStatus",
    "ProviderUnavailable",
    "ProxmoxProvider",
    "TrueNASProvider",
    "PROVIDERS",
    "configured_providers",
    "provider_for",
]
