from django.core.management.base import BaseCommand

from apps.core.dbcheck import database_warnings
from apps.core.roles import ensure_groups
from apps.sources.seed import seed_all


class Command(BaseCommand):
    help = "Create the Admin/Operator roles and example seed data. Safe to run repeatedly."

    def add_arguments(self, parser):
        parser.add_argument("--no-seed", action="store_true", help="Only create roles")

    def handle(self, *args, **options):
        ensure_groups()
        from apps.core.roles import ADMIN_GROUP, OPERATOR_GROUP

        self.stdout.write(self.style.SUCCESS(f"Roles ready: '{ADMIN_GROUP}', '{OPERATOR_GROUP}'"))
        if not options["no_seed"]:
            result = seed_all()
            self.stdout.write(self.style.SUCCESS(f"Seed data: {result}"))
        for warning in database_warnings():
            self.stdout.write(self.style.WARNING("WARNING: " + warning))
