"""Write-time scheme validation on the URL columns — the pre-release breaking
change (operator ruling 2026-08-10).

A URL column is something a browser eventually navigates, and
`<a href="javascript:…">` executes with this app's CSRF token readable by
design. Every sink already refuses non-http(s) values (`safeUrl`, the recovery
export, the Discord embed); these tests pin the write path joining them, so
the database cannot hold a value a sink would have to refuse — and no future
renderer can be wrong about it.

The migration half: rows written before the rule are *repaired*, not
grandfathered — scheme-less values were already broken (a digit-led
`IP:port` parses as a relative path; a `host.name:port` parses its host as
the URL scheme), and a grandfathered row would 422 on unrelated edits the
moment `clean()` learned the rule.
"""

import importlib
import json
from types import SimpleNamespace

import pytest
from django.apps import apps as live_apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from services.models import Service

repair = importlib.import_module(
    "services.migrations.0007_url_scheme_repair"
).repair

pytestmark = pytest.mark.django_db


def post(client, payload, **extra):
    return client.post(
        "/api/v1/services",
        data=json.dumps(payload),
        content_type="application/json",
        **extra,
    )


def patch(client, slug, payload, **extra):
    return client.patch(
        f"/api/v1/services/{slug}",
        data=json.dumps(payload),
        content_type="application/json",
        **extra,
    )


def make(slug="svc", **kwargs):
    kwargs.setdefault("launch_url", f"https://{slug}.lab.local")
    kwargs.setdefault("name", slug)
    return Service.objects.create(slug=slug, **kwargs)


def named_fields(response):
    """The field names a 422 blames, across both detail shapes the API uses:
    `on_model_validation_error` emits a pydantic-style list of {loc, msg};
    `HttpError` paths emit a plain string."""
    detail = response.json()["detail"]
    if isinstance(detail, str):
        return detail
    return {entry["loc"][-1] for entry in detail}


# --- launch_url: the write path refuses what the sinks refuse ---------------


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>x</script>",
        "ftp://192.168.0.10/",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "192.168.0.10:8096",        # digit-led: parses as a relative path
        "jellyfin.lab.local:8096",  # host-led: parses the host as the SCHEME
        "//192.168.0.10:8096",      # protocol-relative: no scheme of its own
        "http://",                  # scheme with no host
    ],
)
def test_a_launch_url_without_an_http_scheme_is_refused_by_name(
    client, agent_headers, value
):
    response = post(client, {"launch_url": value, "slug": "svc"}, **agent_headers)
    assert response.status_code == 422, response.content
    assert "launch_url" in named_fields(response)
    assert not Service.objects.exists()


@pytest.mark.parametrize(
    "value",
    [
        "http://192.168.0.10:8096",
        "https://jellyfin.lab.local:8096/web",
        "HTTP://a.lab.local",  # scheme case is not the caller's problem
    ],
)
def test_a_full_http_url_is_accepted(client, agent_headers, value):
    response = post(client, {"launch_url": value, "slug": "svc"}, **agent_headers)
    assert response.status_code == 201, response.content


def test_a_patch_cannot_sneak_a_bad_scheme_onto_a_good_row(client, agent_headers):
    make(slug="jellyfin")
    response = patch(
        client, "jellyfin", {"launch_url": "jellyfin.lab.local:8096"}, **agent_headers
    )
    assert response.status_code == 422
    assert "launch_url" in named_fields(response)


# --- The sibling browser-target columns carry the same rule ----------------
# repo_url and docs_url have no renderer *today* — which is exactly why they
# are covered: the next renderer inherits a safe column instead of a trap.


@pytest.mark.parametrize("field", ["launch_url_external", "repo_url", "docs_url"])
def test_the_optional_url_columns_refuse_bad_schemes_by_name(
    client, agent_headers, field
):
    response = post(
        client,
        {"launch_url": "https://a.lab.local", field: "javascript:alert(1)"},
        **agent_headers,
    )
    assert response.status_code == 422
    assert field in named_fields(response)


@pytest.mark.parametrize("field", ["launch_url_external", "repo_url", "docs_url"])
def test_the_optional_url_columns_accept_https(client, agent_headers, field):
    response = post(
        client,
        {"launch_url": "https://a.lab.local", field: "https://example.com/x"},
        **agent_headers,
    )
    assert response.status_code == 201, response.content


# --- health_url: http(s) plus tcp, because the tcp checker is real ----------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("tcp://192.168.0.10:5432", 201),
        ("http://a.lab.local/health", 201),
        ("tcp://", 422),
        ("db.lab.local:5432", 422),
        ("gopher://a.lab.local", 422),
    ],
)
def test_health_url_allows_exactly_the_checker_schemes(
    client, agent_headers, value, expected
):
    response = post(
        client,
        {"launch_url": "https://a.lab.local", "health_url": value},
        **agent_headers,
    )
    assert response.status_code == expected, response.content
    if expected == 422:
        assert "health_url" in named_fields(response)


# --- icon_url: the three shapes the app itself produces, nothing else -------


@pytest.mark.parametrize(
    "value",
    [
        "https://a.lab.local/icon.png",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "/media/icons/0123abcd.png",
    ],
)
def test_icon_url_accepts_the_shapes_the_app_writes(client, agent_headers, value):
    response = post(
        client,
        {"launch_url": "https://a.lab.local", "icon_url": value},
        **agent_headers,
    )
    assert response.status_code == 201, response.content


@pytest.mark.parametrize(
    "value",
    [
        # utf-8 (non-base64) data URI: the exact B3 attribute-breakout shape.
        'data:image/svg+xml,<svg/>" onerror="alert(1)',
        "javascript:alert(1)",
        "/media/icons/../secret",   # a slash past the prefix is not the shape
        "media/icons/0123abcd.png", # missing the leading slash the app writes
        "192.168.0.10/icon.png",
    ],
)
def test_icon_url_refuses_everything_else_by_name(client, agent_headers, value):
    response = post(
        client,
        {"launch_url": "https://a.lab.local", "icon_url": value},
        **agent_headers,
    )
    assert response.status_code == 422, response.content
    assert "icon_url" in named_fields(response)


# --- The repair: scheme-less legacy rows are fixed, garbage is left ---------


def test_repair_fixes_the_two_broken_shapes_and_clears_bad_icons(client, agent_headers):
    make(slug="legacy")
    # Legacy rows got in exactly like this: writes that never ran clean().
    Service.objects.filter(slug="legacy").update(
        launch_url="192.168.0.10:8096",
        health_url="db.lab.local:5432",
        docs_url="wiki.lab.local/page",
        icon_url='data:image/svg+xml,<svg/>',
    )

    repair(live_apps, None)

    row = Service.objects.get(slug="legacy")
    assert row.launch_url == "http://192.168.0.10:8096"
    assert row.health_url == "http://db.lab.local:5432"
    assert row.docs_url == "http://wiki.lab.local/page"
    assert row.icon_url is None
    # The whole point of repairing over grandfathering: an unrelated edit works.
    response = patch(client, "legacy", {"description": "still editable"}, **agent_headers)
    assert response.status_code == 200, response.content


@pytest.mark.parametrize(
    "field, value",
    [
        ("launch_url", "https://kept.lab.local/x"),
        ("health_url", "tcp://192.168.0.10:5432"),
        ("icon_url", "data:image/svg+xml;base64,PHN2Zy8+"),
        ("icon_url", "/media/icons/0123abcd.png"),
    ],
)
def test_repair_leaves_already_valid_values_alone(field, value):
    make(slug="fine")
    Service.objects.filter(slug="fine").update(**{field: value})

    repair(live_apps, None)

    assert getattr(Service.objects.get(slug="fine"), field) == value


def test_repair_leaves_garbage_alone_and_the_next_edit_names_it(
    client, agent_headers
):
    """`javascript:` is not a typo and gets no `http://` guess. The row's next
    edit 422s on exactly that field — which is the correct demand, unlike a
    scheme-less row blocking on a field the caller never touched."""
    make(slug="garbage")
    Service.objects.filter(slug="garbage").update(launch_url="javascript:alert(1)")

    repair(live_apps, None)

    assert Service.objects.get(slug="garbage").launch_url == "javascript:alert(1)"
    blocked = patch(client, "garbage", {"description": "x"}, **agent_headers)
    assert blocked.status_code == 422
    assert "launch_url" in named_fields(blocked)
    fixed = patch(
        client, "garbage", {"launch_url": "https://garbage.lab.local"}, **agent_headers
    )
    assert fixed.status_code == 200, fixed.content


# --- The migration wiring, via the real executor (the 0003 pattern) ---------

APP = "services"
BEFORE = (APP, "0006_link_misses_and_export_state")
AFTER = (APP, "0007_url_scheme_repair")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    executor.loader.build_graph()
    return executor


@pytest.mark.django_db(transaction=True)
def test_0007_repairs_forward_and_reverses_as_a_noop():
    try:
        executor = _migrate(BEFORE)
        OldService = executor.loader.project_state(BEFORE).apps.get_model(
            APP, "Service"
        )
        OldService.objects.create(
            slug="schemeless", name="s", launch_url="192.168.0.10:8096"
        )

        executor = _migrate(AFTER)
        NewService = executor.loader.project_state(AFTER).apps.get_model(
            APP, "Service"
        )
        assert (
            NewService.objects.get(slug="schemeless").launch_url
            == "http://192.168.0.10:8096"
        )

        # Reverse is a deliberate no-op: the repaired value stays repaired.
        executor = _migrate(BEFORE)
        OldService = executor.loader.project_state(BEFORE).apps.get_model(
            APP, "Service"
        )
        assert (
            OldService.objects.get(slug="schemeless").launch_url
            == "http://192.168.0.10:8096"
        )
    finally:
        call_command("migrate", APP, verbosity=0)


# --- The one URL write that skips clean(): refetch-icon ---------------------


def test_refetch_icon_stores_only_shapes_clean_would_accept(
    client, admin_headers, monkeypatch
):
    """`refetch_service_icon` writes via `save(update_fields=…)`, which never
    runs `clean()` (the window-2 lesson). That is safe only while its source —
    the probe — produces exclusively shapes `clean()` accepts, so pin the
    stored value against the model's own rule rather than trusting the seam."""
    make(slug="icons")
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: SimpleNamespace(
            icon_url="data:image/png;base64,AAAA", error=None
        ),
    )
    response = client.post("/api/v1/services/icons/refetch-icon", **admin_headers)
    assert response.status_code == 200, response.content
    stored = Service.objects.get(slug="icons").icon_url
    assert Service._is_acceptable_icon(stored)
