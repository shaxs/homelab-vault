"""The TrueNAS provider — v1 §7a (the second provider).

TrueNAS fits the seam as a *storage* provider, not a guest one, and these tests
pin that: `guests()` is empty and `infrastructure()` is None on purpose, while
the real data rides `report()`. The cases that matter most mirror the operator's
actual want — a DEGRADED pool, a failing SMART disk, and the box's own alert
feed — plus the two things a public panel must never leak (disk serials) and the
best-effort reads degrading without taking the section down.
"""

import pytest

from services.providers import TrueNASProvider, configured_providers
from services.providers.base import Provider, ProviderUnavailable
from services.providers.truenas import _rpc_error_text

from .truenas_fakes import (
    GIB,
    TIB,
    FakeTrueNAS,
    install,
    install_unreachable,
)


@pytest.fixture
def provider(settings):
    settings.TRUENAS_HOST = "https://truenas.test"
    settings.TRUENAS_API_KEY = "1-secretkey"
    settings.TRUENAS_API_USER = "hvault"
    settings.TRUENAS_VERIFY_TLS = False
    settings.TRUENAS_TIMEOUT_S = 15.0
    return TrueNASProvider()


# --- Configuration & seam --------------------------------------------------


def test_no_credentials_is_not_configured():
    # Hermetic via the `_no_real_truenas` autouse fixture in conftest, which
    # clears TRUENAS_* the way `_no_real_proxmox` clears the Proxmox creds — so
    # this holds even on a dev box whose .env points at a real box.
    assert TrueNASProvider().is_configured() is False


def test_is_configured_needs_host_key_and_user(settings):
    """All three: 25.04 keys are user-linked, so a key without its username
    cannot authenticate and must read as not-configured, not as a poll error."""
    settings.TRUENAS_HOST = "https://truenas.test"
    settings.TRUENAS_API_KEY = ""
    settings.TRUENAS_API_USER = "hvault"
    assert TrueNASProvider().is_configured() is False
    settings.TRUENAS_API_KEY = "key"
    settings.TRUENAS_HOST = ""
    assert TrueNASProvider().is_configured() is False
    settings.TRUENAS_HOST = "https://truenas.test"
    settings.TRUENAS_API_USER = ""
    assert TrueNASProvider().is_configured() is False


def test_partial_configuration_names_the_missing_variable(settings):
    """The upgrade trap: a TN2-era deploy has host+key set, and TN-WS adds a
    third required var. "Not configured." beside a plainly-set host and key is
    a dead end; the gap must be named. Wholly-unset stays the quiet §7d
    string — not using TrueNAS is a choice, not a mistake."""
    settings.TRUENAS_HOST = "https://truenas.test"
    settings.TRUENAS_API_KEY = "key"
    settings.TRUENAS_API_USER = ""
    provider = TrueNASProvider()
    assert provider.configuration_gaps() == ["TRUENAS_API_USER"]
    status = provider.status()
    assert status.configured is False
    assert "TRUENAS_API_USER" in status.detail

    settings.TRUENAS_HOST = ""
    settings.TRUENAS_API_KEY = ""
    assert TrueNASProvider().configuration_gaps() == []
    assert TrueNASProvider().status().detail == "No TrueNAS credentials set."


def test_a_host_without_a_scheme_is_refused_by_name(settings):
    settings.TRUENAS_HOST = "truenas.test"
    settings.TRUENAS_API_KEY = "key"
    settings.TRUENAS_API_USER = "hvault"
    with pytest.raises(ProviderUnavailable) as exc:
        TrueNASProvider()._ws_url()
    assert "must start with https://" in str(exc.value)


def test_the_transport_is_always_wss_never_plaintext(settings):
    """A security control, not a preference: TrueNAS 25.04+ REVOKES a
    user-linked key the moment it is presented over plain HTTP (verified live,
    2026-08-12 — two keys destroyed proving it). So an http:// TRUENAS_HOST
    still yields a wss:// URL, and its port — which belongs to the plaintext
    listener — is dropped; an https:// host keeps host:port verbatim."""
    settings.TRUENAS_API_KEY = "key"
    settings.TRUENAS_API_USER = "hvault"

    settings.TRUENAS_HOST = "http://truenas.test"
    assert TrueNASProvider()._ws_url() == "wss://truenas.test/api/current"
    settings.TRUENAS_HOST = "http://truenas.test:80"
    assert TrueNASProvider()._ws_url() == "wss://truenas.test/api/current"
    settings.TRUENAS_HOST = "https://truenas.test:8443"
    assert TrueNASProvider()._ws_url() == "wss://truenas.test:8443/api/current"


def test_truenas_satisfies_the_provider_protocol():
    """The runtime_checkable seam still holds for a guest-less provider —
    `report()` is intentionally NOT on the Protocol, so this stays true."""
    assert isinstance(TrueNASProvider(), Provider)


def test_a_configured_truenas_is_listed(provider):
    assert any(isinstance(p, TrueNASProvider) for p in configured_providers())


# --- status() --------------------------------------------------------------


def test_status_is_usable_when_login_and_ping_answer(provider, monkeypatch):
    box = install(monkeypatch, provider, FakeTrueNAS())
    status = provider.status()
    assert status.usable is True
    assert status.reachable and status.permitted
    # The login is the FIRST call on the session — the key is never presented
    # to an unauthenticated method — and the session is not leaked.
    assert box.calls[0] == "auth.login_ex"
    assert box.closed == 1


def test_a_refused_login_is_reachable_but_not_permitted(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS(login_response="AUTH_ERR"))
    status = provider.status()
    assert status.configured and status.reachable
    assert status.permitted is False
    assert status.usable is False
    assert "TRUENAS_API_USER" in status.detail


def test_an_expired_login_names_the_plaintext_revocation_trap(provider, monkeypatch):
    """EXPIRED covers revoked keys, and the by-far most likely cause is 25.04
    revoking a key it saw over plain HTTP — the detail must say so, or the
    operator is left checking an expiry date that reads "Never" (which is
    exactly how the live debugging went before this was understood)."""
    install(monkeypatch, provider, FakeTrueNAS(login_response="EXPIRED"))
    status = provider.status()
    assert status.permitted is False
    assert "revoke" in status.detail.lower()
    assert "plain HTTP" in status.detail


def test_a_connection_error_is_not_reachable(provider, monkeypatch):
    install_unreachable(monkeypatch, provider)
    status = provider.status()
    assert status.reachable is False
    assert status.usable is False


# --- The guest-less seam ---------------------------------------------------


def test_guests_is_always_empty_and_never_raises(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    assert provider.guests() == []


def test_infrastructure_is_none(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    assert provider.infrastructure() is None


# --- report(): pools -------------------------------------------------------


def test_report_reads_pools_with_capacity_and_health(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    report = provider.report()
    tank = next(p for p in report.pools if p.name == "tank")
    assert tank.status == "ONLINE"
    assert tank.healthy is True
    assert tank.total_bytes == 10 * TIB
    assert tank.used_bytes == 6 * TIB
    assert tank.free_bytes == 4 * TIB


def test_a_degraded_pool_is_unhealthy(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    backup = next(p for p in provider.report().pools if p.name == "backup")
    assert backup.status == "DEGRADED"
    assert backup.healthy is False


def test_a_running_scrub_is_reported(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    backup = next(p for p in provider.report().pools if p.name == "backup")
    assert backup.scan_function == "SCRUB"
    assert backup.scan_state == "SCANNING"
    assert backup.scan_percentage == 42.5


def test_pool_capacity_falls_back_to_the_root_dataset(provider, monkeypatch):
    """The `backup` pool omits size/allocated/free on its /pool row, so capacity
    must come from the dataset — the older-SCALE path."""
    install(monkeypatch, provider, FakeTrueNAS())
    backup = next(p for p in provider.report().pools if p.name == "backup")
    assert backup.used_bytes == 3 * TIB
    assert backup.total_bytes == 4 * TIB  # used 3 + available 1
    assert backup.free_bytes == 1 * TIB


# --- report(): disks -------------------------------------------------------


def test_disk_serials_are_never_carried(provider, monkeypatch):
    """The panel is public — a disk must not be able to leak its serial."""
    install(monkeypatch, provider, FakeTrueNAS())
    for disk in provider.report().disks:
        assert not hasattr(disk, "serial")
        assert "SECRET" not in repr(disk)
        assert "CCCC" not in repr(disk)


def test_disk_pool_membership_comes_from_topology_not_the_disk_row(provider, monkeypatch):
    """The disk↔pool finding (verified against 25.04, 2026-08-12): `/disk` leaves
    each row's own `pool` field null, so membership is derived from each pool's
    topology tree. A provider reading `row["pool"]` would report every disk as
    unassigned; this pins the derived mapping instead."""
    install(monkeypatch, provider, FakeTrueNAS())
    disks = {d.name: d for d in provider.report().disks}
    assert disks["sda"].pool == "tank"
    assert disks["sdb"].pool == "tank"
    assert disks["sdc"].pool == "backup"


def test_a_disk_in_no_pool_reports_empty_membership(provider, monkeypatch):
    """A spare or unassigned drive is in no pool's topology → "" (not a crash,
    not a stale label from some other pool)."""
    from .truenas_fakes import DEFAULT_DISKS, disk

    box = FakeTrueNAS(disks=DEFAULT_DISKS + [disk("sdz", "SPARE-MODEL", serial="SP-1")])
    install(monkeypatch, provider, box)
    sdz = next(d for d in provider.report().disks if d.name == "sdz")
    assert sdz.pool == ""


def test_a_failing_smart_disk_surfaces(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    sdc = next(d for d in provider.report().disks if d.name == "sdc")
    assert sdc.smart_status == "FAIL"
    sda = next(d for d in provider.report().disks if d.name == "sda")
    assert sda.smart_status == "PASS"


def test_disk_temperatures_are_merged(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    sda = next(d for d in provider.report().disks if d.name == "sda")
    assert sda.temperature_c == 34


def test_a_temperature_read_failure_does_not_fail_the_report(provider, monkeypatch):
    """A spun-down disk that will not answer must not blank the pool beside it."""
    install(monkeypatch, provider, FakeTrueNAS(temperatures_fail=True))
    report = provider.report()
    assert report.pools  # pools still read
    assert all(d.temperature_c is None for d in report.disks)


# --- report(): system, backups, alerts -------------------------------------


def test_report_reads_system(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    system = provider.report().system
    assert system.mem_total_bytes == 64 * GIB
    assert system.load_avg == 0.52
    assert system.cores == 12
    assert system.uptime_s == 864000


def test_report_reads_backup_task_outcomes(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    backups = provider.report().backups
    cloud = next(b for b in backups if b.kind == "cloudsync")
    assert cloud.state == "FAILED"
    repl = next(b for b in backups if b.kind == "replication")
    assert repl.state == "FINISHED"


def test_report_reads_active_alerts_and_filters_dismissed(provider, monkeypatch):
    install(monkeypatch, provider, FakeTrueNAS())
    alerts = provider.report().alerts
    assert [a.key for a in alerts] == ["alert-1"]
    assert alerts[0].level == "CRITICAL"
    assert "sdc" in alerts[0].message
    # `klass` is captured as the coarsened, public-safe category (TN3 shows this
    # instead of the untrusted `formatted` message on the public panel).
    assert alerts[0].category == "ZpoolCapacityNotice"


def test_report_payload_is_json_shaped(provider, monkeypatch):
    """`as_payload()` is what the snapshot caches — it must be plain data."""
    install(monkeypatch, provider, FakeTrueNAS())
    payload = provider.report().as_payload()
    assert set(payload) == {"system", "pools", "disks", "backups", "alerts"}
    assert payload["pools"][0]["name"] == "tank"
    # datetimes serialised to isoformat strings, not datetime objects
    assert isinstance(payload["alerts"][0]["at"], str)


def test_report_raises_when_the_box_is_unreachable(provider, monkeypatch):
    install_unreachable(monkeypatch, provider)
    with pytest.raises(ProviderUnavailable):
        provider.report()


def test_a_smart_backend_failure_degrades_to_unknown(provider, monkeypatch):
    """The live 25.04.2.6 case: `smart.test.results` fails SERVER-side (the
    middleware's own response schema rejects a field it emits). Every disk reads
    SMART-unknown and nothing else is lost — the authoritative failing-drive
    signal stays pool health plus the alert feed."""
    install(monkeypatch, provider, FakeTrueNAS(smart_fail=True))
    report = provider.report()
    assert report.pools and report.disks
    assert all(d.smart_status == "" for d in report.disks)


def test_a_jsonrpc_error_never_quotes_the_error_data(provider):
    """TrueNAS embeds the failing call's params — INCLUDING THE API KEY — in
    `error.data.trace` (observed live, 2026-08-12). The text that reaches an
    exception, a snapshot detail, or a log line is built from the code and
    `errname` alone; everything else in the error object is dropped unread."""
    error = {
        "code": -32001,
        "message": "Method call error",
        "data": {
            "errname": "EINVAL",
            "reason": "AUTH: something",
            "trace": {"params": [{"api_key": "1-SUPERSECRET", "username": "hvault"}]},
        },
    }
    text = _rpc_error_text("auth.login_ex", error)
    assert "SUPERSECRET" not in text
    assert "reason" not in text and "AUTH: something" not in text
    assert "EINVAL" in text and "-32001" in text
