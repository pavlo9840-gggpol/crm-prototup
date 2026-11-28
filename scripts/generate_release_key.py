#!/usr/bin/env python3
"""Generate an Ed25519 key pair for signing AutoZapaz releases.

Keep the private key offline and out of the repository. The public key is
printed as Base64URL and can be installed in AUTOZAPAZ_RELEASE_PUBLIC_KEY.
"""
import argparse
import base64
from getpass import getpass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()
    private_path = args.private_key.expanduser()
    public_path = args.public_key.expanduser()
    if private_path.exists() or public_path.exists():
        parser.error("Refusing to overwrite existing key files.")
    if private_path.resolve() == public_path.resolve():
        parser.error("Private and public key paths must be different.")
    password = getpass("Password for encrypted private key (min 12 characters): ")
    if len(password) < 12:
        parser.error("Private-key password must be at least 12 characters.")
    confirm = getpass("Confirm password: ")
    if password != confirm:
        parser.error("Passwords do not match.")
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_bytes)
    private_path.chmod(0o600)
    public_value = base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")
    public_path.write_text(public_value + "\n", encoding="ascii")
    public_path.chmod(0o644)
    print("Private key:", private_path)
    print("Public key:", public_path)
    print("AUTOZAPAZ_RELEASE_PUBLIC_KEY=" + public_value)
    print("WARNING: Keep the private key offline. Never commit or distribute it.")


if __name__ == "__main__":
    main()
