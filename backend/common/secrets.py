"""Encryption helpers for credentials stored by tenant-owned modules."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretDecryptionError(ValueError):
    """Raised when a stored credential cannot be decrypted with the active key."""


CIPHERTEXT_SCHEME = "fernet"
CIPHERTEXT_VERSION = "v1"
CIPHERTEXT_PREFIX = f"{CIPHERTEXT_SCHEME}:{CIPHERTEXT_VERSION}:"
_CIPHERTEXT_SCHEME_PREFIX = f"{CIPHERTEXT_SCHEME}:"
_DECRYPTION_ERROR = "stored credential cannot be decrypted"


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
    payload = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{CIPHERTEXT_PREFIX}{payload}"


def decrypt_secret(value: str) -> str:
    try:
        if value.startswith(CIPHERTEXT_PREFIX):
            payload = value[len(CIPHERTEXT_PREFIX) :]
        elif value.startswith(_CIPHERTEXT_SCHEME_PREFIX):
            # A ciphertext from a newer/unknown format must fail closed instead
            # of being misinterpreted as a legacy Fernet token.
            raise InvalidToken
        else:
            # Credentials encrypted before version prefixes were introduced are
            # still readable so deployments can rotate them in place.
            payload = value
        return _fernet().decrypt(payload.encode("ascii")).decode("utf-8")
    except (
        AttributeError,
        ImproperlyConfigured,
        InvalidToken,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        # Do not chain the provider/key/ciphertext exception. Callers receive one
        # stable message that is safe for API responses and logs.
        raise SecretDecryptionError(_DECRYPTION_ERROR) from None
