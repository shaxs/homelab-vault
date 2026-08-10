"""Services API — spec §4c."""

import json

import pytest

from services.models import Service

pytestmark = pytest.mark.django_db


def post(client, payload, **extra):
    return client.post(
        "/api/v1/services",
        data=json.dumps(payload),
        content_type="application/json",
        **extra,
    )


# --- Minimum payload and derivation ---------------------------------------


def test_minimum_payload_is_just_launch_url(client, agent_headers):
    response = post(client, {"launch_url": "https://karakeep.lab.local"}, **agent_headers)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["slug"] == "karakeep"
    assert body["name"]  # derived, never blank
    assert body["health_url"] is None  # defaults to launch_url on read


def test_an_ip_addressed_url_with_no_slug_is_refused(client, agent_headers):
    """v1 §2b's five-minute check, at the API boundary rather than the unit one.

    Slugs are immutable and are the upsert key, so deriving one from an address
    would permanently record where a service happened to live the day it was
    added. `derive_slug` returns "" for that case (test_slugs.py) — this asserts
    the API turns that into a 422 that names `slug` and says what to send, which
    is the half a caller actually experiences.
    """
    response = post(client, {"launch_url": "https://192.168.50.89:2283/"}, **agent_headers)
    assert response.status_code == 422, response.content
    assert "slug" in response.json()["detail"]
    assert not Service.objects.exists()


def test_an_ip_addressed_url_is_accepted_once_a_slug_is_supplied(client, agent_headers):
    """The restriction is on *derivation*, never on what a caller may choose."""
    response = post(
        client,
        {"launch_url": "https://192.168.50.89:2283/", "slug": "immich"},
        **agent_headers,
    )
    assert response.status_code == 201, response.content
    assert response.json()["slug"] == "immich"


def test_health_url_defaults_to_launch_url():
    service = Service(launch_url="https://a.lab.local", slug="a", name="A")
    assert service.probe_url == "https://a.lab.local"
    service.health_url = "http://192.168.50.101:3000/health"
    assert service.probe_url == "http://192.168.50.101:3000/health"


# --- Upsert ---------------------------------------------------------------


def test_reregistering_updates_rather_than_duplicating(client, agent_headers):
    """The single most important property for machine-driven writes (§4c)."""
    first = post(client, {"launch_url": "https://karakeep.lab.local"}, **agent_headers)
    assert first.status_code == 201

    second = post(
        client,
        {"launch_url": "https://karakeep.lab.local", "category": "tools"},
        **agent_headers,
    )
    assert second.status_code == 200  # updated, not created
    assert Service.objects.filter(slug="karakeep").count() == 1
    assert Service.objects.get(slug="karakeep").category == "tools"


def test_url_and_stack_name_are_separate_keys(client, agent_headers):
    """Ruling 2026-08-02: slugs may contain hyphens and the stack name need not
    match the hostname, so these are two naming conventions rather than one.

    The upsert protects within a convention, not across both — registering one
    service by URL and again by stack name yields two records. Accepted: in
    practice Komodo-driven writes always supply the stack and manual adds never
    do, so they do not collide.
    """
    post(client, {"launch_url": "https://Kara-Keep.lab.local"}, **agent_headers)
    post(
        client,
        {"launch_url": "https://Kara-Keep.lab.local", "slug": "karakeep"},
        **agent_headers,
    )
    assert sorted(Service.objects.values_list("slug", flat=True)) == [
        "kara-keep",
        "karakeep",
    ]


def test_upsert_still_protects_within_one_convention(client, agent_headers):
    """The property §4c actually depends on: repeated writes of the same shape
    update rather than duplicate."""
    for _ in range(3):
        post(client, {"launch_url": "https://Kara-Keep.lab.local"}, **agent_headers)
    for _ in range(3):
        post(
            client,
            {"launch_url": "https://Kara-Keep.lab.local", "slug": "karakeep"},
            **agent_headers,
        )
    assert Service.objects.count() == 2


def test_a_lost_insert_race_updates_the_winner_instead_of_500ing(
    client, agent_headers, monkeypatch
):
    """Audit S13: two agents registering the same NEW slug concurrently both
    pass the existence check; one insert wins and the other used to raise
    IntegrityError -> 500. Inventory reconciliation is exactly the workload
    that produces concurrent identical registrations, so the loser must update
    the winner and answer 200.

    The race is simulated deterministically: the winner's row exists, but this
    request's FIRST existence check is blinded — the view a request has when
    the winner commits between its check and its insert. The retry lookup
    (second call) sees the truth.
    """
    from services import api as api_module

    winner = Service.objects.create(
        slug="racer",
        name="Winner's name",
        launch_url="https://racer.lab.local",
        created_by="the-first-agent",
    )

    real_filter = Service.objects.filter
    calls = {"n": 0}

    def pre_commit_view(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return Service.objects.none()
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(api_module.Service.objects, "filter", pre_commit_view)

    response = post(
        client,
        {"launch_url": "https://racer.lab.local", "category": "infra"},
        **agent_headers,
    )

    assert response.status_code == 200  # an update, not a create, never a 500
    assert Service.objects.count() == 1
    after = Service.objects.get(slug="racer")
    assert after.id == winner.id
    assert after.category == "infra"  # the loser's payload landed
    # The winner's provenance survived — the retry applies the payload only.
    assert after.created_by == "the-first-agent"


def test_idempotent_reregistration_changes_nothing(client, agent_headers):
    """DoD (§11): re-registering with the same one-field payload changes
    nothing."""
    post(client, {"launch_url": "https://karakeep.lab.local"}, **agent_headers)
    before = Service.objects.get(slug="karakeep")
    snapshot = (before.name, before.launch_url, before.category, before.tags)

    post(client, {"launch_url": "https://karakeep.lab.local"}, **agent_headers)
    after = Service.objects.get(slug="karakeep")
    assert (after.name, after.launch_url, after.category, after.tags) == snapshot


# --- Size limits (audit S12) -----------------------------------------------
#
# Before these, every service string was an unconstrained TextField and tags an
# unbounded text[]. The public catalog re-serves the whole collection on every
# 15-second poll and filters it in the browser, so parked megabytes keep
# costing long after the writer stops — persistent resource exhaustion, gated
# only by `open-writes`.


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "n" * 201),
        ("category", "c" * 201),
        ("host_label", "h" * 201),
        ("description", "d" * 4001),
        ("launch_url_external", "https://x.lab.local/" + "p" * 2000),
        ("health_url", "https://x.lab.local/" + "p" * 2000),
        ("repo_url", "https://x.lab.local/" + "p" * 2000),
        ("docs_url", "https://x.lab.local/" + "p" * 2000),
        ("icon_url", "data:image/png;base64," + "A" * 60_000),
    ],
    # Explicit ids: the default id embeds the value, and a 60K-char test id
    # overflows Windows' 32767-char environment variable cap via
    # PYTEST_CURRENT_TEST — an ERROR before the test even runs.
    ids=[
        "name", "category", "host_label", "description",
        "launch_url_external", "health_url", "repo_url", "docs_url",
        "icon_url",
    ],
)
def test_an_oversized_field_is_refused_by_name_on_create(
    client, agent_headers, field, value
):
    response = post(
        client,
        {"launch_url": "https://sized.lab.local", field: value},
        **agent_headers,
    )
    assert response.status_code == 422
    assert field in response.content.decode()
    assert Service.objects.count() == 0


def test_an_oversized_launch_url_is_refused_by_name(client, agent_headers):
    response = post(
        client,
        {"launch_url": "https://x.lab.local/" + "p" * 2000},
        **agent_headers,
    )
    assert response.status_code == 422
    assert "launch_url" in response.content.decode()


def test_an_oversized_field_is_refused_on_patch_too(client, agent_headers):
    """Same bound, other write path — the S1 lesson generalised."""
    post(client, {"launch_url": "https://sized.lab.local"}, **agent_headers)
    response = client.patch(
        "/api/v1/services/sized",
        data={"name": "n" * 201},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "name" in response.content.decode()


def test_a_value_exactly_at_the_limit_is_accepted(client, agent_headers):
    response = post(
        client,
        {"launch_url": "https://sized.lab.local", "name": "n" * 200},
        **agent_headers,
    )
    assert response.status_code == 201


def test_too_many_tags_are_refused_by_name(client, agent_headers):
    response = post(
        client,
        {
            "launch_url": "https://tagged.lab.local",
            "tags": [f"t{n}" for n in range(33)],
        },
        **agent_headers,
    )
    assert response.status_code == 422
    assert "tags" in response.content.decode()


def test_an_oversized_tag_is_refused_by_name(client, agent_headers):
    response = post(
        client,
        {"launch_url": "https://tagged.lab.local", "tags": ["ok", "x" * 101]},
        **agent_headers,
    )
    assert response.status_code == 422
    assert "tags" in response.content.decode()


def test_thirty_two_short_tags_are_fine(client, agent_headers):
    response = post(
        client,
        {
            "launch_url": "https://tagged.lab.local",
            "tags": [f"t{n}" for n in range(32)],
        },
        **agent_headers,
    )
    assert response.status_code == 201


def test_a_taxonomy_rename_cannot_smuggle_an_oversized_category(
    client, agent_headers
):
    """Found by this window's security gate: `rename_category` writes via
    `save(update_fields=...)`, which never runs `clean()` — a third write path
    around the S12 ceilings until it got its own bound."""
    post(
        client,
        {"launch_url": "https://sized.lab.local", "category": "infra"},
        **agent_headers,
    )
    response = client.patch(
        "/api/v1/categories/infra",
        data={"name": "c" * 201},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "name" in response.content.decode()
    assert Service.objects.get(slug="sized").category == "infra"


def test_a_tag_rename_cannot_smuggle_an_oversized_tag(client, agent_headers):
    post(
        client,
        {"launch_url": "https://sized.lab.local", "tags": ["ok"]},
        **agent_headers,
    )
    response = client.patch(
        "/api/v1/tags/ok",
        data={"name": "t" * 101},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert Service.objects.get(slug="sized").tags == ["ok"]


def test_a_saved_view_refuses_an_oversized_name_and_query(client, agent_headers):
    """Same S12 class, same gate finding: `/views` is agent-writable and the
    list is served anonymously, so it gets the same ceilings."""
    refused = client.post(
        "/api/v1/views",
        data={"name": "n" * 201, "query": "state:up"},
        content_type="application/json",
        **agent_headers,
    )
    assert refused.status_code == 422
    refused = client.post(
        "/api/v1/views",
        data={"name": "fine", "query": "q" * 2001},
        content_type="application/json",
        **agent_headers,
    )
    assert refused.status_code == 422
    from services.models import SavedView

    assert SavedView.objects.count() == 0


def test_a_view_position_out_of_integer_range_is_a_422_not_a_500(
    client, agent_headers
):
    """Views skip `full_clean`, so Django's automatic integer-range validators
    never run for them — `position: 10**12` was a Postgres DataError 500
    instead of the contract's 422 (this window's api-security gate)."""
    response = client.post(
        "/api/v1/views",
        data={"name": "v", "query": "state:up", "position": 10**12},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "position" in response.content.decode()


def test_a_view_rename_is_bounded_too(client, agent_headers):
    from services.models import SavedView

    SavedView.objects.create(name="mine", query="state:up")
    response = client.patch(
        "/api/v1/views/mine",
        data={"name": "n" * 201},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert SavedView.objects.filter(name="mine").exists()


def test_the_catalog_ceiling_refuses_new_records_not_updates(
    client, agent_headers, monkeypatch
):
    """The cap must not break §4c idempotency: at the ceiling, re-registering
    an existing slug still answers 200. Only a NEW record is refused."""
    from services import api as api_module

    monkeypatch.setattr(api_module, "MAX_SERVICES", 2)
    post(client, {"launch_url": "https://one.lab.local"}, **agent_headers)
    post(client, {"launch_url": "https://two.lab.local"}, **agent_headers)

    refused = post(client, {"launch_url": "https://three.lab.local"}, **agent_headers)
    assert refused.status_code == 422
    assert "limit" in refused.content.decode().lower()
    assert Service.objects.count() == 2

    updated = post(
        client,
        {"launch_url": "https://one.lab.local", "category": "still-works"},
        **agent_headers,
    )
    assert updated.status_code == 200


# --- Strict validation ----------------------------------------------------


def test_unknown_field_is_rejected_and_named(client, agent_headers):
    """"A caller that guesses a field name must be told it guessed wrong" (§4c)."""
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "lanuch_target": "new_tab"},
        **agent_headers,
    )
    assert response.status_code == 422
    assert "lanuch_target" in response.content.decode()


def test_provenance_is_server_set_and_rejected_from_callers(client, agent_headers):
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "provenance": "manual"},
        **agent_headers,
    )
    assert response.status_code == 422
    assert "provenance" in response.content.decode()


def test_created_by_is_rejected_from_callers(client, agent_headers):
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "created_by": "someone-else"},
        **agent_headers,
    )
    assert response.status_code == 422
    assert "created_by" in response.content.decode()


def test_api_writes_are_stamped_with_provenance_api(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    service = Service.objects.get(slug="x")
    assert service.provenance == "api"
    assert service.created_by == "token:agent"


def test_patch_cannot_change_the_slug(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps({"slug": "renamed"}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "slug" in response.content.decode()


# --- Reads are unauthenticated (§4c) --------------------------------------


def test_catalog_reads_need_no_authentication(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    assert client.get("/api/v1/services").status_code == 200
    assert client.get("/api/v1/services/x").status_code == 200
    assert client.get("/api/v1/health/x").status_code == 200
    assert client.get("/api/v1/export/json").status_code == 200


def test_tags_round_trip_through_the_database(client, agent_headers):
    """`tags` is a PostgreSQL `text[]` (`ArrayField`); a list must survive the
    round trip through the database and back out of the API unchanged."""
    post(
        client,
        {"launch_url": "https://x.lab.local", "tags": ["backup", "media"]},
        **agent_headers,
    )
    service = Service.objects.get(slug="x")
    assert service.tags == ["backup", "media"]
    assert client.get("/api/v1/services/x").json()["tags"] == ["backup", "media"]


def test_empty_tags_default_to_a_list(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    assert Service.objects.get(slug="x").tags == []


# --- The edit form's save path (VALIDATION_V1.3 findings 5 and 7) ---------
#
# These exist because 463 tests exercised `notify_enabled` and the nullable
# interval through `Service.objects.create()` and through the worker, and NOT
# ONE went through PATCH — so a field missing from `ServicePatch` was invisible
# to the whole suite while being the first thing a human hit. `extra="forbid"`
# is what turned that omission into a 422 on every save rather than a silently
# ignored field, which is the design working; the gap was the coverage.


def test_patch_can_turn_notifications_off(client, agent_headers):
    """§4's flag has to be reachable from the edit form, not just at create."""
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps({"notify_enabled": False}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 200, response.content
    assert response.json()["notify_enabled"] is False
    assert Service.objects.get(slug="x").notify_enabled is False


def test_patch_can_turn_notifications_back_on(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    Service.objects.filter(slug="x").update(notify_enabled=False)
    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps({"notify_enabled": True}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 200, response.content
    assert Service.objects.get(slug="x").notify_enabled is True


def test_the_edit_forms_whole_payload_is_accepted(client, agent_headers):
    """The exact shape `ServiceRecord.vue` sends on save.

    Field-by-field tests pass while the *combination* the UI actually sends is
    rejected — which is what happened: `notify_enabled` was absent from
    `ServicePatch`, so every save from the record screen 422'd.
    """
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps(
            {
                "name": "X",
                "icon_url": None,
                "category": None,
                "host_label": None,
                "description": None,
                "launch_url": "https://x.lab.local",
                "health_url": None,
                "health_enabled": True,
                "notify_enabled": False,
                "health_interval_s": 300,
                "health_fail_threshold": 5,
                "tags": [],
            }
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 200, response.content
    saved = Service.objects.get(slug="x")
    assert saved.notify_enabled is False
    assert saved.health_interval_s == 300
    assert saved.health_fail_threshold == 5


def test_patch_can_pin_and_then_unpin_an_interval(client, agent_headers):
    """Null is the state that means "follow the global", so clearing the field
    on the edit form has to reach the column as a real null."""
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)

    client.patch(
        "/api/v1/services/x",
        data=json.dumps({"health_interval_s": 300, "health_fail_threshold": 5}),
        content_type="application/json",
        **agent_headers,
    )
    assert Service.objects.get(slug="x").health_interval_s == 300

    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps({"health_interval_s": None, "health_fail_threshold": None}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 200, response.content
    saved = Service.objects.get(slug="x")
    assert saved.health_interval_s is None
    assert saved.health_fail_threshold is None
    # And the response still carries the number actually in force.
    assert response.json()["health_interval_effective_s"] == 60


# --- health_timeout_ms bounds (audit S1) -----------------------------------


def test_a_negative_timeout_is_refused_and_named(client, agent_headers):
    """httpx accepts a negative Timeout and `asyncio.wait_for` raises
    immediately on one, so -5000 would fail every check instantly and turn the
    whole catalog down in a single write (audit S1)."""
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "health_timeout_ms": -5000},
        **agent_headers,
    )
    assert response.status_code == 422, response.content
    assert "health_timeout_ms" in response.content.decode()
    assert not Service.objects.exists()


def test_an_enormous_timeout_is_refused_and_named(client, agent_headers):
    """POST /health/{slug}/check runs the check on the request thread, so an
    unbounded timeout is a gunicorn worker held hostage (audit S1)."""
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "health_timeout_ms": 2147483647},
        **agent_headers,
    )
    assert response.status_code == 422, response.content
    assert "health_timeout_ms" in response.content.decode()
    assert not Service.objects.exists()


@pytest.mark.parametrize("value", [250, 60000])
def test_the_timeout_bounds_are_inclusive(client, agent_headers, value):
    response = post(
        client,
        {"launch_url": "https://x.lab.local", "health_timeout_ms": value},
        **agent_headers,
    )
    assert response.status_code == 201, response.content


def test_patch_cannot_smuggle_an_out_of_bounds_timeout(client, agent_headers):
    post(client, {"launch_url": "https://x.lab.local"}, **agent_headers)
    created = Service.objects.get(slug="x").health_timeout_ms

    response = client.patch(
        "/api/v1/services/x",
        data=json.dumps({"health_timeout_ms": -5000}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422, response.content
    assert "health_timeout_ms" in response.content.decode()
    # The stored value is untouched — the rejected patch never lands.
    assert Service.objects.get(slug="x").health_timeout_ms == created
