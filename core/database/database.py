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

        # Active positions table - мінімальні дані для tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP,
                metadata TEXT
            )
        """)

        self.conn.commit()
        logger.info("Database tables created/verified")

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

    def update_position_status(self, order_id: str, status: str, closed_at: datetime = None) -> None:
        """Оновлює статус позиції"""
        if closed_at:
            self.execute("""
                UPDATE positions
                SET status = ?, closed_at = ?
                WHERE order_id = ?
            """, (status, closed_at, order_id))
        else:
            self.execute("""
                UPDATE positions
                SET status = ?
                WHERE order_id = ?
            """, (status, order_id))

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
