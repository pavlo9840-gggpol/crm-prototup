import base64
import hashlib
import json
import tempfile
import shlex
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase

from scripts.verify_release import verify_release


ROOT = Path(__file__).resolve().parent.parent


class LinuxDeploymentAssetsTests(SimpleTestCase):
    def test_systemd_service_uses_loopback_and_dedicated_account(self):
        unit = (ROOT / "deploy/linux/autozapaz.service").read_text(encoding="utf-8")
        self.assertIn("User=autozapaz", unit)
        self.assertIn("Group=autozapaz", unit)
        self.assertIn("waitress-serve", unit)
        self.assertIn("--listen=127.0.0.1:8000", unit)
        self.assertIn("--threads=8", unit)
        self.assertIn("config.wsgi:application", unit)
        self.assertNotIn("manage.py runserver", unit)
        self.assertIn("EnvironmentFile=/etc/autozapaz/autozapaz.env", unit)
        self.assertIn("ReadWritePaths=/var/lib/autozapaz", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertNotIn("--listen=0.0.0.0", unit)

    def test_installer_preserves_existing_environment_and_requires_public_key(self):
        script = (ROOT / "deploy/linux/install.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -f "$ENV_FILE" ]]', script)
        self.assertIn('AUTOZAPAZ_LICENSE_PUBLIC_KEY', script)
        self.assertIn('AUTOZAPAZ_RELEASE_PUBLIC_KEY', script)
        self.assertIn('TRUSTED_VERIFIER_DIR="/usr/local/lib/autozapaz"', script)
        self.assertIn('TRUSTED_VERIFIER="$TRUSTED_VERIFIER_DIR/verify_release.py"', script)
        self.assertIn('TRUSTED_VERIFIER_DIR', script)
        self.assertIn('Каталог bootstrap verifier має належати root', script)
        self.assertIn('Каталог початкового релізу має належати root', script)
        self.assertIn('Початковий реліз містить символічні посилання', script)
        self.assertIn('не бути доступними для запису групі/іншим', script)
        self.assertIn('validate_env_file', script)
        self.assertIn('python3 "$TRUSTED_VERIFIER" --release "$APP_DIR"', script)
        self.assertIn('chmod 0640 "$ENV_FILE"', script)
        self.assertIn('systemctl enable --now autozapaz.service', script)
        self.assertIn('migrate --noinput', script)
        self.assertIn('collectstatic --noinput', script)
        self.assertIn('health_check() {', script)
        self.assertIn('http://127.0.0.1:8000/login/', script)
        self.assertIn('if ! health_check; then', script)
        self.assertIn('journalctl -u autozapaz.service -n 100', script)
        self.assertNotIn("rm -rf", script)
        self.assertIn('UNIT_INSTALLED=0', script)
        self.assertIn('SERVICE_STARTED=0', script)
        self.assertIn('INSTALL_COMPLETE=0', script)
        self.assertIn('on_exit() {', script)
        self.assertIn('trap on_exit EXIT', script)
        self.assertIn('systemctl disable autozapaz.service', script)
        self.assertIn('rm -f -- "$SERVICE_FILE"', script)
        self.assertIn('Каталог програми, конфігурація та дані збережені', script)
        self.assertIn('INSTALL_COMPLETE=1', script)


    def test_updater_creates_and_verifies_backup_before_switching_release(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        backup = script.index('backup --keep 30')
        verify = script.index('verify_backup "$BACKUP_PATH"')
        restore = script.index('restore_backup "$BACKUP_PATH" "$RESTORE_DIR" --apply')
        switch = script.index('atomic_link "$APP_LINK" "$NEW_APP"')
        migrate = script.index('manage.py" migrate --noinput')
        self.assertLess(backup, verify)
        self.assertLess(verify, restore)
        self.assertLess(restore, switch)
        self.assertLess(switch, migrate)
        self.assertIn('migrate --plan', script)
        self.assertIn('health_check', script)

    def test_updater_has_code_venv_database_and_media_rollback(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        self.assertIn('APP_SWITCHED=1', script)
        self.assertIn('VENV_SWITCHED=1', script)
        self.assertIn('SERVICE_STOPPED=1', script)
        self.assertIn('UPDATE_COMPLETE=1', script)
        self.assertIn('ROLLBACK_ATTEMPTED=1', script)
        self.assertIn('on_exit() {', script)
        self.assertIn('trap on_exit EXIT', script)
        self.assertIn('Неочікувана помилка', script)
        self.assertIn('rollback_update "Неочікувана помилка', script)
        self.assertIn('"$DATA_DIR/db.sqlite3$suffix.failed-$STAMP"', script)
        self.assertIn('media.failed-', script)
        self.assertIn('OLD_APP_REAL', script)
        self.assertIn('OLD_VENV_REAL', script)
        self.assertIn('systemctl start "$SERVICE"', script)
        self.assertIn('Старі релізи не видаляються автоматично', script)

    def test_updater_rollback_fails_closed_when_restore_steps_fail(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        rollback = script[script.index("rollback_update() {"):script.index("\non_exit() {")]
        self.assertIn('if ! systemctl stop "$SERVICE"; then', rollback)
        self.assertIn('if ! cp "$RESTORE_DIR/db.sqlite3" "$temporary_db"', rollback)
        self.assertIn('if ! mv "$RESTORE_DIR/media" "$DATA_DIR/media"; then', rollback)
        self.assertIn('Служба залишена зупиненою', rollback)
        self.assertIn('if ! chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/media"', rollback)
        self.assertIn('if ! systemctl start "$SERVICE"; then', rollback)
        self.assertIn('if ! health_check; then', rollback)
        self.assertIn(r'Backup: %s\n', rollback)
        self.assertNotIn(r'Backup: %s\\n', rollback)
        self.assertNotIn('systemctl start "$SERVICE" || true', rollback)

    def test_updater_exit_trap_uses_health_checked_service_recovery(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        start = script.index("on_exit() {")
        end = script.index("\ntrap on_exit EXIT", start)
        exit_handler = script[start:end]
        self.assertIn("resume_service || printf", exit_handler)
        self.assertNotIn("SERVICE_STOPPED=0", exit_handler)
        helper_start = script.index("resume_service() {")
        helper_end = script.index("rollback_update() {", helper_start)
        self.assertIn("health_check", script[helper_start:helper_end])

    def test_updater_restarts_old_service_only_after_health_check(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        resume = script[script.index("resume_service() {"):script.index("\nrollback_update() {")]
        self.assertIn('if ! systemctl start "$SERVICE"; then', resume)
        self.assertIn("if ! health_check; then", resume)
        self.assertIn("SERVICE_STOPPED=0", resume)
        self.assertEqual(script.count("resume_service || true"), 5)
        self.assertNotIn('systemctl start "$SERVICE" || true', script)

    def test_updater_resume_service_preserves_stopped_flag_on_failures(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        resume = script[script.index("resume_service() {"):script.index("\nrollback_update() {")]
        scenarios = (
            ("start failure", "1", "0", 1, ["start"]),
            ("health failure", "0", "1", 1, ["start", "health", "stop"]),
            ("success", "0", "0", 0, ["start", "health"]),
        )
        for name, fail_start, fail_health, expected_stopped, expected_calls in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / "calls.log"
                harness = f"""
SERVICE=autozapaz.service
SERVICE_STOPPED=1
CALL_LOG={shlex.quote(str(log))}
FAIL_START={fail_start}
FAIL_HEALTH={fail_health}
systemctl() {{
    printf '%s\\n' "$1" >> "$CALL_LOG"
    [[ "$1" != start || "$FAIL_START" != 1 ]]
}}
health_check() {{
    printf 'health\\n' >> "$CALL_LOG"
    [[ "$FAIL_HEALTH" != 1 ]]
}}
{resume}
if resume_service; then printf 'RESULT=success\\n'; else printf 'RESULT=failure\\n'; fi
printf 'SERVICE_STOPPED=%s\\n' "$SERVICE_STOPPED"
"""
                completed = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"SERVICE_STOPPED={expected_stopped}", completed.stdout)
                self.assertEqual(log.read_text(encoding="utf-8").splitlines(), expected_calls)

    def test_updater_verifies_signed_release_before_copying_or_stopping_service(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        verify = script.index('"$VENV_LINK/bin/python" "$TRUSTED_VERIFIER" --release')
        copy = script.index('cp -a "$RELEASE_SOURCE" "$NEW_APP"')
        stop_section = script.index('info "Зупинка служби та резервне копіювання"')
        stop = script.index('systemctl stop "$SERVICE"', stop_section)
        self.assertLess(verify, copy)
        self.assertLess(verify, stop)
        self.assertIn('AUTOZAPAZ_RELEASE_PUBLIC_KEY', script)
        self.assertIn('TRUSTED_VERIFIER="$APP_LINK/scripts/verify_release.py"', script)
        self.assertIn('Staging-копія релізу не пройшла повторну перевірку', script)
        self.assertIn('--version "$VERSION"', script)
        self.assertIn('Підпис або контрольні суми релізу не пройшли перевірку', script)

    def test_signed_release_tools_exist_and_validate_manifest(self):
        signer = (ROOT / "scripts/sign_release.py").read_text(encoding="utf-8")
        verifier = (ROOT / "scripts/verify_release.py").read_text(encoding="utf-8")
        self.assertIn("Ed25519PrivateKey", signer)
        self.assertIn("release-manifest.json", signer)
        self.assertIn("release-manifest.sig", signer)
        self.assertIn("Ed25519PublicKey", verifier)
        self.assertIn("verify_release", verifier)
        self.assertIn("file list mismatch", verifier)
        self.assertIn("digest mismatch", verifier)

    def test_updater_rejects_symlinks_and_invalid_release_version(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        self.assertIn('VERSION" =~ ^[A-Za-z0-9]', script)
        self.assertIn('find "$RELEASE_SOURCE" -type l', script)
        self.assertIn('find "$RELEASE_SOURCE" ! -type d ! -type f', script)
        self.assertIn('не може бути всередині чинної програми', script)


    def _execute_updater_rollback(self, fail_media_restore=False, fail_health_check=False, fail_initial_stop=False, fail_migration=False):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        start = script.index("rollback_update() {")
        end = script.index("\non_exit() {", start)
        rollback = script[start:end]

        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        data = root / "data"
        restore = root / "restore"
        old_app = root / "releases" / "old"
        new_app = root / "releases" / "new"
        old_venv = root / "venvs" / "old"
        new_venv = root / "venvs" / "new"
        app_link = root / "current"
        venv_link = root / "venv"
        call_log = root / "calls.log"
        for directory in (data, restore / "media", old_app, new_app, old_venv, new_venv, data / "media"):
            directory.mkdir(parents=True, exist_ok=True)
        (data / "db.sqlite3").write_text("new database", encoding="utf-8")
        (restore / "db.sqlite3").write_text("previous database", encoding="utf-8")
        (data / "db.sqlite3-wal").write_text("new WAL", encoding="utf-8")
        (data / "db.sqlite3-shm").write_text("new SHM", encoding="utf-8")
        (data / "media" / "new.txt").write_text("new media", encoding="utf-8")
        (restore / "media" / "old.txt").write_text("previous media", encoding="utf-8")
        app_link.symlink_to(new_app, target_is_directory=True)
        venv_link.symlink_to(new_venv, target_is_directory=True)

        variables = {
            "DATA_DIR": data,
            "RESTORE_DIR": restore,
            "OLD_APP_REAL": old_app,
            "OLD_VENV_REAL": old_venv,
            "APP_LINK": app_link,
            "VENV_LINK": venv_link,
            "SERVICE": "autozapaz.service",
            "SERVICE_USER": "autozapaz",
            "STAMP": "test",
            "BACKUP_PATH": str(root / "backup.zip"),
            "CALL_LOG": call_log,
            "FAIL_MEDIA_RESTORE": "1" if fail_media_restore else "0",
            "FAIL_HEALTH_CHECK": "1" if fail_health_check else "0",
            "FAIL_INITIAL_STOP": "1" if fail_initial_stop else "0",
            "FAIL_MIGRATION": "1" if fail_migration else "0",
            "APP_SWITCHED": "1",
            "VENV_SWITCHED": "1",
            "ROLLBACK_ATTEMPTED": "0",
        }
        assignments = "\n".join(
            f"{key}={shlex.quote(str(value))}" for key, value in variables.items()
        )
        harness = f"""
{assignments}
systemctl() {{
    printf 'systemctl %s\\n' "$*" >> "$CALL_LOG"
    [[ "$1" != stop || "$FAIL_INITIAL_STOP" != 1 ]]
}}
chown() {{ return 0; }}
chmod() {{ return 0; }}
mv() {{
    if [[ "$FAIL_MEDIA_RESTORE" == 1 && "$1" == "$RESTORE_DIR/media" ]]; then
        return 1
    fi
    command mv "$@"
}}
health_check() {{
    printf 'health_check\\n' >> "$CALL_LOG"
    [[ "$FAIL_HEALTH_CHECK" != 1 ]]
}}
as_service() {{
    [[ "$FAIL_MIGRATION" != 1 ]]
}}
fail() {{
    printf '%s\\n' "$*" >&2
    exit 1
}}
{rollback}
if [[ "$FAIL_MIGRATION" == 1 ]]; then
    if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" migrate --noinput; then
        rollback_update "Міграції завершилися помилкою." || true
        fail "Оновлення скасовано."
    fi
else
    rollback_update "integration test"
fi
"""
        completed = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            check=False,
        )
        return temporary, completed, data, restore, old_app, old_venv, call_log

    def test_updater_rollback_executes_against_isolated_filesystem(self):
        temporary, completed, data, restore, old_app, old_venv, call_log = self._execute_updater_rollback()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((data / "db.sqlite3").read_text(encoding="utf-8"), "previous database")
        self.assertEqual((data / "media" / "old.txt").read_text(encoding="utf-8"), "previous media")
        self.assertEqual((data / "db.sqlite3-wal.failed-test").read_text(encoding="utf-8"), "new WAL")
        self.assertEqual((data / "db.sqlite3-shm.failed-test").read_text(encoding="utf-8"), "new SHM")
        self.assertEqual((data / "media" / "new.txt").exists(), False)
        self.assertEqual((data.parent / "current").resolve(), old_app)
        self.assertEqual((data.parent / "venv").resolve(), old_venv)
        calls = call_log.read_text(encoding="utf-8")
        self.assertIn("systemctl start autozapaz.service", calls)
        self.assertIn("health_check", calls)

    def test_updater_rollback_keeps_service_stopped_if_media_restore_fails(self):
        temporary, completed, data, restore, old_app, old_venv, call_log = self._execute_updater_rollback(
            fail_media_restore=True
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual((data / "db.sqlite3").read_text(encoding="utf-8"), "previous database")
        calls = call_log.read_text(encoding="utf-8")
        self.assertIn("systemctl stop autozapaz.service", calls)
        self.assertNotIn("systemctl start autozapaz.service", calls)
        self.assertIn("Служба залишена зупиненою", completed.stderr)
        self.assertTrue((restore / "media" / "old.txt").exists())


    def test_updater_rollback_stops_service_when_health_check_fails(self):
        temporary, completed, data, restore, old_app, old_venv, call_log = self._execute_updater_rollback(
            fail_health_check=True
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(completed.returncode, 0)
        calls = call_log.read_text(encoding="utf-8").splitlines()
        start_index = calls.index("systemctl start autozapaz.service")
        self.assertIn("systemctl stop autozapaz.service", calls[start_index + 1:])
        self.assertIn("health-check; службу зупинено", completed.stderr)


    def test_updater_rollback_does_not_touch_data_if_service_cannot_stop(self):
        temporary, completed, data, restore, old_app, old_venv, call_log = self._execute_updater_rollback(
            fail_initial_stop=True
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual((data / "db.sqlite3").read_text(encoding="utf-8"), "new database")
        self.assertTrue((data / "media" / "new.txt").exists())
        self.assertEqual((data.parent / "current").resolve(), data.parent / "releases" / "new")
        calls = call_log.read_text(encoding="utf-8")
        self.assertIn("systemctl stop autozapaz.service", calls)
        self.assertNotIn("systemctl start autozapaz.service", calls)

    def test_updater_migration_failure_runs_real_rollback_and_cancels_update(self):
        temporary, completed, data, restore, old_app, old_venv, call_log = self._execute_updater_rollback(
            fail_migration=True
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("Міграції завершилися помилкою", completed.stderr)
        self.assertIn("Оновлення скасовано", completed.stderr)
        self.assertIn("Попередню версію та дані відновлено", completed.stdout)
        self.assertEqual((data / "db.sqlite3").read_text(encoding="utf-8"), "previous database")
        self.assertEqual((data / "media" / "old.txt").read_text(encoding="utf-8"), "previous media")
        self.assertEqual((data.parent / "current").resolve(), old_app)
        self.assertEqual((data.parent / "venv").resolve(), old_venv)
        calls = call_log.read_text(encoding="utf-8")
        self.assertIn("systemctl start autozapaz.service", calls)
        self.assertIn("health_check", calls)


class SignedReleaseVerificationTests(SimpleTestCase):
    def _make_signed_release(self, root, private_key):
        payload_file = root / "manage.py"
        payload_file.write_text("print('safe')\n", encoding="utf-8")
        data = payload_file.read_bytes()
        manifest = {
            "format_version": 1,
            "version": "1.2.3",
            "files": {
                "manage.py": {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            },
        }
        payload = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        (root / "release-manifest.json").write_bytes(payload + b"\n")
        signature = base64.urlsafe_b64encode(private_key.sign(payload)).decode("ascii").rstrip("=")
        (root / "release-manifest.sig").write_text(signature + "\n", encoding="ascii")

    def test_valid_ed25519_release_and_expected_version(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        encoded_key = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_signed_release(root, private_key)
            self.assertEqual(verify_release(root, encoded_key, "1.2.3"), "1.2.3")

    def test_tampered_file_is_rejected(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        encoded_key = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_signed_release(root, private_key)
            (root / "manage.py").write_text("print('tampered')\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "size mismatch|digest mismatch"):
                verify_release(root, encoded_key, "1.2.3")

    def test_wrong_version_is_rejected(self):
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        encoded_key = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_signed_release(root, private_key)
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                verify_release(root, encoded_key, "9.9.9")
