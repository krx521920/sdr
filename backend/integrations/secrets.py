"""Encryption helpers for tenant-owned integration credentials."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretDecryptionError(ValueError):
    """Raised when a stored credential cannot be decrypted with the active key."""


def _fernet() -> Fernet:
    configured_key = getattr(settings, "INTEGRATION_ENCRYPTION_KEY", "").strip()
    if configured_key:
        key = configured_key.encode("ascii")
    else:
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            "INTEGRATION_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def encrypt_secret(value: str) -> str:
    if not value:
        raise ValueError("secret cannot be empty")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "stored integration secret cannot be decrypted"
        ) from exc
