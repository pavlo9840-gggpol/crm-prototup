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

    def test_backup_includes_committed_wal_database_pages(self):
        database_connection = admin_tools.connection
        database_name = database_connection.settings_dict.get("NAME")
        if not database_name or str(database_name) == ":memory:":
            self.skipTest("WAL snapshot test requires a file-backed SQLite test database")

        table_name = "backup_wal_snapshot_fixture"
        with database_connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"CREATE TABLE {table_name} (value TEXT NOT NULL)")
            cursor.execute(f"INSERT INTO {table_name} (value) VALUES (%s)", ["committed-in-wal"])

        try:
            data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
            with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
                with tempfile.TemporaryDirectory(prefix="autozapaz-wal-test-") as temp_dir:
                    snapshot = Path(temp_dir) / "snapshot.sqlite3"
                    snapshot.write_bytes(archive.read("db.sqlite3"))
                    import sqlite3
                    with sqlite3.connect(snapshot) as backup_db:
                        value = backup_db.execute(
                            f"SELECT value FROM {table_name}"
                        ).fetchone()[0]
                    self.assertEqual(value, "committed-in-wal")
        finally:
            with database_connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                cursor.execute("PRAGMA journal_mode=DELETE")

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


    def test_manifest_rejects_boolean_version_and_size(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with zipfile.ZipFile(io.BytesIO(data), "r") as source:
            files = {name: source.read(name) for name in source.namelist()}

        for field, invalid_value in (("format_version", True), ("size", True)):
            with self.subTest(field=field):
                manifest = json.loads(files["manifest.json"])
                if field == "format_version":
                    manifest[field] = invalid_value
                else:
                    manifest["files"]["db.sqlite3"][field] = invalid_value
                malicious = io.BytesIO()
                with zipfile.ZipFile(malicious, "w") as archive:
                    for name, payload in files.items():
                        archive.writestr(
                            name,
                            json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                            if name == "manifest.json" else payload,
                        )
                with self.assertRaises(ValueError):
                    admin_tools.verify_backup_archive(malicious.getvalue())

    def test_verifier_rejects_archive_over_total_uncompressed_size_limit(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with patch.object(admin_tools, "MAX_BACKUP_TOTAL_SIZE", 1):
            with self.assertRaisesRegex(ValueError, "Розпакований розмір"):
                admin_tools.verify_backup_archive(data)

    def test_verifier_rejects_too_many_archive_files(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("db.sqlite3", b"placeholder")
            archive.writestr("media/one.txt", b"x")
        with patch.object(admin_tools, "MAX_BACKUP_ARCHIVE_FILES", 1):
            with self.assertRaisesRegex(ValueError, "надто багато файлів"):
                admin_tools.verify_backup_archive(data.getvalue())

    def test_verifier_rejects_oversized_manifest_before_parsing(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with patch.object(admin_tools, "MAX_BACKUP_MANIFEST_SIZE", 1):
            with self.assertRaisesRegex(ValueError, "Маніфест.*дозволений розмір"):
                admin_tools.verify_backup_archive(data)

    def test_manifest_rejects_duplicate_json_keys(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        forged = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data), "r") as source, zipfile.ZipFile(forged, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "manifest.json":
                    # Duplicate a top-level key while keeping the document valid JSON.
                    payload = b'{"format_version":1,' + payload[1:]
                target.writestr(name, payload)

        with self.assertRaisesRegex(ValueError, "повторюваний ключ: format_version"):
            admin_tools.verify_backup_archive(forged.getvalue())

    def test_manifest_must_cover_every_payload_file(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        forged = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data), "r") as source, zipfile.ZipFile(forged, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["files"].pop("db.sqlite3")
                    payload = json.dumps(manifest).encode("utf-8")
                target.writestr(name, payload)

        with self.assertRaisesRegex(ValueError, "не містить контрольної суми db.sqlite3"):
            admin_tools.verify_backup_archive(forged.getvalue())

    def test_manifest_rejects_unknown_version_and_malformed_digest(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with zipfile.ZipFile(io.BytesIO(data), "r") as source:
            members = {name: source.read(name) for name in source.namelist()}

        for mutate, expected_error in (
            (lambda manifest: manifest.update(format_version=99), "Непідтримувана версія"),
            (lambda manifest: manifest["files"]["db.sqlite3"].update(sha256="not-a-hash"), "SHA-256"),
        ):
            altered = dict(members)
            manifest = json.loads(altered["manifest.json"])
            mutate(manifest)
            altered["manifest.json"] = json.dumps(manifest).encode("utf-8")
            archive_data = io.BytesIO()
            with zipfile.ZipFile(archive_data, "w") as archive:
                for name, payload in altered.items():
                    archive.writestr(name, payload)
            with self.assertRaisesRegex(ValueError, expected_error):
                admin_tools.verify_backup_archive(archive_data.getvalue())

    def test_restore_rejects_unexpected_archive_members_without_creating_target(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with zipfile.ZipFile(io.BytesIO(data), "r") as source:
            database = source.read("db.sqlite3")
            manifest = source.read("manifest.json")
            readme = source.read("README.txt")
        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("db.sqlite3", database)
            archive.writestr("manifest.json", manifest)
            archive.writestr("README.txt", readme)
            archive.writestr("unexpected.txt", "should not be restored")

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "restore"
            with self.assertRaisesRegex(ValueError, "Склад файлів не збігається з маніфестом"):
                admin_tools.restore_backup_archive(malicious.getvalue(), target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(temp_dir).glob(".autozapaz-restore-*")), [])

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

    def test_save_backup_streams_archive_without_building_full_bytes_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            admin_tools, "BACKUP_DIR", Path(temp_dir)
        ), patch.object(
            admin_tools, "make_backup_zip", side_effect=AssertionError("in-memory path must not be used")
        ):
            created = admin_tools.save_backup(keep=1)
            self.assertTrue(created.is_file())
            report = admin_tools.verify_backup_archive(created)
            self.assertEqual(report["integrity"], "ok")
            self.assertTrue(report["manifest"])

    def test_save_backup_rejects_zero_retention(self):
        with self.assertRaisesRegex(ValueError, "не меншою за 1"):
            admin_tools.save_backup(keep=0)

    def test_verify_backup_management_command_accepts_valid_archive(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "backup.zip"
            path.write_bytes(data)
            call_command("verify_backup", str(path), stdout=io.StringIO())

    def test_restore_creates_verified_database_and_media_in_new_directory(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_root = root / "source-media"
            media_root.mkdir()
            (media_root / "logo.txt").write_text("autozapaz-logo", encoding="utf-8")
            data = self._make_archive(media_root)
            target = root / "restored-installation"

            report = admin_tools.restore_backup_archive(data, target)

            self.assertEqual(report["directory"], target.resolve())
            self.assertTrue((target / "db.sqlite3").is_file())
            self.assertEqual((target / "media" / "logo.txt").read_text(encoding="utf-8"), "autozapaz-logo")
            self.assertEqual(report["media_files"], 1)
            database = sqlite3.connect(target / "db.sqlite3")
            try:
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                database.close()

    def test_restore_never_overwrites_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "existing"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("do not overwrite", encoding="utf-8")
            data = self._make_archive(target / "missing-media")

            with self.assertRaisesRegex(ValueError, "вже існує"):
                admin_tools.restore_backup_archive(data, target)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite")

    def test_restore_management_command_requires_explicit_apply(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "backup.zip"
            archive_path.write_bytes(data)
            with self.assertRaises(CommandError):
                call_command("restore_backup", str(archive_path), str(Path(temp_dir) / "restore"))

    def test_restore_management_command_restores_only_after_apply(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "backup.zip"
            target = Path(temp_dir) / "restore"
            archive_path.write_bytes(data)
            call_command("restore_backup", str(archive_path), str(target), "--apply", stdout=io.StringIO())
            self.assertTrue((target / "db.sqlite3").is_file())

    def test_restore_rejects_dangling_symlink_destination(self):
        data = self._make_archive(Path(tempfile.gettempdir()) / "autozapaz-no-media")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "restore-link"
            target.symlink_to(Path(tempfile.gettempdir()) / "missing-target")
            with self.assertRaisesRegex(ValueError, "вже існує"):
                admin_tools.restore_backup_archive(data, target)

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
