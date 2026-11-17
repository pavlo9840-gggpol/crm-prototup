from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parent.parent


class LinuxDeploymentAssetsTests(SimpleTestCase):
    def test_systemd_service_uses_loopback_and_dedicated_account(self):
        unit = (ROOT / "deploy/linux/autozapaz.service").read_text(encoding="utf-8")
        self.assertIn("User=autozapaz", unit)
        self.assertIn("Group=autozapaz", unit)
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("--port 8000", unit)
        self.assertIn("EnvironmentFile=/etc/autozapaz/autozapaz.env", unit)
        self.assertIn("ReadWritePaths=/var/lib/autozapaz", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertNotIn("--host 0.0.0.0", unit)

    def test_installer_preserves_existing_environment_and_requires_public_key(self):
        script = (ROOT / "deploy/linux/install.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -f "$ENV_FILE" ]]', script)
        self.assertIn('AUTOZAPAZ_LICENSE_PUBLIC_KEY', script)
        self.assertIn('chmod 0640 "$ENV_FILE"', script)
        self.assertIn('systemctl enable --now autozapaz.service', script)
        self.assertIn('migrate --noinput', script)
        self.assertIn('collectstatic --noinput', script)
        self.assertNotIn("rm -rf", script)
