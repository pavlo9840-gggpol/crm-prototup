from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.admin_tools import verify_backup_archive


class Command(BaseCommand):
    help = "Перевірити ZIP-резервну копію АвтоЗапас та цілісність SQLite."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="Шлях до backup-YYYYMMDD-HHMMSS.zip")

    def handle(self, *args, **options):
        archive_path = Path(options["archive"]).expanduser()
        if not archive_path.is_file():
            raise CommandError(f"Файл резервної копії не знайдено: {archive_path}")
        try:
            report = verify_backup_archive(archive_path)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            "Резервна копія справна: "
            f"SQLite integrity={report['integrity']}; "
            f"файлів={report['files']}; "
            f"медіафайлів={report['media_files']}; "
            f"маніфест={'так' if report['manifest'] else 'ні (legacy архів)'}"
        ))
