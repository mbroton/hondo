"""Token encryption boundary.

This is the one module allowed to touch token-encryption key material or import
``cryptography``. No other module may do either.
"""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

GENERATE_KEY_COMMAND = (
    "uv run python -c 'from hondo.crypto import generate_key; print(generate_key())'"
)


class TokenCipherError(Exception):
    """Token encryption configuration or decryption failed."""


class TokenCipher:
    def __init__(self, key: str | SecretStr) -> None:
        value = key.get_secret_value() if isinstance(key, SecretStr) else key
        try:
            self._fernet = Fernet(value.encode())
        except ValueError as exc:
            raise TokenCipherError("token encryption key is malformed") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise TokenCipherError("token decryption failed") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()
