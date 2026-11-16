"""Offline Ed25519 license validation for local AutoZapaz installations.

Token format: AZ1.<base64url-json-payload>.<base64url-ed25519-signature>
Only the public verification key is shipped with the application.
"""
import base64
import json
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.utils import timezone


class LicenseError(ValueError):
    pass


def _b64decode(value):
    try:
        raw = value.encode("ascii")
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception as exc:
        raise LicenseError("Некоректний формат ліцензійного ключа.") from exc


def _parse_datetime(value, field_name, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str):
        raise LicenseError(f"У ключі відсутнє поле {field_name}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LicenseError(f"Некоректна дата в полі {field_name}.") from exc
    if parsed.tzinfo is None:
        raise LicenseError(f"Дата {field_name} повинна містити часовий пояс.")
    return parsed


def verify_license_token(token, *, now=None):
    """Return validated claims or raise LicenseError; never contacts a server."""
    token = (token or "").strip()
    if len(token) > 8192:
        raise LicenseError("Ліцензійний ключ завеликий.")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "AZ1":
        raise LicenseError("Непідтримуваний формат ключа. Очікується ключ АвтоЗапас AZ1.")

    public_text = getattr(settings, "AUTOZAPAZ_LICENSE_PUBLIC_KEY", "")
    if not public_text:
        raise LicenseError("У цій збірці ще не налаштовано відкритий ключ видавця ліцензій.")
    try:
        public_bytes = _b64decode(public_text)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
    except Exception as exc:
        raise LicenseError("Некоректно налаштований відкритий ключ ліцензування.") from exc

    payload_bytes = _b64decode(parts[1])
    signature = _b64decode(parts[2])
    if len(payload_bytes) > 4096:
        raise LicenseError("Дані ліцензії завеликі.")
    try:
        public_key.verify(signature, payload_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise LicenseError("Підпис ліцензії недійсний або ключ було змінено.") from exc

    try:
        claims = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseError("Не вдалося прочитати дані ліцензії.") from exc
    if not isinstance(claims, dict) or claims.get("version") != 1:
        raise LicenseError("Невідома версія ліцензії.")

    license_id = claims.get("license_id")
    plan = claims.get("plan")
    if not isinstance(license_id, str) or not license_id.strip() or len(license_id) > 80:
        raise LicenseError("У ліцензії немає коректного ідентифікатора.")
    if plan not in {"start", "pro", "business"}:
        raise LicenseError("У ліцензії вказано невідомий тариф.")

    issued_at = _parse_datetime(claims.get("issued_at"), "issued_at")
    expires_at = _parse_datetime(claims.get("expires_at"), "expires_at", optional=True)
    current = now or timezone.now()
    if issued_at.timestamp() > current.timestamp() + 300:
        raise LicenseError("Дата видачі ліцензії знаходиться в майбутньому.")
    if expires_at and expires_at <= current:
        raise LicenseError("Строк дії ліцензії завершився.")

    max_users = claims.get("max_users", 1)
    max_workstations = claims.get("max_workstations", 1)
    if type(max_users) is not int or not 1 <= max_users <= 10000:
        raise LicenseError("Некоректний ліміт користувачів у ліцензії.")
    if type(max_workstations) is not int or not 1 <= max_workstations <= 1000:
        raise LicenseError("Некоректний ліміт робочих місць у ліцензії.")
    features = claims.get("features", [])
    if not isinstance(features, list) or any(not isinstance(x, str) or len(x) > 80 for x in features):
        raise LicenseError("Некоректний перелік модулів у ліцензії.")

    return {
        "license_id": license_id.strip(),
        "plan": plan,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "max_users": max_users,
        "max_workstations": max_workstations,
        "features": sorted(set(features)),
        "customer": str(claims.get("customer", ""))[:200],
    }


def get_active_license():
    """Return the stored activation only while its signed token remains valid."""
    from .models import LicenseActivation

    activation = LicenseActivation.objects.filter(pk=1).first()
    if not activation:
        return None
    try:
        claims = verify_license_token(activation.token)
    except LicenseError:
        return None
    if claims["license_id"] != activation.license_id:
        return None
    return claims


def license_is_required():
    return bool(getattr(settings, "LICENSE_ENFORCEMENT_ENABLED", not settings.DEBUG))
