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
