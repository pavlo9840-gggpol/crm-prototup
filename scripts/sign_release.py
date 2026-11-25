#!/usr/bin/env python3
"""Create a signed manifest for a distributable AutoZapaz release."""
import argparse
import base64
import hashlib
import json
import re
from getpass import getpass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MANIFEST = "release-manifest.json"
SIGNATURE = "release-manifest.sig"
HASH_CHUNK_BYTES = 1024 * 1024
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def validate_version(version):
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("invalid release version")
    return version


def build_manifest(root):
    root = Path(root).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("release contains symlink: " + str(path.relative_to(root)))
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            size, digest = sha256_file(path)
            files[relative] = {"size": size, "sha256": digest}
    if not files:
        raise ValueError("release is empty")
    return {"format_version": 1, "version": None, "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    root = args.release.resolve()
    private_path = args.private_key.expanduser().resolve()
    if not root.is_dir():
        parser.error("release directory does not exist")
    try:
        private_path.relative_to(root)
    except ValueError:
        pass
    else:
        parser.error("private signing key must be stored outside the release directory")
    if not private_path.is_file() or args.private_key.expanduser().is_symlink():
        parser.error("private signing key must be a regular, non-symlink file")
    if (root / MANIFEST).exists() or (root / SIGNATURE).exists():
        parser.error("refusing to overwrite existing signature files")
    try:
        validate_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))

    key_data = private_path.read_bytes()
    key = serialization.load_pem_private_key(
        key_data, password=getpass("Release signing-key password: ").encode("utf-8")
    )
    if not isinstance(key, Ed25519PrivateKey):
        parser.error("release key must be Ed25519")

    try:
        manifest = build_manifest(root)
    except ValueError as exc:
        parser.error(str(exc))
    manifest["version"] = args.version
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    signature = b64(key.sign(payload))

    # Write temporary files outside the release tree so they cannot enter its manifest.
    import tempfile
    manifest_tmp = None
    signature_tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=root.parent, prefix=".release-manifest-", delete=False) as stream:
            manifest_tmp = Path(stream.name)
            stream.write(payload + b"\\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=root.parent,
            prefix=".release-signature-", delete=False
        ) as stream:
            signature_tmp = Path(stream.name)
            stream.write(signature + "\\n")
            stream.flush()
            os.fsync(stream.fileno())
        import os
        os.replace(manifest_tmp, root / MANIFEST)
        manifest_tmp = None
        os.replace(signature_tmp, root / SIGNATURE)
        signature_tmp = None
    finally:
        for temporary in (manifest_tmp, signature_tmp):
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    print("Signed release:", root)


if __name__ == "__main__":
    main()
