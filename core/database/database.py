import sqlite3
import logging
from pathlib import Path
from typing import Optional, List
from datetime import datetime


logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/trading_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self) -> None:
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate_positions_table()
        logger.info(f"Database initialized: {self.db_path}")

    def _create_tables(self) -> None:
        cursor = self.conn.cursor()

        # Balance table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS balance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                free REAL NOT NULL,
                locked REAL NOT NULL,
                total REAL NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        """)

        # Active positions table - мінімальні дані для tracking.
        # close_price/realized_pnl/roe_percent — окремі колонки (не metadata JSON),
        # щоб звіти/статистика/повідомлення могли надійно читати їх напряму,
        # а не залежати від того, чи хтось коректно записав ці значення в JSON.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP,
                close_price REAL,
                realized_pnl REAL,
                roe_percent REAL,
                metadata TEXT
            )
        """)

        self.conn.commit()
        logger.info("Database tables created/verified")

    def _migrate_positions_table(self) -> None:
        """
        Для БД, створених до появи close_price/realized_pnl/roe_percent,
        CREATE TABLE IF NOT EXISTS новий стовпець не додасть — таблиця
        вже існує зі старою схемою. Тому перевіряємо PRAGMA table_info
        і додаємо відсутні колонки вручну, не чіпаючи наявні дані.
        """
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations = {
            'close_price': 'ALTER TABLE positions ADD COLUMN close_price REAL',
            'realized_pnl': 'ALTER TABLE positions ADD COLUMN realized_pnl REAL',
            'roe_percent': 'ALTER TABLE positions ADD COLUMN roe_percent REAL',
        }

        for column, ddl in migrations.items():
            if column not in existing_columns:
                logger.info(f"Migrating positions table: adding missing column '{column}'")
                cursor.execute(ddl)

        self.conn.commit()

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        return cursor

    def fetch_one(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchone()

    def fetch_all(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchall()

    def insert_balance(self, asset: str, free: float, locked: float) -> None:
        self.execute("""
            INSERT INTO balance (asset, free, locked, total, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (asset, free, locked, free + locked, datetime.utcnow()))

    def get_latest_balance(self, asset: str) -> Optional[sqlite3.Row]:
        return self.fetch_one("SELECT * FROM balance WHERE asset = ? ORDER BY timestamp DESC LIMIT 1", (asset,))

    def insert_position(self, order_id: str, symbol: str, side: str, status: str, metadata: str = None) -> int:
        """Зберігає мінімальні дані про відкриту позицію"""
        cursor = self.execute("""
            INSERT INTO positions (order_id, symbol, side, status, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (order_id, symbol, side, status, datetime.utcnow(), metadata))
        return cursor.lastrowid

    def update_position_status(
        self,
        order_id: str,
        status: str,
        closed_at: datetime = None,
        close_price: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        roe_percent: Optional[float] = None
    ) -> None:
        """
        Оновлює статус позиції. close_price/realized_pnl/roe_percent —
        опціональні: якщо не передані, відповідні колонки не чіпаються
        (щоб, наприклад, проміжний виклик update_position_status без цих
        даних не затер вже записані значення значенням NULL).
        """
        set_clauses = ["status = ?"]
        params: list = [status]

        if closed_at is not None:
            set_clauses.append("closed_at = ?")
            params.append(closed_at)
        if close_price is not None:
            set_clauses.append("close_price = ?")
            params.append(close_price)
        if realized_pnl is not None:
            set_clauses.append("realized_pnl = ?")
            params.append(realized_pnl)
        if roe_percent is not None:
            set_clauses.append("roe_percent = ?")
            params.append(roe_percent)

        params.append(order_id)

        self.execute(f"""
            UPDATE positions
            SET {', '.join(set_clauses)}
            WHERE order_id = ?
        """, tuple(params))

    def get_active_positions(self) -> List[sqlite3.Row]:
        """Повертає всі активні позиції"""
        return self.fetch_all("SELECT * FROM positions WHERE status = 'OPEN' ORDER BY created_at DESC")

    def update_position_metadata(self, order_id: str, metadata: str) -> None:
        self.execute("UPDATE positions SET metadata = ? WHERE order_id = ?", (metadata, order_id))

    def get_open_position_by_symbol_side(self, symbol: str, side: str) -> Optional[sqlite3.Row]:
        return self.fetch_one(
            "SELECT * FROM positions WHERE symbol = ? AND side = ? AND status = 'OPEN'",
            (symbol, side)
        )

    def get_closed_positions(self, limit: int = 5, offset: int = 0):
        return self.fetch_all("""
            SELECT * FROM positions
            WHERE status = 'CLOSED'
            ORDER BY closed_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

    def get_all_closed_positions(self):
        return self.fetch_all("SELECT * FROM positions WHERE status = 'CLOSED' ORDER BY closed_at DESC")

    def get_closed_positions_count(self) -> int:
        row = self.fetch_one("SELECT COUNT(*) as cnt FROM positions WHERE status = 'CLOSED'")
        return row['cnt'] if row else 0

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")