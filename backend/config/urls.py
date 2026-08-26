import re

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import path, re_path
from django.views.static import serve

from services.api import api
from services.views import spa_index

# Cached favicons are written at runtime (§9 caches them locally so the offline
# export can inline them), so they cannot go through collectstatic and whitenoise
# — there is nothing to collect at build time.
#
# This route is deliberately NOT wrapped in `if settings.DEBUG`, which is the
# usual shape. Doing that is why every icon 404'd in production while working
# perfectly in development: DEBUG=False simply removed the route.
#
# `django.views.static.serve` is discouraged at scale, and rightly — it blocks a
# worker thread and does no caching cleverness. At this scale it is the right
# call: a few dozen icons of a few KB each, on a LAN, for one user. Whitenoise
# is not an option here without `autorefresh`, since it indexes its file list at
# startup and would never see an icon cached after boot. Put a reverse proxy in
# front of MEDIA_ROOT if this ever serves a real crowd.
_media_prefix = re.escape(settings.MEDIA_URL.lstrip("/"))


def serve_media(request, path):
    """Read MEDIA_ROOT per request rather than at import time.

    Passing `{"document_root": settings.MEDIA_ROOT}` in the URL pattern freezes
    the value when this module is first imported, which makes the route immune
    to `override_settings` and quietly wrong if the setting is ever computed
    later. Resolving it here costs nothing and keeps it honest.
    """
    return serve(request, path, document_root=settings.MEDIA_ROOT)


class HttpResponseTemporaryRedirect(HttpResponseRedirect):
    """307. Django ships 301 and 302; neither is safe for an API move.

    Subclassed off `HttpResponseRedirect` rather than the base class Django
    keeps private (`HttpResponseRedirectBase` is not exported from
    `django.http`), so this inherits its `Location` handling and scheme
    validation and changes only the status code.

    301/302 permit a client to convert POST to GET and drop the body, so a
    forgotten caller's write would silently become a read that returns 200.
    307 obliges the client to replay the original method and body.

    307 rather than 308 (spec §3 allows either): 308 is permanently cacheable
    with no purge path, so a browser that learned `/api/* -> /api/v1/*` would
    hold it forever. §3 contemplates `/api/v2/` if a consumer we do not control
    ever appears, and a cached 308 would break exactly the machines that had
    used v1. The cost is one extra round trip on a path that exists only for
    bookmarks and forgotten callers.
    """

    status_code = 307


def redirect_to_v1(request, rest):
    """Rewrite an unprefixed `/api/...` path onto `/api/v1/...` (spec §3).

    Deliberately a *path* rewrite and nothing else: the target is built from a
    hardcoded prefix, the host is never read, and no scheme is ever attached,
    so there is no input that turns this into an open redirect. `rest` is
    whatever followed `/api/`; leading slashes and backslashes are stripped
    because `//host` and `/\\host` are the two shapes a browser would parse as
    an authority rather than a path. The invariant the tests assert is that
    `Location` always begins with `/api/v1/`.
    """
    location = "/api/v1/" + rest.lstrip("/\\")
    query = request.META.get("QUERY_STRING", "")
    if query:
        location = f"{location}?{query}"
    return HttpResponseTemporaryRedirect(location)


urlpatterns = [
    # One NinjaAPI instance. The version prefix is reserved, not operated —
    # no negotiation, no dual maintenance, no deprecation windows (§3): the
    # app and its API ship as one artifact and move together on every pull.
    path("api/v1/", api.urls),
    # `(?!v1(?:/|$))` is a loop guard, not an optimisation. Django resolves in
    # order so a v1 path never reaches here today, but without the lookahead a
    # future reordering would send `/api/v1/x` to `/api/v1/v1/x` and onward
    # forever.
    re_path(r"^api/(?!v1(?:/|$))(?P<rest>.*)$", redirect_to_v1),
    re_path(rf"^{_media_prefix}(?P<path>.*)$", serve_media),
    # --- SPA routes (v1 §8) ------------------------------------------------
    #
    # Every path the frontend router owns has to be served by Django too, or a
    # bookmark to it 404s: the browser asks the server for `/catalog` on a cold
    # load, and only then does any JavaScript run.
    #
    # **Enumerated rather than a catch-all**, deliberately. A catch-all answers
    # every unknown path with 200 and an HTML page — including a mistyped asset
    # or an API path that lost its prefix — which turns a clear 404 into a
    # silent one, and would have masked exactly the kind of path bug V1.1 spent
    # a chunk on. An unknown path should 404.
    #
    # **This list and `frontend/src/router.js` must move together.** Adding a
    # screen means adding it in both; `test_spa_routes.py` asserts the pairing.
    path("", spa_index, name="spa-index"),
    path("catalog", spa_index, name="spa-catalog"),
    path("dashboard", spa_index, name="spa-dashboard"),
    # `/dashboard/{adapter}` — the adapter-bar sub-routes (TN4). A parameterised
    # route like the record one below: the frontend owns it with `DASHBOARD_RE`,
    # and a bookmark to `/dashboard/truenas` is a cold server load, so Django has
    # to serve the SPA for it too. `[^/]+` (one segment) keeps `/dashboard/x/y`
    # a 404, matching the router's own resolution.
    re_path(r"^dashboard/(?P<adapter>[^/]+)/?$", spa_index, name="spa-dashboard-adapter"),
    path("settings", spa_index, name="spa-settings"),
    # `/settings/{section}` — the sectioned settings screen (TN-SET), same shape
    # as the adapter route above: a bookmark is a cold server load, and the
    # frontend owns the parameter with `SETTINGS_RE`. One segment only.
    re_path(r"^settings/(?P<section>[^/]+)/?$", spa_index, name="spa-settings-section"),
    # The slug pattern matches the API's, so a service reachable at
    # `/api/v1/services/{slug}` is reachable at `/services/{slug}`.
    re_path(r"^services/(?P<slug>[^/]+)/?$", spa_index, name="spa-record"),
]
