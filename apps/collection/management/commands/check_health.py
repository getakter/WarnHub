from django.core.management.base import BaseCommand

from apps.collection.health import refresh_all_health


class Command(BaseCommand):
    help = "Recompute health (healthy / silent / degraded / failing) for every feed."

    def handle(self, *args, **options):
        self.stdout.write(str(refresh_all_health()))
