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
MAX_RELEASE_SEQUENCE = 2**63 - 2


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
    return {"format_version": 2, "version": None, "release_sequence": None, "files": files}


def _same_file(left, right):
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except FileNotFoundError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def publish_signed_files(root, manifest_payload, signature):
    """Publish a manifest/signature pair without overwriting or leaving half a pair."""
    import os
    import tempfile

    root = Path(root).resolve()
    manifest_path = root / MANIFEST
    signature_path = root / SIGNATURE
    if manifest_path.exists() or signature_path.exists():
        raise FileExistsError("refusing to overwrite existing signature files")

    temporary_paths = []
    published_paths = []
    try:
        with tempfile.NamedTemporaryFile(dir=root.parent, prefix=".release-manifest-", delete=False) as stream:
            manifest_tmp = Path(stream.name)
            temporary_paths.append(manifest_tmp)
            stream.write(manifest_payload + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=root.parent,
            prefix=".release-signature-", delete=False
        ) as stream:
            signature_tmp = Path(stream.name)
            temporary_paths.append(signature_tmp)
            stream.write(signature + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        # Hard-link publication is atomic and fails if another process created a target.
        os.link(manifest_tmp, manifest_path)
        published_paths.append((manifest_path, manifest_tmp))
        os.link(signature_tmp, signature_path)
        published_paths.append((signature_path, signature_tmp))

        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            directory_fd = os.open(root, os.O_RDONLY | directory_flag)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        for published, temporary in reversed(published_paths):
            if _same_file(published, temporary):
                published.unlink(missing_ok=True)
        raise
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", required=True, type=int, help="Monotonically increasing release sequence")
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
    try:
        if private_path.stat().st_nlink != 1:
            parser.error("private signing key must not have additional hard links")
    except OSError as exc:
        parser.error("cannot inspect private signing key: " + str(exc))
    if (root / MANIFEST).exists() or (root / SIGNATURE).exists():
        parser.error("refusing to overwrite existing signature files")
    try:
        validate_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.sequence <= MAX_RELEASE_SEQUENCE:
        parser.error("release sequence must be between 1 and 2^63-2")

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
    manifest["release_sequence"] = args.sequence
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    signature = b64(key.sign(payload))

    try:
        publish_signed_files(root, payload, signature)
    except OSError as exc:
        parser.error("could not publish signed release files: " + str(exc))

    print("Signed release:", root)


if __name__ == "__main__":
    main()
