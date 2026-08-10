"""Release-packaging invariants, pinned (audit window 4: S14, B6, B8).

Text-level pins in the test_compose_wiring style: Docker is not runnable in
this environment, so nothing here proves an image builds — CI's container job
does that. The bug class this catches is the guarantee being *edited away*:
a digest pin dropped during a base-image bump, `npm ci` quietly reverted to
`npm install`, a hash requirement removed to make an install error go away,
a private-path ignore rule deleted, or the author's real topology leaking
back into a fixture. Every one of those is a silent regression today and a
public one after release.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
GITIGNORE = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
LOCK = (REPO_ROOT / "backend" / "requirements.lock").read_text(encoding="utf-8")


# --- S14: the build must be reproducible -----------------------------------


def test_base_images_are_pinned_by_digest():
    froms = [l for l in DOCKERFILE.splitlines() if l.startswith("FROM ")]
    assert len(froms) == 2
    for line in froms:
        assert "@sha256:" in line, f"unpinned base image: {line}"


def test_frontend_installs_with_npm_ci_against_a_mandatory_lockfile():
    # No glob: `package-lock.json*` made the lockfile optional, and a missing
    # lock plus `npm install` resolves fresh inside the image.
    assert "COPY frontend/package.json frontend/package-lock.json ./" in DOCKERFILE
    assert "package-lock.json*" not in DOCKERFILE
    assert "RUN npm ci" in DOCKERFILE
    # The executable form only — a comment may name npm install to explain why not.
    assert "RUN npm install" not in DOCKERFILE


def test_python_deps_install_hash_checked_from_the_lock():
    # The whole RUN line, not a substring — a comment mentioning the flag must
    # not satisfy this (the red-check caught exactly that).
    assert (
        "RUN pip install --no-cache-dir --require-hashes -r backend/requirements.lock"
        in DOCKERFILE
    )
    # The old shape: a bare `gunicorn` appended to the pip install line,
    # unpinned and absent from any requirements file.
    assert "requirements.txt gunicorn" not in DOCKERFILE


def _lock_entries():
    """Map each pinned package in the lock to its own hash lines.

    Per entry, not an aggregate count: one package carries dozens of platform
    wheels' hashes, so a total-vs-total comparison passes with a hashless
    package inserted (the red-check proved it).
    """
    entries, current = {}, None
    for line in LOCK.splitlines():
        if line and not line.startswith((" ", "#")) and "==" in line:
            current = line.split("==")[0]
            entries[current] = []
        elif current and line.strip().startswith("--hash=sha256:"):
            entries[current].append(line.strip())
    return entries


def test_the_lock_pins_every_package_with_hashes():
    entries = _lock_entries()
    assert len(entries) >= 10, "lock looks truncated"
    unhashed = [name for name, hashes in entries.items() if not hashes]
    assert not unhashed, f"pinned without a hash: {unhashed}"
    assert "gunicorn" in entries, "gunicorn must ride the lock"


# --- B6: the private-path protection must travel with a clone ---------------


@pytest.mark.parametrize(
    "rule",
    [
        "spec/",
        "design/",
        "_backups/",
        ".claude/",
        "CLAUDE.md",
        "AGENTS.md",
        ".mcp.json",
        "ops/DEPLOY.md",
        "ops/RESTORE.md",
        "ops/kan_sync.py",
    ],
)
def test_private_paths_are_ignored_by_the_tracked_gitignore(rule):
    # Whole-line match: a mention inside a comment must not satisfy this.
    assert rule in GITIGNORE.splitlines(), f"{rule} missing from tracked .gitignore"


# --- B8: the author's topology must not re-enter tracked files --------------

# Constructed by concatenation so this file never matches its own scan.
_TOPOLOGY = [
    b"192.168." + b"68.",
    b"pve-" + b"m3",
    b"pve-" + b"main",
    b"pve-" + b"intel",
    b"pve-" + b"ha",
    b"truenas" + b"-media",
    b"home" + b"-lab",
    b"/mnt/" + b"truenas",
]


def test_no_tracked_file_carries_the_real_topology():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    offenders = []
    for rel in out.stdout.decode("utf-8").split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        for pattern in _TOPOLOGY:
            if pattern in data:
                offenders.append(f"{rel}: {pattern.decode()}")
    assert not offenders, "real topology in tracked files:\n" + "\n".join(offenders)
