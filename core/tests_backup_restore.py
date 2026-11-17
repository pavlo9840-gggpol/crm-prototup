import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from core import admin_tools


class BackupArchiveTests(TransactionTestCase):
    def _make_archive(self, media_root):
        with override_settings(MEDIA_ROOT=media_root):
            return admin_tools.make_backup_zip()

    def test_backup_contains_database_media_and_verified_sha256_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir) / "media"
            media_root.mkdir()
            (media_root / "photo.txt").write_text("autozapaz media fixture", encoding="utf-8")

            data = self._make_archive(media_root)
            report = admin_tools.verify_backup_archive(data)

            self.assertEqual(report["integrity"], "ok")
            self.assertTrue(report["manifest"])
            self.assertEqual(report["media_files"], 1)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertIn("db.sqlite3", manifest["files"])
                self.assertIn("media/photo.txt", manifest["files"])

    def test_manifest_detects_modified_media_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir) / "media"
            media_root.mkdir()
            (media_root / "photo.txt").write_text("original", encoding="utf-8")
            data = self._make_archive(media_root)

        corrupted = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data), "r") as source, zipfile.ZipFile(corrupted, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "media/photo.txt":
                    payload = b"tampered"
                target.writestr(name, payload)

        with self.assertRaisesRegex(ValueError, "Контрольна сума не збігається"):
            admin_tools.verify_backup_archive(corrupted.getvalue())

    def test_verifier_rejects_path_traversal_entries(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with zipfile.ZipFile(io.BytesIO(data), "r") as source:
            database = source.read("db.sqlite3")
        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("db.sqlite3", database)
            archive.writestr("../outside.txt", "not allowed")

        with self.assertRaisesRegex(ValueError, "Небезпечний шлях"):
            admin_tools.verify_backup_archive(malicious.getvalue())

    def test_save_backup_is_atomic_and_retention_keeps_latest_requested_count(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(admin_tools, "BACKUP_DIR", Path(temp_dir)), patch.object(
            admin_tools, "make_backup_zip", return_value=data
        ):
            for name in ("backup-20000101-000000.zip", "backup-20000102-000000.zip", "backup-20000103-000000.zip"):
                (Path(temp_dir) / name).write_bytes(data)

            created = admin_tools.save_backup(keep=2)
            saved = sorted(Path(temp_dir).glob("backup-*.zip"))
            self.assertTrue(created.is_file())
            self.assertEqual(len(saved), 2)
            self.assertFalse(any(path.name.startswith(".autozapaz-backup-") for path in Path(temp_dir).iterdir()))

    def test_save_backup_rejects_zero_retention(self):
        with self.assertRaisesRegex(ValueError, "не меншою за 1"):
            admin_tools.save_backup(keep=0)

    def test_verify_backup_management_command_accepts_valid_archive(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "backup.zip"
            path.write_bytes(data)
            call_command("verify_backup", str(path), stdout=io.StringIO())

    def test_verify_backup_management_command_rejects_missing_file(self):
        with self.assertRaises(CommandError):
            call_command("verify_backup", "/definitely/missing/autozapaz-backup.zip")


class BackupLegacyCompatibilityTests(SimpleTestCase):
    def test_archive_with_non_sqlite_database_is_rejected_cleanly(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("db.sqlite3", b"this is not a sqlite database")
        with self.assertRaisesRegex(ValueError, "не є коректною SQLite-базою"):
            admin_tools.verify_backup_archive(data.getvalue())

    def test_legacy_zip_without_manifest_remains_supported(self):
        # A minimal valid SQLite database is supplied by the test helper below.
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as database:
                database.execute("CREATE TABLE legacy_fixture (id INTEGER PRIMARY KEY)")
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.write(db_path, "db.sqlite3")

        report = admin_tools.verify_backup_archive(data.getvalue())
        self.assertFalse(report["manifest"])
        self.assertEqual(report["integrity"], "ok")
