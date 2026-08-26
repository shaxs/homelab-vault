"""Every route the frontend owns is served by Django too — v1 §8.

A bookmark is a **cold** load: the browser asks the server for `/catalog` and
only then does any JavaScript run. A frontend router alone gives you a URL that
works until somebody presses reload, which is the exact bug `BACKLOG.3` recorded
against the settings screen and the service record.

The list here and the one in `frontend/src/router.js` have to move together.
`test_the_spa_route_list_matches_the_frontend_router` reads the JavaScript and
fails if they drift, because two lists that must agree and are never compared
are one list that is wrong.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import get_resolver

from services.views import spa_index

pytestmark = pytest.mark.django_db

#: Bookmarkable app routes. `/services/{slug}`, `/dashboard/{adapter}` (TN4)
#: and `/settings/{section}` (TN-SET) are the parameterised ones.
SPA_PATHS = [
    "/",
    "/catalog",
    "/dashboard",
    "/dashboard/truenas",
    "/settings",
    "/settings/notifications",
    "/services/jellyfin",
]


@pytest.mark.parametrize("path", SPA_PATHS)
def test_every_app_route_is_served_on_a_cold_load(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("path", SPA_PATHS)
def test_every_app_route_issues_the_csrf_cookie(client, path):
    """`@ensure_csrf_cookie` on one route is not enough once there are five: a
    session-authenticated write from a page loaded at `/settings` 403s without
    it, which looks exactly like a broken CSRF split (§4c)."""
    response = client.get(path)

    assert "csrftoken" in response.cookies


def test_an_unknown_path_is_a_404_not_the_app(client):
    """Enumerated routes, not a catch-all.

    A catch-all answers every unknown path with 200 and an HTML page, including
    a mistyped asset or an API path that lost its prefix — turning a clear 404
    into a silent one. That is the class of bug V1.1 spent a chunk on.
    """
    assert client.get("/not-a-page").status_code == 404
    assert client.get("/catalog/extra").status_code == 404


def test_the_api_is_still_the_api(client):
    """The SPA routes must not shadow `/api/v1/` or the unprefixed redirect."""
    assert client.get("/api/v1/services").status_code == 200
    assert client.get("/api/services").status_code == 307


def test_a_slug_with_a_dot_or_a_hyphen_still_resolves(client):
    """Slugs may contain hyphens (AS_BUILT §3 row 1) and a service named for a
    hostname can contain a dot. Both are real catalog slugs, so both are real
    URLs."""
    assert client.get("/services/kara-keep").status_code == 200
    assert client.get("/services/pve.lab.local").status_code == 200


def test_the_spa_route_list_matches_the_frontend_router():
    """The two lists are compared rather than trusted.

    Adding a screen means adding it in `config/urls.py` and in `router.js`; miss
    either and the route works in the app and 404s on reload, which is only ever
    found by hand.

    Audit T4 rewrote both sides of the comparison. The old regex
    (`r"'(/[a-z]*)':\\s*\\{\\s*name:"`) could not match `/service-map` or `/v2`,
    so such a route silently vanished from the comparison and the test passed —
    re-opening `BACKLOG.3`, the exact bug it exists to prevent. And `served`
    was rebuilt from this file's own `SPA_PATHS`, a third hand-maintained copy
    that could drift in step with the other two. Now: `declared` matches ANY
    quoted path key (`[^']*`, not `[^']+` — the bare `'/'` key has nothing
    after the slash and `+` would drop the root route from the comparison),
    and `served` comes from the resolver Django actually routes with.
    """
    router = (
        Path(settings.BASE_DIR).parent / "frontend" / "src" / "router.js"
    ).read_text(encoding="utf-8")
    declared = set(re.findall(r"'(/[^']*)':", router))

    served = set()
    regex_routes = []
    for pattern in get_resolver().url_patterns:
        if getattr(pattern, "callback", None) is not spa_index:
            continue
        route = str(pattern.pattern)
        # RegexPattern stringifies to its raw regex; those are the parameterised
        # routes the frontend owns with RECORD_RE / DASHBOARD_RE, not keys in
        # STATIC_ROUTES — asserted separately below.
        if route.startswith("^"):
            regex_routes.append(route)
            continue
        served.add("/" + route)

    assert declared == served

    # The parameterised routes exist on both sides too: three regex routes serve
    # the SPA — record, adapter (TN4) and settings section (TN-SET) — and the
    # frontend builds and parses the same shapes (`RECORD_RE`, `DASHBOARD_RE`,
    # `SETTINGS_RE`).
    joined = " ".join(regex_routes)
    assert len(regex_routes) == 3
    assert "services/" in joined
    assert "dashboard/" in joined
    assert "settings/" in joined
    assert "/services/" in router
    assert "/dashboard/" in router
    assert "/settings/" in router
