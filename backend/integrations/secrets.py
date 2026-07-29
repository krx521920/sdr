"""Backwards-compatible imports for integration credential encryption."""

from common.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

__all__ = ["SecretDecryptionError", "decrypt_secret", "encrypt_secret"]
