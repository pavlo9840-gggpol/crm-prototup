import sqlite3
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


class LocalDataMigrationCommandTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app_dir = root / "application"
        self.data_dir = root / "user-data"
        self.app_dir.mkdir()
        self.data_dir.mkdir()
        self.database = self.app_dir / "db.sqlite3"
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO fixture(value) VALUES ('legacy-shop-data')")
        media = self.app_dir / "media" / "products"
        media.mkdir(parents=True)
        (media / "pump.jpg").write_bytes(b"product-image-fixture")
        backups = self.app_dir / "backups"
        backups.mkdir()
        (backups / "backup-20261117-120000.zip").write_bytes(b"legacy-backup-fixture")
        self.settings = override_settings(
            BASE_DIR=self.app_dir,
            AUTOZAPAZ_DATA_DIR=self.data_dir,
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def test_default_run_is_read_only_and_shows_migration_plan(self):
        import io

        output = io.StringIO()
        call_command("migrate_local_data", stdout=output)
        self.assertTrue(self.database.is_file())
        self.assertFalse((self.data_dir / "db.sqlite3").exists())
        self.assertIn("лише аудит", output.getvalue())

    def test_apply_copies_sqlite_media_and_backups_without_deleting_sources(self):
        import io\n\n        call_command("migrate_local_data", "--apply", stdout=io.StringIO())
        target_db = self.data_dir / "db.sqlite3"
        self.assertTrue(target_db.is_file())
        self.assertEqual(
            (self.data_dir / "media" / "products" / "pump.jpg").read_bytes(),
            b"product-image-fixture",
        )
        self.assertEqual(
            (self.data_dir / "backups" / "backup-20261117-120000.zip").read_bytes(),
            b"legacy-backup-fixture",
        )
        with sqlite3.connect(target_db) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                db.execute("SELECT value FROM fixture").fetchone()[0],
                "legacy-shop-data",
            )
        self.assertTrue(self.database.is_file())
        self.assertTrue((self.app_dir / "media" / "products" / "pump.jpg").is_file())

    def test_existing_destination_is_never_overwritten(self):
        target = self.data_dir / "db.sqlite3"
        target.write_bytes(b"keep-this-file")
        with self.assertRaisesRegex(CommandError, "уже існують"):
            call_command("migrate_local_data", "--apply")
        self.assertEqual(target.read_bytes(), b"keep-this-file")
        self.assertTrue(self.database.is_file())

    def test_corrupt_legacy_database_does_not_publish_partial_data(self):
        self.database.write_bytes(b"not-a-sqlite-database")
        with self.assertRaises(CommandError):
            call_command("migrate_local_data", "--apply")
        self.assertFalse((self.data_dir / "db.sqlite3").exists())
        self.assertFalse((self.data_dir / "media").exists())
        self.assertTrue(self.database.is_file())
        self.assertTrue((self.app_dir / "media").is_dir())

    def test_symlink_in_legacy_media_is_rejected(self):
        media = self.app_dir / "media"
        (media / "outside-link").symlink_to(self.app_dir / "db.sqlite3")
        with self.assertRaises(CommandError):
            call_command("migrate_local_data", "--apply")
        self.assertFalse((self.data_dir / "db.sqlite3").exists())
        self.assertTrue(self.database.is_file())
