#!/usr/bin/env python3
"""Verify an AutoZapaz release manifest signature and every file digest."""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST = "release-manifest.json"
SIGNATURE = "release-manifest.sig"


def decode_b64(value):
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_key(value):
    value = value.strip()
    if value.startswith("-----BEGIN"):
        key = serialization.load_pem_public_key(value.encode("ascii"))
    else:
        raw = decode_b64(value)
        key = Ed25519PublicKey.from_public_bytes(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("release public key must be Ed25519")
    return key


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key: " + key)
        result[key] = value
    return result


def verify_release(root, public_key, expected_version=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("release directory does not exist")
    manifest_path = root / MANIFEST
    signature_path = root / SIGNATURE
    if not manifest_path.is_file() or not signature_path.is_file():
        raise ValueError("signed release manifest or signature missing")

    payload = manifest_path.read_bytes().strip()
    signature = decode_b64(signature_path.read_text(encoding="ascii").strip())
    load_key(public_key).verify(signature, payload)
    manifest = json.loads(payload, object_pairs_hook=reject_duplicate_keys)

    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
        raise ValueError("unsupported manifest format")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("invalid release version")
    if expected_version and version != expected_version:
        raise ValueError("release version mismatch")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest file list invalid")

    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("release contains symlink")
        if path.is_file() and path.relative_to(root).as_posix() not in (MANIFEST, SIGNATURE):
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError("release file list mismatch")

    for relative, metadata in files.items():
        if (not isinstance(relative, str) or not relative or relative.startswith("/")
                or "\\" in relative or ".." in Path(relative).parts
                or Path(relative).is_absolute()):
            raise ValueError("unsafe manifest path")
        if not isinstance(metadata, dict) or type(metadata.get("size")) is not int or metadata["size"] < 0:
            raise ValueError("invalid file metadata")
        digest = metadata.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid SHA256")
        file_path = root / relative
        if not file_path.is_file() or file_path.stat().st_size != metadata["size"]:
            raise ValueError("size mismatch: " + relative)
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != digest:
            raise ValueError("digest mismatch: " + relative)
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--public-key", default=os.environ.get("AUTOZAPAZ_RELEASE_PUBLIC_KEY", ""))
    parser.add_argument("--version")
    args = parser.parse_args()
    if not args.public_key:
        parser.error("release public key required")
    try:
        version = verify_release(args.release, args.public_key, args.version)
    except Exception as exc:
        print("Release verification failed:", exc, file=sys.stderr)
        raise SystemExit(1)
    print("Verified signed release:", version)


if __name__ == "__main__":
    main()
