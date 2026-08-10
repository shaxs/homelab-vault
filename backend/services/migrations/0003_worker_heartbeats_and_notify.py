"""V1.3 — the deadman's heartbeat registry, `notify_enabled`, and the change
that makes "applies to every service without its own interval" describable.

`health_interval_s` and `health_fail_threshold` become nullable, and **null means
follow the global setting** (operator ruling, 2026-08-05). Until now every row
was stamped with a value at insert, so no service could *be* one "without its own
interval" — the settings screen described a state the schema made unreachable.

Existing rows are unstamped only where the stored value equals the deploy-time
default that stamped them, which is indistinguishable from never having been
chosen. Anything else is a number somebody typed, and stays pinned. Reversible:
going back fills the nulls in again, so the column can take its NOT NULL back.
"""

from django.db import migrations, models

#: The deploy-time defaults as they stood when this migration shipped
#: (2026-08-05), frozen (audit S17). The first version filtered on
#: `settings.HEALTH_DEFAULT_*`, which made the rewrite depend on the
#: environment it RUNS in rather than on the values that stamped the rows:
#: change the default to 120 before upgrading and every 60-second row stays
#: permanently pinned instead of following the settings table. A data
#: migration describes history, and history does not change with `.env`.
STAMPED_INTERVAL_S = 60
STAMPED_FAIL_THRESHOLD = 3


def unstamp_untouched_defaults(apps, schema_editor):
    Service = apps.get_model("services", "Service")
    Service.objects.filter(health_interval_s=STAMPED_INTERVAL_S).update(
        health_interval_s=None
    )
    Service.objects.filter(
        health_fail_threshold=STAMPED_FAIL_THRESHOLD
    ).update(health_fail_threshold=None)


def restamp_defaults(apps, schema_editor):
    """Reverse: a nullable column going back to NOT NULL cannot hold nulls."""
    Service = apps.get_model("services", "Service")
    Service.objects.filter(health_interval_s=None).update(
        health_interval_s=STAMPED_INTERVAL_S
    )
    Service.objects.filter(health_fail_threshold=None).update(
        health_fail_threshold=STAMPED_FAIL_THRESHOLD
    )


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0002_settings_table'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkerHeartbeat',
            fields=[
                ('name', models.TextField(primary_key=True, serialize=False)),
                ('interval_s', models.IntegerField()),
                ('started_at', models.DateTimeField()),
                ('last_beat_at', models.DateTimeField()),
                ('cycles', models.BigIntegerField(default=0)),
                ('state', models.TextField(choices=[('ok', 'Ok'), ('stalled', 'Stalled')], default='ok')),
                ('stalled_since', models.DateTimeField(blank=True, null=True)),
                ('alerted_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'worker_heartbeats',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='service',
            name='health_consec_successes',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='service',
            name='notify_enabled',
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name='service',
            name='health_fail_threshold',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='service',
            name='health_interval_s',
            field=models.IntegerField(blank=True, null=True),
        ),
        # Last, so that on reverse it runs FIRST — the nulls have to go before
        # the columns take their NOT NULL back.
        migrations.RunPython(unstamp_untouched_defaults, restamp_defaults),
    ]
