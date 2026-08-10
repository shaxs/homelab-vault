"""Working out what a probed page is actually running — spec §7 autofill.

The hard case is an auth-walled app: the probe lands on a login screen whose
title describes the page, not the service. Sonarr and Radarr both serve
"Login - <app>" from the *same host*, so a naive derivation gives both the slug
`login` and the second import silently overwrites the first.
"""

import pytest

from services.api import _identity, _looks_generic, _primary_title, _suggest_slug
from services.probe import ProbeResult, _extract_meta


def result(**kwargs):
    base = {"ok": True, "url": "http://h/", "final_url": "http://h/", "status": 200}
    return ProbeResult(**{**base, **kwargs})


class TestPrimaryTitle:
    @pytest.mark.parametrize(
        "title,expected",
        [
            # The real cases, taken from the running services.
            ("Login - Sonarr", "Sonarr"),
            ("Login - Radarr", "Radarr"),
            ("Sign in to continue | Jellyfin", "Jellyfin"),
            ("Dashboard | Grafana", "Grafana"),
            # And the ones where the app comes first.
            ("OmniRoute — AI Gateway for Multi-Provider LLMs", "OmniRoute"),
            ("Grafana | Dashboards", "Grafana"),
            ("Jellyfin: Media System", "Jellyfin"),
        ],
    )
    def test_drops_the_segment_that_names_the_page(self, title, expected):
        assert _primary_title(title) == expected

    def test_keeps_a_compound_name_whole(self):
        assert _primary_title("Kara-Keep") == "Kara-Keep"

    def test_an_entirely_generic_title_is_returned_rather_than_dropped(self):
        # Better a poor name than none; the caller tries another signal.
        assert _primary_title("Login") == "Login"


class TestLooksGeneric:
    @pytest.mark.parametrize(
        "title", ["Login", "login", "Sign in", "Welcome", "Dashboard", "  Home  "]
    )
    def test_page_names_are_generic(self, title):
        assert _looks_generic(title) is True

    @pytest.mark.parametrize("title", ["Sonarr", "OpenObserve", "Portal", "Console"])
    def test_real_service_names_are_not(self, title):
        # "Portal" and "Console" are plausible service names and must survive.
        assert _looks_generic(title) is False


class TestIdentity:
    def test_a_useful_title_wins(self):
        assert _identity(result(title="OpenObserve", site_name="Something")) == (
            "OpenObserve"
        )

    def test_login_title_resolves_through_the_title_segments(self):
        assert _identity(result(title="Login - Sonarr")) == "Sonarr"

    def test_falls_back_to_site_name_when_the_title_says_nothing(self):
        assert _identity(result(title="Login", site_name="Radarr")) == "Radarr"

    def test_falls_back_to_a_short_description(self):
        # Sonarr publishes <meta name="description" content="Sonarr">.
        assert _identity(result(title="Login", description="Sonarr")) == "Sonarr"

    def test_ignores_a_long_marketing_description(self):
        prose = "The best media management solution for your home server, ever."
        assert _identity(result(title="Login", description=prose)) == "Login"

    def test_returns_none_when_the_page_offers_nothing(self):
        assert _identity(result()) is None


class TestSlugSuggestion:
    def test_sonarr_and_radarr_no_longer_collide(self):
        """Both run on the same host, so a hostname fallback would collide too —
        the page metadata is the only thing that separates them."""
        sonarr = _suggest_slug(
            "http://192.168.50.125:8989/", _identity(result(title="Login - Sonarr"))
        )
        radarr = _suggest_slug(
            "http://192.168.50.125:7878/", _identity(result(title="Login - Radarr"))
        )
        assert (sonarr, radarr) == ("sonarr", "radarr")

    def test_a_generic_identity_yields_nothing_for_an_ip_url(self):
        """"Login" is not an identity and neither is an address, so the UI has
        to ask rather than guess."""
        assert _suggest_slug("http://10.0.0.5/", "Login") == ""

    def test_a_generic_identity_still_falls_back_to_a_real_hostname(self):
        assert _suggest_slug("http://immich.lab.local/", "Login") == "immich"


class TestMetaExtraction:
    def test_reads_multiline_meta_tags(self):
        body = b"""
        <html><head>
        <meta name="description" content="Sonarr" />
        <meta
            property="og:site_name"
            content="Sonarr Media Manager"
        />
        </head>"""
        meta = _extract_meta(body, "text/html")
        assert meta["site_name"] == "Sonarr Media Manager"
        assert meta["description"] == "Sonarr"

    def test_prefers_og_site_name_over_og_title(self):
        body = (
            b'<meta property="og:title" content="Page">'
            b'<meta property="og:site_name" content="App">'
        )
        assert _extract_meta(body, "text/html")["site_name"] == "App"

    def test_ignores_non_html(self):
        assert _extract_meta(b'<meta name="description" content="x">', "image/png") == {}

    def test_survives_a_page_with_no_meta(self):
        assert _extract_meta(b"<html><body>hi</body></html>", "text/html") == {}
