"""The compose file's own invariant, pinned (audit B4).

docker-compose.yml's header states it: Compose does not inject `.env` into
containers on its own, so a variable not listed in a container's `environment:`
block is invisible to the app no matter what `.env` says.
`HOMELABVAULT_AUTH_MODE` and `ADMIN_REAUTH_WINDOW_S` had 34 lines of
`.env.example` documentation since V1.2 and appeared nowhere in compose — an
operator setting `open-writes` restarted into a silent `full`, with nothing in
any log saying why, because `current_auth_mode()` only warns about a value it
actually received.

This is a text-level pin, stated plainly: Docker is not runnable in this
environment and pyyaml is not a dependency, so nothing here proves the value
reaches a running container — the staging clone does that. The bug class this
catches is the line being *absent*, which is exactly how B4 happened.
"""

from pathlib import Path

import pytest

COMPOSE = (
    Path(__file__).resolve().parents[3] / "docker-compose.yml"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    [
        "HOMELABVAULT_AUTH_MODE: ${HOMELABVAULT_AUTH_MODE:-full}",
        "ADMIN_REAUTH_WINDOW_S: ${ADMIN_REAUTH_WINDOW_S:-300}",
    ],
)
def test_documented_auth_env_reaches_the_web_container(line):
    assert line in COMPOSE


# --- published image wiring ------------------------------------------------
# Same text-level reasoning as above: this cannot prove a pull works, only that
# the lines are present. The bug class is again ABSENCE — drop `image:` and a
# deploy silently goes back to building on the server, which is the thing
# publishing to GHCR exists to stop, and it fails open (it still works, just
# slowly and from unreviewed local state) so nothing would ever report it.

# Counted against the DIRECTIVES, not the file: the header comment explains this
# wiring and quotes `build: .` while doing so, which made the first draft of
# these tests count four services and go red against a correct file. A test that
# a comment can satisfy — or break — is measuring the wrong thing.
SETTINGS = "\n".join(
    line for line in COMPOSE.splitlines() if not line.lstrip().startswith("#")
)

IMAGE_REF = "image: ghcr.io/shaxs/homelab-vault:${IMAGE_TAG:-latest}"
APP_SERVICES = 4  # web, worker, proxmox-worker, truenas-worker (TN2)


def test_every_app_service_runs_the_published_image():
    assert SETTINGS.count(IMAGE_REF) == APP_SERVICES


def test_the_app_services_keep_a_local_build_fallback():
    # `build: .` beside `image:` is what makes `up -d --build` still work when
    # the registry is unreachable — a fallback ops/DEPLOY.md §8 promises.
    assert SETTINGS.count("build: .") == APP_SERVICES


def test_the_image_tag_is_a_parameter_not_a_hardcoded_version():
    # A version pinned in the compose file would put "which release is this
    # host on" under git rather than in `.env`, so every host would have to
    # carry a local edit and pulling the file would silently change versions.
    assert "${IMAGE_TAG:-latest}" in SETTINGS
