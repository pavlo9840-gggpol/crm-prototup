#!/usr/bin/env python3
"""Publisher-only helper. Generate an Ed25519 keypair outside the customer build."""
import argparse
import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", default="autozapaz-license-private.pem")
    args = parser.parse_args()

    path = Path(args.private_key).expanduser().resolve()
    if path.exists():
        parser.error(f"Refusing to overwrite existing private key: {path}")

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private_bytes)
    try:
        path.chmod(0o600)
    except OSError:
        pass

    print("PRIVATE KEY SAVED (keep it offline and never commit/distribute it):")
    print(path)
    print("Set this value in the release environment as AUTOZAPAZ_LICENSE_PUBLIC_KEY:")
    print(base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("="))


if __name__ == "__main__":
    main()
