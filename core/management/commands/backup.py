from django.core.management.base import BaseCommand
from core.admin_tools import save_backup


class Command(BaseCommand):
    help = "Save a ZIP backup of the database (+media) into ./backups and keep the last N copies (cron-friendly)"

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=14)

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS(f"Saved {save_backup(opts['keep'])}"))
