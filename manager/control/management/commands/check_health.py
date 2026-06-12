"""
Periodic health check — intended to run from cron, e.g. every 5 minutes:

    */5 * * * * /srv/djmanager/venv/bin/python /srv/djmanager/manage.py check_health

Records a server-resource sample and alerts (via NotificationSettings) on any
managed service that is not running. Safe to run without cron too — the
dashboard records samples on its own; this command adds the down-service watch.
"""
from django.core.management.base import BaseCommand

from control.models import HealthSample
from control.utils import (
    get_all_projects, get_server_stats, get_service_status,
    send_notification, EVENT_SERVICE_DOWN,
)


class Command(BaseCommand):
    help = 'Record a health sample and alert on down services.'

    def handle(self, *args, **options):
        stats = get_server_stats()
        HealthSample.record(stats)
        HealthSample.prune(keep_days=7)

        down = []
        for proj in get_all_projects():
            name = proj.get('PROJECTNAME')
            if not name:
                continue
            # get_service_status returns 'active' | 'inactive' | 'failed' | 'unknown'.
            # 'unknown' is excluded: it usually means the unit doesn't exist (e.g.
            # the manager pseudo-project), not a crash.
            status = get_service_status(name)
            if status in ('inactive', 'failed'):
                down.append((name, status))

        if down:
            body = '\n'.join(f'- {n}: {s}' for n, s in down)
            send_notification(
                f'{len(down)} Dienst(e) nicht aktiv',
                f'Folgende Dienste laufen nicht:\n\n{body}',
                event_type=EVENT_SERVICE_DOWN,
            )
            self.stdout.write(self.style.WARNING(f'{len(down)} Dienst(e) down'))
        else:
            self.stdout.write(self.style.SUCCESS('Alle Dienste aktiv'))
