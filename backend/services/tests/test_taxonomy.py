"""Renaming and removing categories and tags in bulk.

Spec §6b says these need no administration because "a value exists because a
service uses it and disappears when none do" — true of their lifecycle, but it
leaves no way to fix a typo except editing every service by hand.
"""

import json

import pytest

from services.models import Service

pytestmark = pytest.mark.django_db


def make(slug, category=None, tags=None):
    return Service.objects.create(
        slug=slug,
        name=slug.title(),
        launch_url=f"https://{slug}.lab.local",
        category=category,
        tags=tags or [],
    )


def patch(client, path, payload, **extra):
    return client.patch(
        path, data=json.dumps(payload), content_type="application/json", **extra
    )


# --- Categories -----------------------------------------------------------


def test_rename_category_updates_every_service(client, agent_headers):
    make("a", category="infastructure")
    make("b", category="infastructure")
    make("c", category="media")

    response = patch(
        client, "/api/v1/categories/infastructure", {"name": "infrastructure"},
        **agent_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"name": "infrastructure", "affected": 2}

    assert Service.objects.get(slug="a").category == "infrastructure"
    assert Service.objects.get(slug="b").category == "infrastructure"
    assert Service.objects.get(slug="c").category == "media"


def test_renaming_onto_an_existing_category_merges(client, agent_headers):
    """The typo and the correct value often both exist by the time it's noticed."""
    make("a", category="Media")
    make("b", category="media")

    patch(client, "/api/v1/categories/Media", {"name": "media"}, **agent_headers)

    assert set(Service.objects.values_list("category", flat=True)) == {"media"}


def test_rename_category_rejects_an_empty_name(client, agent_headers):
    make("a", category="media")
    response = patch(client, "/api/v1/categories/media", {"name": "  "}, **agent_headers)
    assert response.status_code == 422
    assert Service.objects.get(slug="a").category == "media"


def test_renaming_an_unused_category_affects_nothing(client, agent_headers):
    response = patch(client, "/api/v1/categories/ghost", {"name": "x"}, **agent_headers)
    assert response.json()["affected"] == 0


def test_clear_category_unsets_it_without_deleting_services(client, admin_headers):
    make("a", category="typo")
    make("b", category="typo")

    response = client.delete("/api/v1/categories/typo", **admin_headers)
    assert response.json()["affected"] == 2
    assert Service.objects.count() == 2
    assert set(Service.objects.values_list("category", flat=True)) == {None}


def test_clearing_a_category_is_admin_only(client, agent_headers):
    make("a", category="typo")
    assert client.delete("/api/v1/categories/typo", **agent_headers).status_code == 401


# --- Tags -----------------------------------------------------------------


def test_rename_tag_updates_every_service(client, agent_headers):
    make("a", tags=["ai", "gpu"])
    make("b", tags=["ai"])
    make("c", tags=["media"])

    response = patch(client, "/api/v1/tags/ai", {"name": "llm"}, **agent_headers)
    assert response.json() == {"name": "llm", "affected": 2}

    assert Service.objects.get(slug="a").tags == ["llm", "gpu"]
    assert Service.objects.get(slug="b").tags == ["llm"]
    assert Service.objects.get(slug="c").tags == ["media"]


def test_renaming_a_tag_onto_an_existing_one_does_not_duplicate_it(
    client, agent_headers
):
    make("a", tags=["arr", "media", "arr-stack"])

    patch(client, "/api/v1/tags/arr-stack", {"name": "arr"}, **agent_headers)

    tags = Service.objects.get(slug="a").tags
    assert tags == ["arr", "media"]
    assert len(tags) == len(set(tags))


def test_tag_order_is_preserved(client, agent_headers):
    make("a", tags=["one", "two", "three"])
    patch(client, "/api/v1/tags/two", {"name": "second"}, **agent_headers)
    assert Service.objects.get(slug="a").tags == ["one", "second", "three"]


def test_delete_tag_removes_it_everywhere(client, admin_headers):
    make("a", tags=["keep", "drop"])
    make("b", tags=["drop"])

    response = client.delete("/api/v1/tags/drop", **admin_headers)
    assert response.json()["affected"] == 2
    assert Service.objects.get(slug="a").tags == ["keep"]
    assert Service.objects.get(slug="b").tags == []


def test_deleting_a_tag_is_admin_only(client, agent_headers):
    make("a", tags=["x"])
    assert client.delete("/api/v1/tags/x", **agent_headers).status_code == 401


def test_tag_rename_matches_exactly_not_by_substring(client, agent_headers):
    make("a", tags=["media", "media-server"])
    patch(client, "/api/v1/tags/media", {"name": "video"}, **agent_headers)
    assert Service.objects.get(slug="a").tags == ["video", "media-server"]


def test_taxonomy_writes_require_authentication(client):
    make("a", category="media", tags=["x"])
    assert patch(client, "/api/v1/categories/media", {"name": "x"}).status_code == 401
    assert patch(client, "/api/v1/tags/x", {"name": "y"}).status_code == 401


# --- Saved views: rename (added 2026-08-07) --------------------------------
#
# Views had create, list and delete but no rename, so a typo in a view name was
# unfixable except by deleting it and rebuilding the query from scratch — the
# same gap this file exists to close for categories and tags.


def a_view(name="Unhealthy", query="state:!up"):
    from services.models import SavedView

    return SavedView.objects.create(name=name, query=query)


def test_a_view_can_be_renamed(client, agent_headers):
    a_view()

    response = client.patch(
        "/api/v1/views/Unhealthy",
        data=json.dumps({"name": "Needs attention"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Needs attention"


def test_renaming_a_view_leaves_its_query_alone(client, agent_headers):
    """A saved view *is* its query (§4b). Renaming is a label change; if the
    query moved too, every saved view would be one rename away from meaning
    something else."""
    a_view(query="host:pve-c state:!up")

    response = client.patch(
        "/api/v1/views/Unhealthy",
        data=json.dumps({"name": "Broken on m3"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.json()["query"] == "host:pve-c state:!up"


def test_renaming_onto_an_existing_view_is_refused_not_merged(client, agent_headers):
    """The opposite of the category rule, deliberately.

    Merging two spellings of a category is the point — one idea, one value. Two
    views are two different stored queries, so merging them would silently throw
    one away.
    """
    a_view(name="One", query="state:down")
    a_view(name="Two", query="state:stale")

    response = client.patch(
        "/api/v1/views/One",
        data=json.dumps({"name": "Two"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 422
    assert "already exists" in response.json()["detail"]
    # Both survive, with their own queries.
    from services.models import SavedView

    assert SavedView.objects.count() == 2
    assert SavedView.objects.get(name="One").query == "state:down"


def test_renaming_a_view_to_its_own_name_is_accepted(client, agent_headers):
    """The collision check must not trip on the row being renamed."""
    a_view(name="Same")

    response = client.patch(
        "/api/v1/views/Same",
        data=json.dumps({"name": "Same"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 200


def test_an_empty_view_name_is_refused(client, agent_headers):
    a_view()

    response = client.patch(
        "/api/v1/views/Unhealthy",
        data=json.dumps({"name": "   "}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 422


def test_renaming_an_unknown_view_is_a_404(client, agent_headers):
    response = client.patch(
        "/api/v1/views/nope",
        data=json.dumps({"name": "x"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 404


def test_renaming_a_view_needs_a_token(client):
    """Agent scope, matching create. Anonymous must not reach it under `full`."""
    a_view()

    response = client.patch(
        "/api/v1/views/Unhealthy",
        data=json.dumps({"name": "x"}),
        content_type="application/json",
    )

    assert response.status_code == 401


def test_the_view_rename_payload_is_strict(client, agent_headers):
    """`extra="forbid"` — an unknown key is a 422 naming it, not a silent drop
    (§4c). Without this, `{"query": "..."}` would look like it worked."""
    a_view()

    response = client.patch(
        "/api/v1/views/Unhealthy",
        data=json.dumps({"name": "x", "query": "state:up"}),
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 422
