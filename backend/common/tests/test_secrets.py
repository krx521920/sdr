import base64

import pytest
from cryptography.fernet import Fernet
from django.test import override_settings

from common.secrets import (
    CIPHERTEXT_PREFIX,
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
)


TEST_KEY_A = base64.urlsafe_b64encode(b"a" * 32).decode("ascii")
TEST_KEY_B = base64.urlsafe_b64encode(b"b" * 32).decode("ascii")
STABLE_ERROR = "stored credential cannot be decrypted"


@override_settings(INTEGRATION_ENCRYPTION_KEY=TEST_KEY_A)
def test_new_ciphertexts_are_versioned_and_round_trip():
    plaintext = "tenant-provider-secret"

    ciphertext = encrypt_secret(plaintext)

    assert ciphertext.startswith(CIPHERTEXT_PREFIX)
    assert plaintext not in ciphertext
    assert decrypt_secret(ciphertext) == plaintext


@override_settings(INTEGRATION_ENCRYPTION_KEY=TEST_KEY_A)
def test_legacy_unprefixed_ciphertexts_remain_decryptable():
    plaintext = "legacy-provider-secret"
    legacy = Fernet(TEST_KEY_A.encode("ascii")).encrypt(
        plaintext.encode("utf-8")
    ).decode("ascii")

    assert not legacy.startswith(CIPHERTEXT_PREFIX)
    assert decrypt_secret(legacy) == plaintext


@override_settings(INTEGRATION_ENCRYPTION_KEY=TEST_KEY_A)
@pytest.mark.parametrize(
    "ciphertext",
    (
        "not-a-ciphertext",
        "fernet:v2:not-supported",
        "fernet:v1:not-valid-fernet-data",
    ),
)
def test_decryption_failures_return_only_the_stable_error(ciphertext):
    with pytest.raises(SecretDecryptionError) as caught:
        decrypt_secret(ciphertext)

    assert str(caught.value) == STABLE_ERROR
    assert ciphertext not in str(caught.value)
    assert caught.value.__cause__ is None


def test_wrong_key_does_not_leak_key_or_ciphertext():
    with override_settings(INTEGRATION_ENCRYPTION_KEY=TEST_KEY_A):
        ciphertext = encrypt_secret("key-rotation-test")

    with override_settings(INTEGRATION_ENCRYPTION_KEY=TEST_KEY_B):
        with pytest.raises(SecretDecryptionError) as caught:
            decrypt_secret(ciphertext)

    message = str(caught.value)
    assert message == STABLE_ERROR
    assert ciphertext not in message
    assert TEST_KEY_A not in message
    assert TEST_KEY_B not in message
    assert caught.value.__cause__ is None


@override_settings(INTEGRATION_ENCRYPTION_KEY="invalid-test-key")
def test_invalid_active_key_is_reported_as_stable_decryption_failure():
    with pytest.raises(SecretDecryptionError) as caught:
        decrypt_secret("legacy-token")

    assert str(caught.value) == STABLE_ERROR
    assert "invalid-test-key" not in str(caught.value)
    assert caught.value.__cause__ is None
