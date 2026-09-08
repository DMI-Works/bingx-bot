"""
Валидация Telegram Mini App initData.

Telegram подписывает данные пользователя HMAC-ключом, производным от вашего
bot token. Без этой проверки любой человек может открыть URL мини-аппа
напрямую в браузере и подделать заголовок с чужим telegram_id.

Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # тот же токен, что использует core/telegram
MAX_AGE_SECONDS = 3600  # initData считаем валидной 1 час


def validate_init_data(init_data: str) -> dict | None:
    if not init_data:
        return None

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > MAX_AGE_SECONDS:
        return None

    return json.loads(parsed["user"]) if "user" in parsed else None
