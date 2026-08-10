"""Slug derivation — spec §4c.

"Slug generation must be identical in the UI and the API, or the upsert protects
nothing." That is the property under test: the same input derives the same slug
everywhere. It is NOT a promise that two different inputs for one service
collapse together — see test_url_and_stack_name_are_independent_conventions.
"""

import pytest

from services.slugs import derive_slug


def test_url_and_stack_name_are_independent_conventions():
    """Ruling 2026-08-02: a slug may contain hyphens, and the stack name need not
    match the hostname. §4c's worked example implied these converge; they do not,
    and the rule text ("non-alphanumerics collapsed to hyphens") is correct."""
    assert derive_slug("https://Kara-Keep.lab.local") == "kara-keep"
    assert derive_slug("https://Kara-Keep.lab.local", stack_name="karakeep") == "karakeep"


def test_derivation_is_deterministic():
    """The property the upsert actually depends on: same input, same slug, every
    time and on both sides of the API."""
    for _ in range(3):
        assert derive_slug("https://Kara-Keep.lab.local") == "kara-keep"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://karakeep.lab.local", "karakeep"),
        ("https://JELLYFIN.lab.local", "jellyfin"),
        ("http://nas.lan:5001", "nas"),
        ("https://open-webui.lab.example/path?q=1", "open-webui"),
        ("nas.lan", "nas"),
        ("", ""),
    ],
)
def test_derives_from_first_hostname_label(url, expected):
    assert derive_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://192.168.50.101:3000",
        "http://10.0.0.5",
        "http://[fd00::1]:8080",
    ],
)
def test_an_address_never_becomes_a_slug(url):
    """The slug is immutable and is the upsert key, so deriving it from an
    address permanently records where the service happened to live. Blank forces
    the caller to supply a real identity (ruling, 2026-08-02)."""
    assert derive_slug(url) == ""


def test_an_explicit_slug_is_still_honoured_for_an_ip_url():
    """The restriction is on *derivation*, not on what a caller may choose."""
    assert derive_slug("http://192.168.50.89:2283/", "immich") == "immich"


def test_an_explicitly_supplied_address_shaped_slug_is_not_filtered():
    assert derive_slug("", "192.168.1.1") == "192-168-1-1"


def test_stack_name_wins_over_url():
    assert derive_slug("https://something-else.lab.local", "karakeep") == "karakeep"


def test_blank_stack_name_falls_back_to_url():
    assert derive_slug("https://karakeep.lab.local", "   ") == "karakeep"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Kara-Keep", "kara-keep"),
        ("kara_keep", "kara-keep"),   # underscores are non-alphanumeric too
        ("KARA KEEP", "kara-keep"),
        ("kara--keep", "kara-keep"),  # runs collapse to one hyphen
        ("-karakeep-", "karakeep"),   # no leading or trailing hyphen
        ("café", "cafe"),             # unicode folds rather than being dropped
    ],
)
def test_normalisation_rules(source, expected):
    assert derive_slug("", stack_name=source) == expected


def test_case_is_ignored():
    assert derive_slug("https://KARAKEEP.lab.local") == derive_slug(
        "https://karakeep.lab.local"
    )
