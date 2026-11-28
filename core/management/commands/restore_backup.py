from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.admin_tools import restore_backup_archive


class Command(BaseCommand):
    help = "Відновити перевірену копію в новий ізольований каталог, не змінюючи робочу БД."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="Шлях до ZIP-резервної копії.")
        parser.add_argument("target_dir", help="Новий каталог для відновлення; він не повинен існувати.")
        parser.add_argument("--apply", action="store_true", help="Підтвердити створення нового каталогу відновлення.")

    def handle(self, *args, **options):
        if not options["apply"]:
            raise CommandError("Для створення каталогу відновлення явно додайте --apply.")
        archive_path = Path(options["archive"]).expanduser()
        if not archive_path.is_file():
            raise CommandError(f"Файл резервної копії не знайдено: {archive_path}")
        try:
            report = restore_backup_archive(archive_path, options["target_dir"])
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f"Копію відновлено в ізольований каталог: {report['directory']}; "
            f"медіафайлів: {report['media_files']}. "
            "Робоча база та MEDIA_ROOT не змінювалися."
        ))
