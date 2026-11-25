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



    def test_release_signer_hashes_large_files_in_bounded_chunks(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from sign_release import HASH_CHUNK_BYTES, sha256_file

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.bin"
            payload = (b"release-data-" * (HASH_CHUNK_BYTES // 13 + 100))
            path.write_bytes(payload)
            size, digest = sha256_file(path)
            self.assertEqual(size, len(payload))
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_release_signer_rejects_unsafe_version_identifiers(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from sign_release import validate_version

        for version in ("", "../1.0", "1.0\\nforged", "x" * 65, " space"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_version(version)
        self.assertEqual(validate_version("1.2.3-beta_1"), "1.2.3-beta_1")

    def test_oversized_manifest_is_rejected_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manage.py").write_text("safe release", encoding="utf-8")
            (root / "release-manifest.json").write_bytes(b" " * (16 * 1024 * 1024 + 1))
            (root / "release-manifest.sig").write_text("AA", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "manifest exceeds size limit"):
                verify_release(root, "A" * 43, "1.2.3")

    def test_oversized_signature_is_rejected_before_decoding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manage.py").write_text("safe release", encoding="utf-8")
            (root / "release-manifest.json").write_bytes(b"{}")
            (root / "release-manifest.sig").write_bytes(b"A" * (4096 + 1))
            with self.assertRaisesRegex(ValueError, "signature exceeds size limit"):
                verify_release(root, "A" * 43, "1.2.3")

    def test_duplicate_manifest_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manage.py").write_text("safe release", encoding="utf-8")
            private = Ed25519PrivateKey.generate()
            public_bytes = private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            public = base64.urlsafe_b64encode(public_bytes).decode().rstrip("=")
            digest = hashlib.sha256(b"safe release").hexdigest()
            payload = (
                '{"format_version":1,"format_version":1,"version":"1.2.3",'
                '"files":{"manage.py":{"size":12,"sha256":"' + digest + '"}}}'
            ).encode()
            (root / "release-manifest.json").write_bytes(payload + b"\n")
            (root / "release-manifest.sig").write_text(
                base64.urlsafe_b64encode(private.sign(payload)).decode().rstrip("="),
                encoding="ascii",
            )
            with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
                verify_release(root, public, "1.2.3")
