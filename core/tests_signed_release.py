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


    def test_signer_cli_creates_release_verified_by_runtime_verifier(self):
        from unittest import mock
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sign_release

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release = base / "release"
            release.mkdir()
            (release / "manage.py").write_text("release payload", encoding="utf-8")
            private = Ed25519PrivateKey.generate()
            pem = private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.BestAvailableEncryption(b"test-password"),
            )
            key_path = base / "publisher-key.pem"
            key_path.write_bytes(pem)
            public = private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            public_value = base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")

            with mock.patch.object(sys, "argv", [
                "sign_release.py", "--release", str(release),
                "--private-key", str(key_path), "--version", "2.0.0",
            ]), mock.patch.object(sign_release, "getpass", return_value="test-password"):
                sign_release.main()

            self.assertEqual(verify_release(release, public_value, "2.0.0"), "2.0.0")
            self.assertTrue((release / "release-manifest.json").read_bytes().endswith(b"\n"))
            self.assertTrue((release / "release-manifest.sig").read_bytes().endswith(b"\n"))

    def test_signer_refuses_private_key_inside_release_tree(self):
        from unittest import mock
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sign_release

        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "release"
            release.mkdir()
            key_path = release / "publisher-key.pem"
            key_path.write_text("not a real key", encoding="ascii")
            with mock.patch.object(sys, "argv", [
                "sign_release.py", "--release", str(release),
                "--private-key", str(key_path), "--version", "1.0.0",
            ]), self.assertRaises(SystemExit):
                sign_release.main()

    def test_signer_refuses_symlink_private_key(self):
        from unittest import mock
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sign_release

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release = base / "release"
            release.mkdir()
            real_key = base / "real-key.pem"
            real_key.write_text("not a real key", encoding="ascii")
            linked_key = base / "linked-key.pem"
            linked_key.symlink_to(real_key)
            with mock.patch.object(sys, "argv", [
                "sign_release.py", "--release", str(release),
                "--private-key", str(linked_key), "--version", "1.0.0",
            ]), self.assertRaises(SystemExit):
                sign_release.main()

    def test_signer_refuses_symlinks_inside_release_tree(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from sign_release import build_manifest

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            target.write_text("payload", encoding="utf-8")
            (root / "linked.txt").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "release contains symlink"):
                build_manifest(root)


    def test_signer_refuses_private_key_with_hard_link_inside_release(self):
        from unittest import mock
        import os
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import sign_release

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release = base / "release"
            release.mkdir()
            key_path = base / "publisher-key.pem"
            key_path.write_text("not a real key", encoding="ascii")
            os.link(key_path, release / "linked-publisher-key.pem")
            with mock.patch.object(sys, "argv", [
                "sign_release.py", "--release", str(release),
                "--private-key", str(key_path), "--version", "1.0.0",
            ]), self.assertRaises(SystemExit):
                sign_release.main()
