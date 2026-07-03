"""
Unit tests for AES-256-GCM Crypto Utilities (app/utils/crypto.py).

Tests cover:
  - Encrypt/decrypt roundtrip
  - Different passwords produce different ciphertexts
  - Wrong password raises an error
  - zero_bytes overwrites memory
  - Empty strings are handled
"""

import pytest

from app.utils.crypto import encrypt_env, decrypt_env, zero_bytes


class TestEncryptDecryptRoundtrip:
    """encrypt_env and decrypt_env work correctly together."""

    def test_basic_roundtrip(self):
        plaintext = "my secret seed phrase"
        password = "correct horse battery staple"
        cipher = encrypt_env(plaintext, password)
        assert cipher != plaintext
        assert decrypt_env(cipher, password) == plaintext

    def test_with_special_characters(self):
        plaintext = (
            "test test test test test test test test test test test junk!@#$%^&*()"
        )
        password = "p@ssw0rd!<>?"
        cipher = encrypt_env(plaintext, password)
        assert decrypt_env(cipher, password) == plaintext

    def test_with_unicode(self):
        plaintext = "héllo wörld 🔐"
        password = "pässwörd"
        cipher = encrypt_env(plaintext, password)
        assert decrypt_env(cipher, password) == plaintext

    def test_empty_string(self):
        plaintext = ""
        password = "password"
        cipher = encrypt_env(plaintext, password)
        assert decrypt_env(cipher, password) == ""

    def test_long_plaintext(self):
        plaintext = "x" * 10_000
        password = "test"
        cipher = encrypt_env(plaintext, password)
        assert decrypt_env(cipher, password) == plaintext


class TestEncryptionProperties:
    """Properties of the encryption scheme."""

    def test_different_passwords_produce_different_output(self):
        plaintext = "same text"
        c1 = encrypt_env(plaintext, "password1")
        c2 = encrypt_env(plaintext, "password2")
        assert c1 != c2

    def test_same_password_produces_different_output(self):
        """AES-GCM uses a random nonce, so same input produces different output."""
        plaintext = "test"
        password = "pass"
        c1 = encrypt_env(plaintext, password)
        c2 = encrypt_env(plaintext, password)
        assert c1 != c2

    def test_output_is_base64(self):
        """Output should be valid base64 (alphanumeric + / + =)."""
        import base64

        cipher = encrypt_env("test", "pass")
        # Should not raise
        base64.b64decode(cipher.encode())

    def test_output_minimum_length(self):
        """salt(16) + nonce(12) + ciphertext(>=1) + b64 overhead."""
        cipher = encrypt_env("a", "pass")
        assert len(cipher) > 40


class TestWrongPassword:
    """Decrypting with the wrong password should fail."""

    def test_wrong_password_raises_error(self):
        plaintext = "secret data"
        password = "correct password"
        wrong = "wrong password"
        cipher = encrypt_env(plaintext, password)
        with pytest.raises(Exception):
            decrypt_env(cipher, wrong)

    def test_tampered_ciphertext_raises_error(self):
        plaintext = "data"
        password = "pass"
        cipher = encrypt_env(plaintext, password)
        # Corrupt the ciphertext
        corrupted = cipher[:-1] + ("X" if cipher[-1:] != "X" else "Y")
        with pytest.raises(Exception):
            decrypt_env(corrupted, password)


class TestZeroBytes:
    """zero_bytes overwrites byte content."""

    def test_zeroes_bytearray(self):
        data = bytearray(b"secret key data")
        zero_bytes(data)
        assert all(b == 0 for b in data)

    def test_zeroes_bytes_converted(self):
        data = b"immutable bytes"
        # bytes should be converted to bytearray internally
        zero_bytes(data)
        # Original bytes object is immutable, but the function handles it
        # We just verify it doesn't crash
        assert isinstance(data, bytes)

    def test_empty_bytes_does_not_crash(self):
        zero_bytes(bytearray())
        zero_bytes(b"")
        assert True

    def test_large_buffer(self):
        data = bytearray(b"x" * 10_000)
        zero_bytes(data)
        assert all(b == 0 for b in data)
