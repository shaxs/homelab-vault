"""SSRF policy — spec §4c.

The probe fetches arbitrary user-supplied URLs from inside the network. These are
diagnostic tests for the address policy itself; they never touch the network
(DNS resolution is stubbed).
"""

import ipaddress

import pytest

from services import probe as probe_module
from services.probe import ProbeRejected, address_allowed, same_origin, validate_url


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin DNS so the policy, not the resolver, is under test."""

    def _install(address):
        monkeypatch.setattr(probe_module, "resolve_host", lambda host: [address])

    return _install


# --- Scheme allowlist -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://internal.lab.local/",
        "ftp://internal.lab.local/",
        "//no-scheme.lab.local/",
        "javascript:alert(1)",
    ],
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(ProbeRejected, match="not allowed|no host"):
        validate_url(url)


def test_a_port_out_of_range_is_rejected_not_a_crash():
    """`urlsplit(...).port` raises ValueError on `:99999`, and at fetch time it
    surfaces as httpx.InvalidURL — which is NOT an HTTPError (the V1.3
    finding), so it escaped every handler on the probe path (audit S9).
    Refused here, where every fetched URL already passes."""
    with pytest.raises(ProbeRejected, match="[Pp]ort"):
        validate_url("http://192.168.0.10:99999/")


def test_probing_a_url_with_a_bad_port_reports_an_error_not_a_500():
    result = probe_module.probe_url("http://192.168.0.10:99999/")
    assert result.ok is False
    assert "port" in (result.error or "").lower()


# --- Hard denies ----------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.99.1.2",
        "0.0.0.0",
        "169.254.1.1",
        "169.254.169.254",  # cloud metadata
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback must not slip through
        "fd00:ec2::254",
    ],
)
def test_hard_denied_addresses(address):
    assert address_allowed(address) is False


def test_metadata_address_denied_even_if_allowlisted(settings):
    """Explicitly allowlisting the metadata range must not re-enable it — the
    hard-deny stage runs first."""
    settings.PROBE_ALLOWED_NETWORKS = [ipaddress.ip_network("169.254.0.0/16")]
    assert address_allowed("169.254.169.254") is False


# --- Allowed-network policy ----------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "172.16.4.4", "192.168.50.101", "100.64.1.9"],
)
def test_default_allowlist_permits_lab_ranges(address):
    """RFC1918 is permitted because this is a homelab (§4c)."""
    assert address_allowed(address) is True


def test_cgnat_is_allowed_only_because_the_range_is_listed(settings):
    """The Tailscale case (§5, line 273).

    100.64.0.0/10 reports is_private=False AND is_global=False on modern
    CPython, so it belongs to neither bucket. This pair proves the allowlist is
    actually consulted rather than the address being swept into a blanket deny:
    with the range listed it passes, and with the range removed the *same*
    address is refused.
    """
    tailscale = "100.101.102.103"
    assert address_allowed(tailscale) is True

    settings.PROBE_ALLOWED_NETWORKS = [
        ipaddress.ip_network(n)
        for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    assert address_allowed(tailscale) is False


def test_private_address_outside_allowlist_is_denied(settings):
    settings.PROBE_ALLOWED_NETWORKS = [ipaddress.ip_network("192.168.0.0/16")]
    assert address_allowed("10.0.0.5") is False
    assert address_allowed("192.168.1.1") is True


def test_public_addresses_are_allowed():
    """A homelab service can legitimately sit on a public address."""
    assert address_allowed("8.8.8.8") is True


def test_validate_url_rejects_denied_resolution(resolves_to):
    resolves_to("127.0.0.1")
    with pytest.raises(ProbeRejected, match="not permitted"):
        validate_url("http://sneaky.lab.local/")


def test_validate_url_accepts_allowed_resolution(resolves_to):
    resolves_to("192.168.50.101")
    assert validate_url("http://nas.lan/") == "http://nas.lan/"


def test_all_resolved_addresses_must_pass(monkeypatch):
    """A host resolving to both a permitted and a denied address is denied —
    otherwise a round-robin record defeats the policy."""
    monkeypatch.setattr(
        probe_module, "resolve_host", lambda host: ["192.168.1.1", "127.0.0.1"]
    )
    with pytest.raises(ProbeRejected):
        validate_url("http://dual.lab.local/")


def test_unresolvable_host_is_rejected(monkeypatch):
    def boom(host):
        raise ProbeRejected(f"Could not resolve host: {host}")

    monkeypatch.setattr(probe_module, "resolve_host", boom)
    with pytest.raises(ProbeRejected, match="resolve"):
        validate_url("http://nope.invalid/")


# --- Favicon same-origin rule --------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("https://x.lab.local/favicon.ico", "https://x.lab.local/", True),
        ("https://x.lab.local:8443/i.png", "https://x.lab.local/", False),
        ("http://x.lab.local/i.png", "https://x.lab.local/", False),
        ("https://evil.example/i.png", "https://x.lab.local/", False),
    ],
)
def test_same_origin(a, b, expected):
    assert same_origin(a, b) is expected


def test_cross_origin_favicon_is_skipped(monkeypatch, settings, tmp_path):
    """A <link rel=icon> can point anywhere; v0 fetches favicons only from the
    same origin as the probed page (§4c)."""
    settings.MEDIA_ROOT = str(tmp_path)
    body = b'<html><head><link rel="icon" href="https://evil.example/i.png"></head>'

    def explode(*args, **kwargs):
        raise AssertionError("cross-origin favicon must not be fetched")

    monkeypatch.setattr(probe_module, "_store_icon", explode)

    class FakeClient:
        def stream(self, *args, **kwargs):
            raise AssertionError("cross-origin favicon must not be fetched")

    assert probe_module._fetch_icon(FakeClient(), "https://x.lab.local/", body) is None


def test_redirect_cap_is_configured_low(settings):
    """Spec §4c caps redirects at 3."""
    assert settings.PROBE_MAX_REDIRECTS == 3
