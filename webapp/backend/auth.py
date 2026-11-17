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
MAX_CLOCK_SKEW_SECONDS = 60  # auth_date из будущего — лишь в пределах расхождения часов


def validate_init_data(init_data: str) -> dict | None:
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None  # битий рядок — це 401, а не 500
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(parsed.get("auth_date", 0))
        user = json.loads(parsed["user"]) if "user" in parsed else None
    except (TypeError, ValueError):
        return None
    age = time.time() - auth_date
    if age > MAX_AGE_SECONDS or age < -MAX_CLOCK_SKEW_SECONDS:
        return None
    return user if isinstance(user, dict) else None
