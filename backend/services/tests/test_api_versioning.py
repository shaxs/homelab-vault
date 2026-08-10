"""The `/api/v1/` prefix and the compatibility redirect — spec §3.

The prefix is *reserved, not operated*: one NinjaAPI instance, no negotiation,
no dual maintenance. So what is actually worth testing is not "v1 answers" —
that is one line — but the redirect, which is the only new behaviour and the
only place this change can go wrong. Two ways it can:

1. **Silently downgrading a write.** A 301/302 permits a client to convert POST
   to GET and drop the body. A forgotten caller's create would come back 200
   with nothing written, which looks like success. `test_unprefixed_post_*` is
   the whole reason the code says 307.
2. **Becoming an open redirect.** A redirect built from request input is the
   classic shape. Ours is a path rewrite onto a hardcoded prefix, and the tests
   below assert that invariant against the inputs that would break a naive one.
"""

import json
from urllib.parse import urlparse

import pytest

from services.models import Service

from .conftest import ADMIN_TOKEN

pytestmark = pytest.mark.django_db


def body(payload):
    return {"data": json.dumps(payload), "content_type": "application/json"}


@pytest.fixture
def service(client, agent_headers):
    client.post(
        "/api/v1/services",
        **body({"launch_url": "https://x.lab.local"}),
        **agent_headers,
    )
    return Service.objects.get(slug="x")


# --- The prefix -----------------------------------------------------------


def test_endpoints_answer_under_the_v1_prefix(client, service):
    assert client.get("/api/v1/services").status_code == 200
    assert client.get("/api/v1/services/x").status_code == 200
    assert client.get("/api/v1/health/x").status_code == 200
    assert client.get("/api/v1/export/json").status_code == 200


def test_schema_and_docs_move_with_it(client):
    """§3: Hermes reads the schema path, so it is part of the contract."""
    assert client.get("/api/v1/openapi.json").status_code == 200
    assert client.get("/api/v1/docs").status_code == 200


# --- The redirect: method and body ----------------------------------------


def test_unprefixed_post_keeps_its_method_and_body(client, agent_headers):
    """The single assertion that justifies 307 over 302.

    Under a 302 the client is entitled to re-issue this as a GET with no body:
    the response would be a 200 catalog listing and no service would be
    created — a write that reports success and does nothing.
    """
    response = client.post(
        "/api/services",
        **body({"launch_url": "https://redirected.lab.local"}),
        follow=True,
        **agent_headers,
    )

    assert response.redirect_chain == [("/api/v1/services", 307)]
    assert response.status_code == 201  # created, not a 200 catalog listing
    assert Service.objects.filter(slug="redirected").exists()


def test_unprefixed_paths_answer_307_and_not_302(client):
    """Pinned deliberately: 301/302 here would still pass a "does it redirect"
    test while breaking every unprefixed write."""
    response = client.get("/api/services")
    assert response.status_code == 307
    assert response["Location"] == "/api/v1/services"


def test_query_string_survives_the_redirect(client):
    """`GET /api/slug-preview?launch_url=…` is the shape that would break
    silently: the endpoint answers 200 with an empty slug when the parameter
    goes missing, so a dropped query string looks like a working redirect.

    Asserted on `Location` and then re-issued by hand rather than with
    `follow=True`. Django's test client only carries the query string across a
    307 for non-GET methods (`Client._follow_redirect`, the
    `request_method not in ("get", "head")` branch) — following here would
    test that quirk instead of this redirect.
    """
    response = client.get("/api/slug-preview?launch_url=https%3A%2F%2Fkara-keep.lab.local")

    assert response.status_code == 307
    assert response["Location"] == (
        "/api/v1/slug-preview?launch_url=https%3A%2F%2Fkara-keep.lab.local"
    )

    followed = client.get(response["Location"])
    assert followed.status_code == 200
    assert followed.json()["slug"] == "kara-keep"


def test_unprefixed_delete_still_deletes(client, admin_headers, service):
    """Method preservation is not POST-only — DELETE must survive too."""
    response = client.delete("/api/services/x", follow=True, **admin_headers)
    assert response.status_code == 200
    assert not Service.objects.filter(slug="x").exists()


# --- The redirect: not an open redirect -----------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api//evil.example/x",
        "/api/%2f%2fevil.example/x",
        "/api/\\\\evil.example/x",
        "/api/https://evil.example/x",
        "/api/../../evil.example",
    ],
)
def test_redirect_target_is_always_a_path_on_this_host(client, path):
    """The invariant: `Location` names no host and no scheme, ever.

    `//host` and `/\\host` are the two shapes a browser parses as an authority
    rather than a path, which is how a "harmless" path rewrite becomes an open
    redirect. Percent-encoding is included because WSGI hands Django an
    already-decoded PATH_INFO, so `%2f%2f` arrives as `//`.
    """
    response = client.get(path)

    assert response.status_code == 307
    location = response["Location"]
    parsed = urlparse(location)
    assert parsed.scheme == ""
    assert parsed.netloc == ""
    assert location.startswith("/api/v1/")


@pytest.mark.parametrize(
    "path",
    ["/api/x%0d%0aX-Injected:%20yes", "/api/x%0aX-Injected:%20yes"],
)
def test_a_newline_in_the_path_cannot_forge_a_response_header(client, path):
    """CRLF in the path must never reach the `Location` header.

    Two layers stop it, and this test exists because the *first* one is
    implicit and a tidy-up could remove it without anyone noticing:

    1. `.` does not match a newline in Python's `re`, so `(?P<rest>.*)$` simply
       fails to match and the request 404s before any redirect is built. That
       safety is a property of the regex, not of anything anyone wrote down —
       adding `re.DOTALL` would silently remove it.
    2. Django's `HttpResponseBase.__setitem__` rejects newlines in a header
       value, so even if layer 1 went away this would be a 500 rather than an
       injected header.

    Asserting *no* Location rather than a 500 pins layer 1 specifically.
    """
    response = client.get(path)

    assert response.status_code == 404
    assert response.get("Location") is None


def test_redirect_ignores_the_host_header(client, settings):
    """A redirect built with `build_absolute_uri` would echo this back."""
    settings.ALLOWED_HOSTS = ["*"]
    response = client.get("/api/services", HTTP_HOST="evil.example")
    assert response["Location"] == "/api/v1/services"


def test_a_v1_path_never_re_enters_the_redirect(client):
    """The loop guard. Without `(?!v1(?:/|$))` a reordering of urlpatterns
    would send /api/v1/x to /api/v1/v1/x and onward forever; an unknown v1
    path must 404 from Ninja instead."""
    assert client.get("/api/v1/no-such-endpoint").status_code == 404


# --- The redirect: not an auth bypass -------------------------------------


def test_bearer_scope_is_unchanged_through_the_redirect(client, agent_headers, service):
    """An agent may not DELETE (§4c). Reaching the handler via the redirect
    must not change that — the concern is a rewrite that drops the
    Authorization header and lands somewhere the scope check reads
    differently."""
    response = client.delete("/api/services/x", follow=True, **agent_headers)
    assert response.status_code == 401
    assert Service.objects.filter(slug="x").exists()


def test_admin_token_survives_the_redirect(client, admin_headers, service):
    """The mirror of the test above: the header is genuinely carried, so the
    401 there is the scope check and not a lost credential."""
    assert client.patch(
        "/api/services/x", **body({"category": "tools"}), follow=True, **admin_headers
    ).status_code == 200


def test_csrf_is_still_enforced_on_the_session_branch_through_the_redirect(service):
    """CsrfViewMiddleware is absent globally, so the session branch of
    services/auth.py is the only thing standing between a cookie-authenticated
    write and CSRF. A redirect that somehow bypassed that branch would be
    invisible in the direct-path tests."""
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.get("/")
    csrf_client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )

    response = csrf_client.patch(
        "/api/services/x", **body({"category": "nope"}), follow=True
    )

    assert response.status_code == 403
    assert Service.objects.get(slug="x").category != "nope"


def test_session_cookie_survives_the_redirect(service):
    """And the mirror again: with a valid CSRF token the same redirected write
    succeeds, so the 403 above is the CSRF check rather than a dropped
    cookie."""
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.get("/")
    token = csrf_client.cookies["csrftoken"].value
    csrf_client.post(
        "/api/v1/session", **body({"token": ADMIN_TOKEN}), HTTP_X_CSRFTOKEN=token
    )

    response = csrf_client.patch(
        "/api/services/x",
        **body({"category": "tools"}),
        follow=True,
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 200
    assert Service.objects.get(slug="x").category == "tools"
