"""
Шифрування секретів користувачів (API-ключі BingX) у MongoDB.

Ключ шифрування — змінна оточення SECRETS_KEY (Fernet, base64 32 байти) і
НІКОЛИ не зберігається в базі: витік бази без змінних оточення Railway не дає
ключів користувачів. Згенерувати: python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())".
"""

from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_ENV_KEY = "SECRETS_KEY"


class SecretsKeyMissing(RuntimeError):
    pass


def _fernet(key: Optional[str] = None) -> Fernet:
    key = key or os.getenv(_ENV_KEY)
    if not key:
        raise SecretsKeyMissing(
            f"{_ENV_KEY} не задано — неможливо зберігати/читати API-ключі користувачів"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plain: str, key: Optional[str] = None) -> str:
    return _fernet(key).encrypt(plain.encode()).decode()


def decrypt_secret(token: str, key: Optional[str] = None) -> str:
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Не вдалося розшифрувати секрет — SECRETS_KEY змінився?") from e


def mask_key(api_key: str) -> str:
    """Для UI: лише останні 4 символи."""
    return api_key[-4:] if api_key else ""
