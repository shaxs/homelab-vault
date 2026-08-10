"""Data-migration honesty — audit S17.

Migration 0003's unstamping pass rewrites rows, and the first version filtered
on the LIVE `settings.HEALTH_DEFAULT_*` values — so what it did depended on the
environment it ran in, not on the values that stamped the rows. On a fresh
install it is a no-op against an empty table; the failure mode is the in-place
upgrade of an existing install whose deploy-time default has moved.

These tests drive the real executor across 0002 <-> 0003 with populated rows
and deliberately *changed* live settings, because calling the migration
functions directly would not catch a regression that reintroduces a
settings-read anywhere else in the migration.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

APP = "services"
BEFORE = (APP, "0002_settings_table")
AFTER = (APP, "0003_worker_heartbeats_and_notify")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    executor.loader.build_graph()
    return executor


@pytest.mark.django_db(transaction=True)
def test_0003_unstamps_by_the_historical_defaults_not_the_live_settings(settings):
    """A row stamped with the 2026-08-05 default (60s/3) unstamps; a row that
    happens to equal the CURRENT deployment's default does not. Under the
    settings-reading version both directions were wrong the moment the
    deployment default moved."""
    settings.HEALTH_DEFAULT_INTERVAL_S = 120
    settings.HEALTH_DEFAULT_FAIL_THRESHOLD = 5
    try:
        executor = _migrate(BEFORE)
        OldService = executor.loader.project_state(BEFORE).apps.get_model(
            APP, "Service"
        )
        OldService.objects.create(
            slug="stamped-with-the-2026-default",
            name="a",
            launch_url="https://a.lab.local",
            health_interval_s=60,
            health_fail_threshold=3,
            health_timeout_ms=5000,
        )
        OldService.objects.create(
            slug="pinned-at-todays-default",
            name="b",
            launch_url="https://b.lab.local",
            health_interval_s=120,
            health_fail_threshold=5,
            health_timeout_ms=5000,
        )

        executor = _migrate(AFTER)

        Service = executor.loader.project_state(AFTER).apps.get_model(
            APP, "Service"
        )
        by_slug = {
            s.slug: (s.health_interval_s, s.health_fail_threshold)
            for s in Service.objects.all()
        }
        # 60/3 was the value the old default stamped: indistinguishable from
        # never having been chosen, so it unstamps to follow-the-global.
        assert by_slug["stamped-with-the-2026-default"] == (None, None)
        # 120/5 equals the LIVE default — under the old code that unstamped
        # it too, silently unpinning a number somebody typed.
        assert by_slug["pinned-at-todays-default"] == (120, 5)
    finally:
        # Whatever happened above, the next test meets the schema at head.
        call_command("migrate", APP, verbosity=0)


@pytest.mark.django_db(transaction=True)
def test_0003_reverse_restamps_with_the_same_frozen_values(settings):
    """The reverse direction fills nulls before the columns take NOT NULL
    back, and it must use the same frozen constants — restamping with a moved
    live default would write history that never happened."""
    settings.HEALTH_DEFAULT_INTERVAL_S = 120
    try:
        executor = _migrate(AFTER)
        Service = executor.loader.project_state(AFTER).apps.get_model(
            APP, "Service"
        )
        Service.objects.create(
            slug="following-the-global",
            name="c",
            launch_url="https://c.lab.local",
            health_interval_s=None,
            health_fail_threshold=None,
            health_timeout_ms=5000,
        )

        executor = _migrate(BEFORE)

        OldService = executor.loader.project_state(BEFORE).apps.get_model(
            APP, "Service"
        )
        row = OldService.objects.get(slug="following-the-global")
        assert row.health_interval_s == 60
        assert row.health_fail_threshold == 3
    finally:
        call_command("migrate", APP, verbosity=0)
