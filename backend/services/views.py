"""SPA entry point.

`ensure_csrf_cookie` matters more than it looks: the SPA can only send the
`X-CSRFToken` header on a cookie-authenticated write if it was issued the CSRF
cookie first. Without this decorator every session-auth write 403s, which looks
exactly like a broken CSRF split (§4c) when it is really a missing cookie.
"""

from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie

_PLACEHOLDER = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Homelab Vault</title></head>
<body style="font:14px system-ui;margin:40px">
<h1>Homelab Vault</h1>
<p>The frontend has not been built yet. Run:</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>The API is live at <a href="/api/v1/docs">/api/v1/docs</a>.</p>
</body></html>
"""


@ensure_csrf_cookie
def spa_index(request, **_route_params):
    """Serve the SPA shell for every route the frontend owns (v1 §8).

    `**_route_params` swallows captured groups — `/services/{slug}` passes one —
    because the server does no routing beyond "this path belongs to the app".
    Resolving the slug here would mean rendering it into the page, and a
    service name can originate from a probed third-party title.
    """
    index = Path(settings.BASE_DIR).parent / "frontend" / "dist" / "index.html"
    if index.is_file():
        return HttpResponse(index.read_text(encoding="utf-8"))
    return HttpResponse(_PLACEHOLDER)
