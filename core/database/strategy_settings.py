import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .database import Database


logger = logging.getLogger(__name__)

DEFAULT_USER_ID = "default"


class StrategySettingsStore:
    """
    Зберігання динамічних параметрів стратегій у БД.

    На кожну пару (user_id, strategy_name) існує до двох рядків:
      - is_default=1 — заводські параметри, з якими стратегія постачається
        в коді. Створюються один раз через seed_defaults() при старті бота
        і надалі користувачем НЕ редагуються — це те, до чого можна
        "скинутися".
      - is_default=0 — поточні активні параметри, які реально
        використовує стратегія. Саме їх редагує користувач.

    user_id поки що завжди DEFAULT_USER_ID ("default") — колонка додана
    заздалегідь, щоб у майбутньому додати per-user редагування без міграції
    схеми.
    """

    def __init__(self, db: Database):
        self.db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                strategy_name TEXT NOT NULL,
                params TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                UNIQUE(user_id, strategy_name, is_default)
            )
        """)
        logger.info("strategy_settings table created/verified")

    # --- внутрішнє ---

    def _get_row(self, strategy_name: str, is_default: bool, user_id: str) -> Optional[dict]:
        row = self.db.fetch_one(
            "SELECT * FROM strategy_settings WHERE user_id = ? AND strategy_name = ? AND is_default = ?",
            (user_id, strategy_name, int(is_default))
        )
        return dict(row) if row else None

    # --- публічне API ---

    def seed_defaults(
        self,
        strategy_name: str,
        default_params: Dict[str, Any],
        user_id: str = DEFAULT_USER_ID
    ) -> None:
        """
        Викликається при старті бота для кожної стратегії з її "заводськими"
        (hardcoded в коді) параметрами. Ідемпотентна: якщо default-рядок вже
        є в БД — НЕ перезаписує його (щоб рестарт бота не затирав історію,
        якщо заводські параметри в коді хтось випадково змінив). Якщо
        активного рядка ще немає — ініціалізує його копією дефолтних.
        """
        now = datetime.utcnow()
        params_json = json.dumps(default_params)

        existing_default = self._get_row(strategy_name, is_default=True, user_id=user_id)
        if not existing_default:
            self.db.execute("""
                INSERT INTO strategy_settings (user_id, strategy_name, params, is_default, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
            """, (user_id, strategy_name, params_json, now, now))
            logger.info(f"Seeded default params for strategy '{strategy_name}' (user={user_id})")
        else:
            logger.debug(f"Default params for '{strategy_name}' already exist, skipping seed")

        existing_active = self._get_row(strategy_name, is_default=False, user_id=user_id)
        if not existing_active:
            self.db.execute("""
                INSERT INTO strategy_settings (user_id, strategy_name, params, is_default, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
            """, (user_id, strategy_name, params_json, now, now))
            logger.info(f"Initialized active params for strategy '{strategy_name}' (user={user_id}) from defaults")

    def get_params(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Optional[Dict[str, Any]]:
        """Повертає поточні активні параметри стратегії. None, якщо стратегія ще не засіяна seed_defaults()."""
        row = self._get_row(strategy_name, is_default=False, user_id=user_id)
        if not row:
            return None
        return json.loads(row['params'])

    def get_default_params(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Optional[Dict[str, Any]]:
        """Повертає заводські параметри стратегії (для показу юзеру 'ось так було спочатку')."""
        row = self._get_row(strategy_name, is_default=True, user_id=user_id)
        if not row:
            return None
        return json.loads(row['params'])

    def update_params(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        user_id: str = DEFAULT_USER_ID
    ) -> Dict[str, Any]:
        """
        Оновлює активні параметри стратегії. Заводські (is_default=1)
        параметри не чіпає — тому reset_to_default() і далі працюватиме
        коректно.
        """
        existing = self._get_row(strategy_name, is_default=False, user_id=user_id)
        if not existing:
            raise ValueError(
                f"No active settings for strategy '{strategy_name}' (user={user_id}) — "
                f"call seed_defaults() first"
            )

        now = datetime.utcnow()
        self.db.execute("""
            UPDATE strategy_settings
            SET params = ?, updated_at = ?
            WHERE user_id = ? AND strategy_name = ? AND is_default = 0
        """, (json.dumps(params), now, user_id, strategy_name))

        logger.info(f"Updated params for strategy '{strategy_name}' (user={user_id})")
        return params

    def reset_to_default(self, strategy_name: str, user_id: str = DEFAULT_USER_ID) -> Dict[str, Any]:
        """Копіює заводські параметри поверх активних. Повертає параметри, до яких відкотились."""
        default_row = self._get_row(strategy_name, is_default=True, user_id=user_id)
        if not default_row:
            raise ValueError(f"No default settings found for strategy '{strategy_name}' (user={user_id})")

        now = datetime.utcnow()
        self.db.execute("""
            UPDATE strategy_settings
            SET params = ?, updated_at = ?
            WHERE user_id = ? AND strategy_name = ? AND is_default = 0
        """, (default_row['params'], now, user_id, strategy_name))

        logger.info(f"Strategy '{strategy_name}' (user={user_id}) reset to default params")
        return json.loads(default_row['params'])

    def list_strategies(self, user_id: str = DEFAULT_USER_ID) -> List[Dict[str, Any]]:
        """Повертає всі активні налаштування стратегій для юзера (для UI/Telegram-меню)."""
        rows = self.db.fetch_all(
            "SELECT strategy_name, params, updated_at FROM strategy_settings WHERE user_id = ? AND is_default = 0 ORDER BY strategy_name",
            (user_id,)
        )
        return [
            {
                'strategy_name': row['strategy_name'],
                'params': json.loads(row['params']),
                'updated_at': row['updated_at'],
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