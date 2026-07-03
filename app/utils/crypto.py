"""
AES-256-GCM encrypt/decrypt for secrets at rest.
Seed phrase and API keys are encrypted in .env.encrypted,
decrypted once at startup into memory, and zeroed on shutdown.
"""

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    return kdf.derive(password.encode())


def encrypt_env(plaintext: str, password: str) -> str:
    """Encrypt a plaintext string.  Returns base64(salt + nonce + ciphertext)."""
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    payload = base64.b64encode(salt + nonce + ciphertext).decode()
    return payload


def decrypt_env(payload: str, password: str) -> str:
    """Decrypt a payload produced by encrypt_env()."""
    raw = base64.b64decode(payload.encode())
    salt, nonce, ciphertext = raw[:16], raw[16:28], raw[28:]
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


def zero_bytes(b: bytearray) -> None:
    """Overwrite a MUTABLE bytearray in place to reduce memory-scraping window.

    Immutable ``bytes`` cannot be zeroed in place: copying into a new
    bytearray and zeroing the copy leaves the original untouched, which
    is a false sense of security. Callers must pass a bytearray.
    """
    if isinstance(b, bytes):
        raise TypeError(
            "zero_bytes requires a mutable bytearray; immutable bytes "
            "cannot be scrubbed in place."
        )
    for i in range(len(b)):
        b[i] = 0
