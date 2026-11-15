"""Secrets-at-rest house pattern (decision-geocoder-token-locators.md §3 F1/F1b) — unit tests.

Pure app-level tests (no DB, no HTTP): the encrypt/decrypt round-trip, and the two typed failure
modes callers key their behavior on — no key (write path → 422) and wrong key (read path → skip
legibly). The end-to-end write-only/negative sweep is scripts/verify-geocoder-token.sh.
"""
import pytest
from cryptography.fernet import Fernet

from app import secrets as secrets_mod
from app.secrets import (
    SecretDecryptError,
    SecretKeyUnavailable,
    decrypt_secret,
    encrypt_secret,
)


@pytest.fixture
def with_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(secrets_mod.settings, "talus_encryption_key", key)
    return key


def test_round_trip(with_key):
    cipher = encrypt_secret("s3cret-token-9876")
    assert isinstance(cipher, bytes)
    assert b"s3cret-token-9876" not in cipher  # ciphertext never contains the plaintext
    assert decrypt_secret(cipher) == "s3cret-token-9876"


def test_no_key_raises_unavailable(monkeypatch):
    monkeypatch.setattr(secrets_mod.settings, "talus_encryption_key", None)
    with pytest.raises(SecretKeyUnavailable) as exc:
        encrypt_secret("anything")
    # The message must be actionable (names the env var) — it becomes the admin-facing 422.
    assert "TALUS_ENCRYPTION_KEY" in str(exc.value)


def test_malformed_key_raises_unavailable(monkeypatch):
    monkeypatch.setattr(secrets_mod.settings, "talus_encryption_key", "not-a-fernet-key")
    with pytest.raises(SecretKeyUnavailable):
        encrypt_secret("anything")


def test_wrong_key_raises_decrypt_error(with_key, monkeypatch):
    cipher = encrypt_secret("s3cret-token-9876")
    # Simulate the key-loss/restore-mismatch case (F1b): a DIFFERENT valid key at read time.
    monkeypatch.setattr(
        secrets_mod.settings, "talus_encryption_key", Fernet.generate_key().decode()
    )
    with pytest.raises(SecretDecryptError):
        decrypt_secret(cipher)


def test_decrypt_with_no_key_raises_unavailable(with_key, monkeypatch):
    cipher = encrypt_secret("s3cret-token-9876")
    monkeypatch.setattr(secrets_mod.settings, "talus_encryption_key", None)
    with pytest.raises(SecretKeyUnavailable):
        decrypt_secret(cipher)
