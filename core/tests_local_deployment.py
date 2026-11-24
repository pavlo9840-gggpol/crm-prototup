import base64
import hashlib
import json
import tempfile
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
        self.assertIn('/usr/local/lib/autozapaz/verify_release.py', script)
        self.assertIn('TRUSTED_VERIFIER_DIR', script)
        self.assertIn('Каталог bootstrap verifier має належати root', script)
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
        self.assertIn('db.sqlite3.failed-', script)
        self.assertIn('media.failed-', script)
        self.assertIn('OLD_APP_REAL', script)
        self.assertIn('OLD_VENV_REAL', script)
        self.assertIn('systemctl start "$SERVICE"', script)
        self.assertIn('Старі релізи не видаляються автоматично', script)

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
