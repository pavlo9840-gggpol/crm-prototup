from pathlib import Path

from django.test import SimpleTestCase


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
        self.assertIn('chmod 0640 "$ENV_FILE"', script)
        self.assertIn('systemctl enable --now autozapaz.service', script)
        self.assertIn('migrate --noinput', script)
        self.assertIn('collectstatic --noinput', script)
        self.assertIn('health_check() {', script)
        self.assertIn('http://127.0.0.1:8000/login/', script)
        self.assertIn('if ! health_check; then', script)
        self.assertIn('journalctl -u autozapaz.service -n 100', script)
        self.assertNotIn("rm -rf", script)


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
        self.assertIn('db.sqlite3.failed-', script)
        self.assertIn('media.failed-', script)
        self.assertIn('OLD_APP_REAL', script)
        self.assertIn('OLD_VENV_REAL', script)
        self.assertIn('systemctl start "$SERVICE"', script)
        self.assertIn('Старі релізи не видаляються автоматично', script)

    def test_updater_rejects_symlinks_and_invalid_release_version(self):
        script = (ROOT / "deploy/linux/update.sh").read_text(encoding="utf-8")
        self.assertIn('VERSION" =~ ^[A-Za-z0-9]', script)
        self.assertIn('find "$RELEASE_SOURCE" -type l', script)
        self.assertIn('find "$RELEASE_SOURCE" ! -type d ! -type f', script)
        self.assertIn('не може бути всередині чинної програми', script)
