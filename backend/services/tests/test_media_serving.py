"""Cached icons must be served with DEBUG=False.

The original route sat inside `if settings.DEBUG:` — the idiom Django's own
docs suggest — so every icon 404'd in production while working perfectly in
development. Icons are written at runtime, so they cannot be collected by
collectstatic and served by whitenoise like the rest of the frontend.
"""

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def cached_icon(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    icons = tmp_path / "icons"
    icons.mkdir()
    icon = icons / "abc123.png"
    icon.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return icon


def test_icons_are_served_when_debug_is_off(client, settings, cached_icon):
    settings.DEBUG = False
    response = client.get("/media/icons/abc123.png")

    assert response.status_code == 200
    assert b"\x89PNG" in b"".join(response.streaming_content)


def test_icons_are_served_when_debug_is_on(client, settings, cached_icon):
    settings.DEBUG = True
    assert client.get("/media/icons/abc123.png").status_code == 200


def test_a_missing_icon_is_404_not_500(client, settings, cached_icon):
    settings.DEBUG = False
    assert client.get("/media/icons/nope.png").status_code == 404


def test_media_route_does_not_escape_media_root(client, settings, cached_icon):
    """django.views.static.serve uses safe_join, so a traversal attempt must not
    reach outside MEDIA_ROOT."""
    settings.DEBUG = False
    response = client.get("/media/icons/../../../etc/passwd")
    assert response.status_code in (400, 404)


def test_the_spa_still_resolves(client):
    """The media pattern is greedy; it must not swallow the SPA route."""
    assert client.get("/").status_code == 200


def test_the_api_still_resolves(client):
    assert client.get("/api/v1/services").status_code == 200
