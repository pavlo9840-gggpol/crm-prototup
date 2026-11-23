import base64
import hashlib
import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from django.test import SimpleTestCase

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from verify_release import verify_release


class SignedReleaseVerificationTests(SimpleTestCase):
    def _signed_release(self, root):
        root = Path(root)
        (root / "manage.py").write_text("safe release", encoding="utf-8")
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        public_value = base64.urlsafe_b64encode(public).decode().rstrip("=")
        data = (root / "manage.py").read_bytes()
        manifest = {
            "format_version": 1,
            "version": "1.2.3",
            "files": {"manage.py": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
        }
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (root / "release-manifest.json").write_bytes(payload + b"\n")
        signature = private.sign(payload)
        (root / "release-manifest.sig").write_text(
            base64.urlsafe_b64encode(signature).decode().rstrip("="), encoding="ascii"
        )
        return public_value

    def test_valid_release_signature_and_hashes_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            public = self._signed_release(temp)
            self.assertEqual(verify_release(temp, public, "1.2.3"), "1.2.3")

    def test_tampered_release_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            public = self._signed_release(temp)
            Path(temp, "manage.py").write_text("modified", encoding="utf-8")
            with self.assertRaises(Exception):
                verify_release(temp, public, "1.2.3")

    def test_release_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            public = self._signed_release(temp)
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                verify_release(temp, public, "9.9.9")

    def test_unlisted_release_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            public = self._signed_release(temp)
            Path(temp, "extra.py").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file list mismatch"):
                verify_release(temp, public, "1.2.3")
\n    def test_duplicate_manifest_keys_are_rejected(self):\n        with tempfile.TemporaryDirectory() as temp:\n            public = self._signed_release(temp)\n            private = Ed25519PrivateKey.generate()\n            public_bytes = private.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)\n            public = base64.urlsafe_b64encode(public_bytes).decode().rstrip("=")\n            payload = b'{"format_version":1,"format_version":1,"version":"1.2.3","files":{"manage.py":{"size":12,"sha256":"' + hashlib.sha256(b"safe release").hexdigest().encode() + b'"}}'\n            Path(temp, "release-manifest.json").write_bytes(payload + b"\\n")\n            Path(temp, "release-manifest.sig").write_text(base64.urlsafe_b64encode(private.sign(payload)).decode().rstrip("="), encoding="ascii")\n            with self.assertRaisesRegex(ValueError, "duplicate manifest key"):\n                verify_release(temp, public, "1.2.3")\n