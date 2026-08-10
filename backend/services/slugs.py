"""Slug derivation — spec §4c.

This module is the ONLY place a slug is derived. Spec §4c: "Slug generation must
be identical in the UI and the API, or the upsert protects nothing." The SPA does
not reimplement this; it calls `GET /api/v1/slug-preview`, so the two cannot
diverge by construction.
"""

import ipaddress
import re
from urllib.parse import urlsplit

from django.utils.text import slugify

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def derive_slug(launch_url: str = "", stack_name: str | None = None) -> str:
    """Derive the immutable upsert key for a service.

    Source is the Komodo stack name when one is supplied, otherwise the first
    hostname label of `launch_url`; non-alphanumerics collapse to hyphens
    (§4c), and the result goes through `django.utils.text.slugify` as §3b
    requires.

        https://Kara-Keep.lab.local  ->  kara-keep
        stack "karakeep"             ->  karakeep

    Those two deliberately do NOT converge. §4c's worked example implies they
    should, but the stack name and the hostname are independent naming
    conventions and a slug is allowed to contain hyphens (ruling, 2026-08-02 —
    the example was overreaching, the rule text is correct).

    Returns "" when `launch_url` is addressed by IP and no name was supplied —
    an address is never an acceptable permanent identity. Callers must ask for
    one rather than inventing it.

    What "identical in the UI and the API" actually protects is that the *same
    input* derives the *same slug* on both sides. It does not promise that two
    different inputs for one service collapse together. Consequence, accepted:
    registering one service both by URL and by stack name yields two records.
    The upsert protects within a naming convention, not across two.
    """
    source = (stack_name or "").strip()
    if not source:
        source = _first_host_label(launch_url)
    if not source:
        return ""

    # Hyphenate separators BEFORE slugify, because slugify *deletes* characters
    # like "." and ":" rather than replacing them — 192.168.50.101 would fold to
    # "19216868101", and fd00::1 / fd0:0::1 would collide on one key.
    # isalnum() is unicode-aware, so accented letters survive to be folded by
    # slugify (café -> cafe) instead of being stripped here.
    hyphenated = "".join(ch if ch.isalnum() else "-" for ch in source)
    folded = slugify(hyphenated, allow_unicode=False)
    # slugify keeps underscores; they are non-alphanumeric and so become hyphens.
    return _NON_ALNUM.sub("-", folded).strip("-")


def _first_host_label(launch_url: str) -> str:
    raw = (launch_url or "").strip()
    if not raw:
        return ""
    # urlsplit only populates .hostname when a scheme is present.
    parts = urlsplit(raw if "//" in raw else f"//{raw}")
    host = parts.hostname or ""
    if is_ip_literal(host):
        # An address is never an acceptable identity. The slug is IMMUTABLE and
        # is the upsert key, so deriving it from an address permanently records
        # where the service happened to live on the day it was added — and
        # "first label" would be worse still, slugging 192.168.50.101 to "192"
        # and colliding with every other host on the subnet.
        #
        # Returning nothing forces the caller to supply one: the UI asks the
        # user, and the API rejects the write with an actionable 422. An
        # explicitly supplied slug is never filtered here, so a caller that
        # really wants an address-shaped slug can still pass one.
        return ""
    return host.split(".")[0]


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
