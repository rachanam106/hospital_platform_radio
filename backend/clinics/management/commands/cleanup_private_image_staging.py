from datetime import timedelta

from django.core.management.base import BaseCommand

from clinics.private_storage import PrivateImageStorage


class Command(BaseCommand):
    help = "Remove private image upload staging chunks older than 24 hours."

    def handle(self, *args, **options):
        storage = PrivateImageStorage()
        staged = storage.cleanup_staging(older_than=timedelta(hours=24))
        orphaned = storage.cleanup_orphans(older_than=timedelta(hours=24))
        self.stdout.write(self.style.SUCCESS(f"Removed {staged} stale upload staging file(s) and {orphaned} orphaned image blob(s)."))
