from __future__ import annotations

import base64

import pytest

from hondo.crypto import TokenCipher, TokenCipherError, generate_key


def test_token_cipher_roundtrip() -> None:
    cipher = TokenCipher(generate_key())
    plaintext = "xoxb-secret-token"

    encrypted = cipher.encrypt(plaintext)

    assert encrypted != plaintext
    assert cipher.decrypt(encrypted) == plaintext


def test_token_cipher_rejects_token_encrypted_with_another_key() -> None:
    encrypted = TokenCipher(generate_key()).encrypt("xoxb-secret-token")

    with pytest.raises(TokenCipherError, match="decryption failed"):
        TokenCipher(generate_key()).decrypt(encrypted)


def test_token_cipher_rejects_a_corrupt_token() -> None:
    with pytest.raises(TokenCipherError, match="decryption failed"):
        TokenCipher(generate_key()).decrypt("corrupt-token")


@pytest.mark.parametrize("length", [72, 73, 4096, 65_536, 262_144])
def test_token_cipher_preserves_long_unicode_secrets(length: int) -> None:
    cipher = TokenCipher(generate_key())
    plaintext = "\u00e9" * length + "\U0001f511-end"

    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


def test_token_cipher_rejects_modified_ciphertext() -> None:
    cipher = TokenCipher(generate_key())
    encrypted = cipher.encrypt("a secret that must not be changed" * 100)
    decoded = bytearray(base64.urlsafe_b64decode(encrypted))
    decoded[len(decoded) // 2] ^= 1

    with pytest.raises(TokenCipherError, match="decryption failed"):
        cipher.decrypt(base64.urlsafe_b64encode(decoded).decode())


def test_token_cipher_rejects_malformed_key() -> None:
    with pytest.raises(TokenCipherError, match="key is malformed"):
        TokenCipher("not-a-fernet-key")


def test_generate_key_produces_a_usable_key() -> None:
    cipher = TokenCipher(generate_key())

    assert cipher.decrypt(cipher.encrypt("token")) == "token"
