import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _copy_tree_without_links(source, destination):
    source = Path(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for root, directories, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative = root_path.relative_to(source)
        target_root = destination / relative
        target_root.mkdir(parents=True, exist_ok=True)
        for name in list(directories):
            item = root_path / name
            if item.is_symlink():
                raise ValueError(f"Символічні посилання в каталозі даних не підтримуються: {item}")
            if not item.is_dir():
                raise ValueError(f"Некоректний елемент каталогу даних: {item}")
            (target_root / name).mkdir(exist_ok=True)
        for name in files:
            item = root_path / name
            if item.is_symlink() or not stat.S_ISREG(item.stat(follow_symlinks=False).st_mode):
                raise ValueError(f"Не звичайний файл у каталозі даних: {item}")
            shutil.copy2(item, target_root / name)


def _copy_sqlite_database(source, destination):
    source = Path(source)
    destination = Path(destination)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
            result = destination_db.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(
                    f"Копія SQLite не пройшла integrity_check: {result[0] if result else 'немає відповіді'}"
                )


class Command(BaseCommand):
    help = (
        "Безпечно скопіювати старі db.sqlite3, media/ та backups/ "
        "із каталогу програми до постійного каталогу даних."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Підтвердити копіювання. Без цього команда лише показує план.",
        )

    def handle(self, *args, **options):
        app_dir = Path(settings.BASE_DIR).resolve()
        data_dir = Path(settings.AUTOZAPAZ_DATA_DIR).expanduser().resolve()
        if app_dir == data_dir:
            self.stdout.write(self.style.SUCCESS(
                f"Дані вже зберігаються в каталозі програми даних: {data_dir}"
            ))
            return

        sources = {
            "db.sqlite3": app_dir / "db.sqlite3",
            "media": app_dir / "media",
            "backups": app_dir / "backups",
        }
        present = {name: path for name, path in sources.items() if path.exists() or path.is_symlink()}
        if any(path.is_symlink() for path in present.values()):
            raise CommandError("Символічні посилання на рівні старих db.sqlite3/media/backups не підтримуються.")
        if not present:
            raise CommandError(
                f"Старих даних у {app_dir} не знайдено. Немає що переносити."
            )
        if "db.sqlite3" not in present:
            raise CommandError(
                "Не знайдено стару db.sqlite3. Перенесення без бази даних не виконується."
            )

        conflicts = [str(data_dir / name) for name in present if (data_dir / name).exists()]
        if conflicts:
            raise CommandError(
                "У каталозі даних уже існують цільові файли/каталоги; нічого не перезаписано: "
                + ", ".join(conflicts)
            )

        self.stdout.write("План перенесення (оригінали залишаться на місці):")
        self.stdout.write(f"  Джерело: {app_dir}")
        self.stdout.write(f"  Каталог даних: {data_dir}")
        for name, path in present.items():
            self.stdout.write(f"  {name}: {path} -> {data_dir / name}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "Це лише аудит. Зупиніть сервер і повторіть команду з --apply для копіювання."
            ))
            return

        data_dir.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".autozapaz-migrate-", dir=data_dir.parent))
        installed = []
        try:
            for name, source in present.items():
                staged = stage / name
                if name == "db.sqlite3":
                    _copy_sqlite_database(source, staged)
                else:
                    _copy_tree_without_links(source, staged)

            # Recheck all destinations immediately before publishing staged files.
            conflicts = [str(data_dir / name) for name in present if (data_dir / name).exists()]
            if conflicts:
                raise ValueError(
                    "Цільові файли з'явилися під час копіювання; нічого не перезаписано: "
                    + ", ".join(conflicts)
                )
            for name in present:
                destination = data_dir / name
                os.replace(stage / name, destination)
                installed.append(destination)
        except Exception as exc:
            for destination in reversed(installed):
                if destination.is_dir():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    destination.unlink(missing_ok=True)
            raise CommandError(
                f"Перенесення не завершено; створені цільові копії прибрано, оригінали збережені: {exc}"
            ) from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        self.stdout.write(self.style.SUCCESS(
            "Копіювання завершено. Старі файли не видалені. Перевірте запуск, товари, продажі та медіафайли "
            "в новій інсталяції, перш ніж вручну прибирати старі копії."
        ))
