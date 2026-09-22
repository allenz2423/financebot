"""AES-256-GCM envelope encryption for the concierge vault.

The vault stores credentials as opaque ciphertext envelopes.  This module
owns the only primitive the vault uses to turn plaintext into an envelope
and back; nothing outside ``src.security.vault`` ever calls these functions
directly, which is what keeps the "no plaintext-returning API" invariant
structural rather than conventional.

Envelope format (single line, stable prefix):

    v1:<nonce_b64>:<ciphertext_b64>:<tag_b64>

The key is 32 bytes derived from the vault master key (SHA-256).  A fresh
random 12-byte nonce is generated per encryption, so equal plaintexts never
produce equal envelopes (required for credential confinement tests).
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional


class EncryptionError(ValueError):
    """Raised when an envelope cannot be decrypted or is malformed."""


PREFIX = "v1"


def derive_key(master_key: str) -> bytes:
    """Derive a 32-byte AES key from the vault master key string."""
    if not master_key:
        raise EncryptionError("vault master key is empty")
    return hashlib.sha256(master_key.encode("utf-8")).digest()


def encrypt_bytes(key: bytes, plaintext: bytes) -> str:
    """Encrypt bytes into a versioned envelope. Never returns plaintext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext, None)
    # Split: last 16 bytes are the GCM tag, the rest is ciphertext.
    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]
    return "{prefix}:{nonce}:{ct}:{tag}".format(
        prefix=PREFIX,
        nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
        ct=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        tag=base64.urlsafe_b64encode(tag).decode("ascii"),
    )


def decrypt_bytes(key: bytes, envelope: str) -> bytes:
    """Decrypt a versioned envelope back into bytes.

    Raises EncryptionError on malformed envelopes, unknown versions, or
    authentication failure (tampered ciphertext).  Callers must treat the
    return value as the highest-privilege object in the process.
    """
    parts = envelope.split(":", 3)
    if len(parts) != 4 or parts[0] != PREFIX:
        raise EncryptionError("malformed envelope")
    try:
        nonce = base64.urlsafe_b64decode(parts[1])
        ciphertext = base64.urlsafe_b64decode(parts[2])
        tag = base64.urlsafe_b64decode(parts[3])
    except Exception as exc:
        raise EncryptionError(f"envelope base64 invalid: {exc}") from exc

    if len(tag) != 16:
        raise EncryptionError("envelope tag length invalid")

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext + tag, None)
    except Exception as exc:
        raise EncryptionError(f"decrypt failed (tamper or wrong key): {type(exc).__name__}") from exc


def encrypt_str(key: bytes, plaintext: str) -> str:
    """Encrypt a UTF-8 string into an envelope."""
    return encrypt_bytes(key, plaintext.encode("utf-8"))


def decrypt_str(key: bytes, envelope: str) -> str:
    """Decrypt an envelope into a UTF-8 string."""
    return decrypt_bytes(key, envelope).decode("utf-8")


def is_envelope(value: object) -> bool:
    """True if the value looks like a vault envelope (never a plaintext secret)."""
    return isinstance(value, str) and value.startswith(PREFIX + ":")


__all__ = [
    "EncryptionError",
    "derive_key",
    "encrypt_bytes",
    "decrypt_bytes",
    "encrypt_str",
    "decrypt_str",
    "is_envelope",
    "PREFIX",
]