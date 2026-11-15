"""Secrets-at-rest — the house pattern (decision-geocoder-token-locators.md §3 F1).

App-level Fernet over a key from .env (TALUS_ENCRYPTION_KEY): the key lives in .env, the ciphertext
in the DB — NEITHER ALONE IS THE SECRET. Chosen over pgcrypto deliberately: with pgp_sym_encrypt the
plaintext and the key transit the SQL layer as statement parameters, where log_statement /
pg_stat_statements could capture them; Fernet keeps plaintext in app memory only, so a pg_dump,
backup, or DB-side log never contains a usable credential.

First consumer: geocoder tokens (V073). Next planned: T43 off-host backup credentials — reuse THIS
module and THIS key; do not mint a second mechanism. Key rotation is documented-manual for v1
(re-enter secrets under the new key); MultiFernet is the known upgrade path once a second consumer
lands (§3 F7).

Callers map the typed errors per §3 F1b: no key at WRITE time → an actionable 422; decrypt failure
at READ time → skip that credential legibly (log + "credential unavailable"), never fail the wider
feature and never proceed without the credential.
"""
from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class SecretKeyUnavailable(Exception):
    """TALUS_ENCRYPTION_KEY is not set (or not a valid Fernet key). Write paths surface this as an
    actionable 422; air-gapped installs that never store a secret never need the key."""


class SecretDecryptError(Exception):
    """Ciphertext exists but the configured key can't open it — the key-loss/restore-mismatch case
    (wrong TALUS_ENCRYPTION_KEY for this DB's ciphertext). Read paths skip the credential, legibly."""


def _fernet() -> Fernet:
    key = settings.talus_encryption_key
    if not key:
        raise SecretKeyUnavailable(
            "TALUS_ENCRYPTION_KEY is not set — generate one "
            "(python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\") "
            "and add it to .env, then restart the api"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretKeyUnavailable(f"TALUS_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt_secret(plaintext: str) -> bytes:
    """Plaintext → Fernet ciphertext for a bytea column. Raises SecretKeyUnavailable (→ 422)."""
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    """Ciphertext → plaintext, in app memory only. Raises SecretKeyUnavailable / SecretDecryptError."""
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode()
    except InvalidToken as exc:
        raise SecretDecryptError(
            "stored ciphertext cannot be decrypted with the configured TALUS_ENCRYPTION_KEY "
            "(key changed since the secret was saved?)"
        ) from exc
