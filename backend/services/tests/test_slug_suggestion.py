"""Slug suggested by the probe for the Add form — spec §7 ("autofill slug")."""

import json

import pytest

from services.api import _primary_title, _suggest_slug
from services.models import Service
from services.probe import ProbeResult

pytestmark = pytest.mark.django_db


def test_hostname_wins_over_the_page_title():
    """A hostname is a stable identity; a page title is editable content."""
    assert _suggest_slug("https://karakeep.lab.local", "Karakeep · Bookmarks") == "karakeep"


def test_ip_addressed_service_falls_back_to_the_title():
    """A bare address is a poor permanent key: the slug is immutable, so an
    IP-derived one encodes where the service lived on the day it was added."""
    assert _suggest_slug("http://192.168.50.17:5080/", "OpenObserve") == "openobserve"


def test_ip_without_an_identity_yields_nothing():
    """No address ever becomes a slug. Blank makes the UI ask and makes the API
    reject with an actionable 422, rather than silently minting a permanent
    identifier out of a network location."""
    assert _suggest_slug("http://192.168.50.17:5080/", None) == ""
    assert _suggest_slug("http://192.168.50.17:5080/", "") == ""


# Turning a page's metadata into a service identity is tested in
# test_identity.py; `_suggest_slug` receives the RESOLVED identity, so these
# only cover slug shaping.


@pytest.mark.parametrize(
    "identity,expected",
    [
        ("Kara-Keep", "kara-keep"),
        ("open-webui", "open-webui"),
        ("e-paper dashboard", "e-paper-dashboard"),
    ],
)
def test_compound_names_keep_their_hyphen(identity, expected):
    assert _suggest_slug("http://10.0.0.5/", identity) == expected


def test_a_long_single_segment_title_is_bounded_at_a_word_boundary():
    slug = _suggest_slug("http://10.0.0.5/", "Proxmox Virtual Environment Cluster Manager")
    assert len(slug) <= 32
    assert not slug.endswith("-")
    assert slug.startswith("proxmox-virtual")


def test_a_pathological_title_still_yields_something_usable():
    slug = _suggest_slug("http://10.0.0.5/", "A" * 200)
    assert 0 < len(slug) <= 32


def test_a_title_that_normalises_to_nothing_yields_nothing():
    assert _suggest_slug("http://10.0.0.5/", "!!!") == ""


class TestPrimaryTitleEdgeCases:
    """Title splitting proper lives in test_identity.py; these are the shapes
    that must not crash or empty out."""

    def test_collapses_whitespace(self):
        assert _primary_title("  Grafana\n\tDashboards ") == "Grafana Dashboards"

    def test_handles_nothing(self):
        assert _primary_title(None) is None
        assert _primary_title("   ") is None

    def test_never_returns_an_empty_string_for_a_pathological_title(self):
        for junk in ("- - -", "|", ":::", "—"):
            assert _primary_title(junk)

    def test_a_tight_colon_is_not_a_separator(self):
        """Otherwise "Camera 16:9" and "Backup 12:30" lose their second half."""
        assert _primary_title("Camera 16:9") == "Camera 16:9"


def test_suggestion_never_leaks_into_server_side_derivation(client, agent_headers, monkeypatch):
    """The server must stay deterministic. If it consulted the title, the same
    URL would key differently depending on whether the service happened to be
    up — the exact duplicate the upsert exists to prevent.

    So an IP-only payload is refused outright, even when the probe found a
    perfectly good name.
    """
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: ProbeResult(
            ok=True, url=url, final_url=url, status=200, title="OpenObserve"
        ),
    )
    response = client.post(
        "/api/v1/services",
        data=json.dumps({"launch_url": "http://192.168.50.17:5080/"}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "slug" in response.content.decode()
    assert not Service.objects.exists()


def test_an_ip_payload_with_an_explicit_slug_succeeds(client, agent_headers):
    """The actionable half of that 422 — this is what the error tells callers
    to do, so it had better work."""
    response = client.post(
        "/api/v1/services",
        data=json.dumps(
            {"launch_url": "http://192.168.50.89:2283/", "slug": "immich"}
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 201
    service = Service.objects.get()
    assert service.slug == "immich"
    # Name falls back to the slug, never to the address.
    assert service.name == "immich"


def test_the_ui_can_pin_a_better_slug_at_creation(client, agent_headers):
    """What the Add form actually does: show the suggestion, let the human
    confirm it, and send it explicitly."""
    response = client.post(
        "/api/v1/services",
        data=json.dumps(
            {"launch_url": "http://192.168.50.17:5080/", "slug": "openobserve"}
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 201
    assert response.json()["slug"] == "openobserve"


def test_a_pinned_slug_is_normalised_the_same_way(client, agent_headers):
    """A slug typed into the form goes through the same derivation as one from
    an agent, so the UI and API cannot diverge (§4c)."""
    response = client.post(
        "/api/v1/services",
        data=json.dumps(
            {"launch_url": "http://192.168.50.17:5080/", "slug": "Open Observe!"}
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.json()["slug"] == "open-observe"
