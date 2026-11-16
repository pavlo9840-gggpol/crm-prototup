#!/usr/bin/env python3
"""Publisher-only helper for issuing signed offline AutoZapaz license keys."""
import argparse
import base64
import json
import os
import uuid
from getpass import getpass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--customer", default="")
    parser.add_argument("--plan", choices=("start", "pro", "business"), required=True)
    parser.add_argument("--days", type=int, default=0, help="0 = perpetual")
    parser.add_argument("--max-users", type=int, default=1)
    parser.add_argument("--max-workstations", type=int, default=1)
    parser.add_argument("--features", default="", help="Comma-separated feature identifiers")
    args = parser.parse_args()

    if args.days < 0 or args.max_users < 1 or args.max_workstations < 1:
        parser.error("days must be >= 0 and user/workstation limits must be >= 1")

    key_data = Path(args.private_key).expanduser().read_bytes()
    password = os.environ.get("AUTOZAPAZ_LICENSE_KEY_PASSWORD") or getpass("Private-key password: ")
    private_key = serialization.load_pem_private_key(key_data, password=password.encode("utf-8"))
    if not isinstance(private_key, Ed25519PrivateKey):
        parser.error("The private key must be Ed25519 PEM.")

    now = datetime.now(timezone.utc)
    claims = {
        "version": 1,
        "license_id": str(uuid.uuid4()),
        "customer": args.customer,
        "plan": args.plan,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(days=args.days)).isoformat() if args.days else None,
        "max_users": args.max_users,
        "max_workstations": args.max_workstations,
        "features": sorted({x.strip() for x in args.features.split(",") if x.strip()}),
    }
    payload = json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload)
    print("AZ1." + b64(payload) + "." + b64(signature))


if __name__ == "__main__":
    main()
