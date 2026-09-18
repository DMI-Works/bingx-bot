import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING

from .database import Database


logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "default"


class StrategySettingsStore:
    """
    Зберігання динамічних параметрів стратегій у MongoDB
    (колекція strategy_settings).

    На кожну пару (user_id, strategy_name) існує до двох документів:
      - is_default=True — заводські параметри, з якими стратегія постачається
        в коді. Створюються один раз через seed_defaults() при старті бота
        і надалі користувачем НЕ редагуються — це те, до чого можна
        "скинутися".
      - is_default=False — поточні активні параметри, які реально
        використовує стратегія. Саме їх редагує користувач.

    Поле enabled зберігається лише на активному документі (is_default=False) —
    це прапорець "чи запускати цю стратегію взагалі", окремий від самих
    параметрів. Вмикається/вимикається через Telegram-меню.

    params зберігається як звичайний вкладений документ (не JSON-рядок,
    як було у SQLite-версії) — Mongo зберігає структуровані дані нативно.

    user_id поки що завжди DEFAULT_USER_ID ("default") — поле додано
    заздалегідь, щоб у майбутньому додати per-user редагування без міграції.
    """

    def __init__(self, db: Database):
        self.db = db
        self.collection = self.db.db.strategy_settings
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.collection.create_index(
            [
                ("user_id", ASCENDING),
                ("strategy_name", ASCENDING),
                ("is_default", ASCENDING),
            ],
            unique=True,
        )
        logger.info("strategy_settings indexes created/verified")

    # --- внутрішнє ---

    def _get_row(self, strategy_name: str, is_default: bool, user_id: str) -> Optional[dict]:
        return self.collection.find_one({
            "user_id": user_id,
            "strategy_name": strategy_name,
            "is_default": is_default,
        })

    # --- публічне API ---

    def seed_defaults(
        self,
        strategy_name: str,
        default_params: Dict[str, Any],
        enabled: bool = True,
        user_id: str = DEFAULT_USER_ID
    ) -> None:
        """
        Викликається при старті бота для кожної стратегії з її "заводськими"
        (hardcoded в коді) параметрами. Ідемпотентна: якщо default-документ
        вже є в БД — НЕ перезаписує його. Якщо активного документа ще
        немає — ініціалізує його копією дефолтних.

        enabled застосовується лише при ПЕРШОМУ створенні активного
        документа. Далі станом enabled керує виключно set_enabled().
        """
        now = datetime.utcnow()

        existing_default = self._get_row(strategy_name, True, user_id)
        if not existing_default:
            self.collection.insert_one({
                "user_id": user_id,
                "strategy_name": strategy_name,
                "params": default_params,
                "is_default": True,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            })
            logger.info(f"Seeded default params for strategy '{strategy_name}' (user={user_id})")
        else:
            logger.debug(f"Default params for '{strategy_name}' already exist, skipping seed")

        existing_active = self._get_row(strategy_name, False, user_id)
        if not existing_active:
            self.collection.insert_one({
                "user_id": user_id,
                "strategy_name": strategy_name,
                "params": default_params,
                "is_default": False,
                "enabled": enabled,
                "created_at": now,
                "updated_at": now,
            })
            logger.info(
                f"Initialized active params for strategy '{strategy_name}' "
                f"(user={user_id}) from defaults, enabled={enabled}"
            )

    def get_params(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Optional[Dict[str, Any]]:
        """Повертає поточні активні параметри стратегії. None, якщо стратегія ще не засіяна seed_defaults()."""
        row = self._get_row(strategy_name, False, user_id)
        return row["params"] if row else None

    def get_default_params(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Optional[Dict[str, Any]]:
        """Повертає заводські параметри стратегії."""
        row = self._get_row(strategy_name, True, user_id)
        return row["params"] if row else None

    def update_params(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        user_id: str = DEFAULT_USER_ID
    ) -> Dict[str, Any]:
        """
        Оновлює активні параметри стратегії. Заводські (is_default=True)
        параметри не чіпає. enabled теж не чіпає — це окремий прапорець.
        """
        existing = self._get_row(strategy_name, False, user_id)
        if not existing:
            raise ValueError(
                f"No active settings for strategy '{strategy_name}' (user={user_id}) — "
                f"call seed_defaults() first"
            )

        now = datetime.utcnow()
        self.collection.update_one(
            {"user_id": user_id, "strategy_name": strategy_name, "is_default": False},
            {"$set": {"params": params, "updated_at": now}},
        )

        logger.info(f"Updated params for strategy '{strategy_name}' (user={user_id})")
        return params

    def reset_to_default(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Dict[str, Any]:
        """Копіює заводські параметри поверх активних. enabled не чіпає. Повертає параметри, до яких відкотились."""
        default_row = self._get_row(strategy_name, True, user_id)
        if not default_row:
            raise ValueError(f"No default settings found for strategy '{strategy_name}' (user={user_id})")

        now = datetime.utcnow()
        self.collection.update_one(
            {"user_id": user_id, "strategy_name": strategy_name, "is_default": False},
            {"$set": {"params": default_row["params"], "updated_at": now}},
        )

        logger.info(f"Strategy '{strategy_name}' (user={user_id}) reset to default params")
        return default_row["params"]

    def is_enabled(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> bool:
        """Чи увімкнена стратегія (запускається при setup_strategies)."""
        row = self._get_row(strategy_name, False, user_id)
        if not row:
            return False
        return bool(row.get("enabled", False))

    def set_enabled(self, strategy_name: str, enabled: bool, user_id: str = DEFAULT_USER_ID) -> None:
        """Вмикає/вимикає стратегію (тумблер у Telegram-меню)."""
        existing = self._get_row(strategy_name, False, user_id)
        if not existing:
            raise ValueError(
                f"No active settings for strategy '{strategy_name}' (user={user_id}) — "
                f"call seed_defaults() first"
            )

        now = datetime.utcnow()
        self.collection.update_one(
            {"user_id": user_id, "strategy_name": strategy_name, "is_default": False},
            {"$set": {"enabled": enabled, "updated_at": now}},
        )

        logger.info(f"Strategy '{strategy_name}' (user={user_id}) enabled={enabled}")

    def list_strategies(self, user_id: str = DEFAULT_USER_ID) -> List[Dict[str, Any]]:
        """Повертає всі активні налаштування стратегій для юзера (для UI/Telegram-меню)."""
        rows = self.collection.find(
            {"user_id": user_id, "is_default": False}
        ).sort("strategy_name", ASCENDING)

        return [
            {
                "strategy_name": row["strategy_name"],
                "params": row["params"],
                "enabled": bool(row.get("enabled", False)),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def is_modified(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> bool:
        """Чи відрізняються активні параметри від заводських (для позначки 'змінено' в UI)."""
        active = self.get_params(strategy_name, user_id)
        default = self.get_default_params(strategy_name, user_id)
        if active is None or default is None:
            return False
        return active != default