import sqlite3
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
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

        # Orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_order_id TEXT UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL,
                stop_price REAL,
                status TEXT NOT NULL,
                filled_quantity REAL DEFAULT 0,
                average_price REAL,
                commission REAL DEFAULT 0,
                commission_asset TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                error_message TEXT,
                metadata TEXT
            )
        """)

        # Positions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                leverage INTEGER NOT NULL,
                margin REAL NOT NULL,
                liquidation_price REAL,
                unrealized_pnl REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                roi REAL DEFAULT 0,
                status TEXT NOT NULL,
                opened_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP,
                stop_loss_price REAL,
                take_profit_levels TEXT,
                metadata TEXT
            )
        """)

        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_trade_id TEXT UNIQUE,
                order_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                commission REAL NOT NULL,
                commission_asset TEXT NOT NULL,
                realized_pnl REAL DEFAULT 0,
                timestamp TIMESTAMP NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """)

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

        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """)

        # Events log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                source TEXT,
                data TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        """)

        # Errors log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS errors_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT,
                context TEXT,
                timestamp TIMESTAMP NOT NULL
            )
        """)

        # Performance metrics table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_commission REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                average_win REAL DEFAULT 0,
                average_loss REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                timestamp TIMESTAMP NOT NULL
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_log_type ON events_log(event_type)")

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

    def insert_order(self, order_data: Dict[str, Any]) -> int:
        cursor = self.execute("""
            INSERT INTO orders (
                exchange_order_id, symbol, side, order_type, quantity, price,
                stop_price, status, filled_quantity, average_price, commission,
                commission_asset, created_at, updated_at, error_message, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_data.get('exchange_order_id'),
            order_data['symbol'],
            order_data['side'],
            order_data['order_type'],
            order_data['quantity'],
            order_data.get('price'),
            order_data.get('stop_price'),
            order_data['status'],
            order_data.get('filled_quantity', 0),
            order_data.get('average_price'),
            order_data.get('commission', 0),
            order_data.get('commission_asset'),
            order_data.get('created_at', datetime.utcnow()),
            datetime.utcnow(),
            order_data.get('error_message'),
            order_data.get('metadata')
        ))
        return cursor.lastrowid

    def update_order(self, order_id: int, updates: Dict[str, Any]) -> None:
        updates['updated_at'] = datetime.utcnow()
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = tuple(updates.values()) + (order_id,)
        self.execute(f"UPDATE orders SET {set_clause} WHERE id = ?", values)

    def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[sqlite3.Row]:
        return self.fetch_one("SELECT * FROM orders WHERE exchange_order_id = ?", (exchange_order_id,))

    def get_open_orders(self) -> List[sqlite3.Row]:
        return self.fetch_all("SELECT * FROM orders WHERE status IN ('CREATED', 'SENT', 'ACCEPTED', 'PARTIALLY_FILLED')")

    def insert_position(self, position_data: Dict[str, Any]) -> int:
        cursor = self.execute("""
            INSERT INTO positions (
                symbol, side, entry_price, quantity, leverage, margin, liquidation_price,
                unrealized_pnl, realized_pnl, roi, status, opened_at, stop_loss_price,
                take_profit_levels, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position_data['symbol'],
            position_data['side'],
            position_data['entry_price'],
            position_data['quantity'],
            position_data['leverage'],
            position_data['margin'],
            position_data.get('liquidation_price'),
            position_data.get('unrealized_pnl', 0),
            position_data.get('realized_pnl', 0),
            position_data.get('roi', 0),
            position_data['status'],
            position_data.get('opened_at', datetime.utcnow()),
            position_data.get('stop_loss_price'),
            position_data.get('take_profit_levels'),
            position_data.get('metadata')
        ))
        return cursor.lastrowid

    def update_position(self, position_id: int, updates: Dict[str, Any]) -> None:
        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = tuple(updates.values()) + (position_id,)
        self.execute(f"UPDATE positions SET {set_clause} WHERE id = ?", values)

    def get_open_positions(self) -> List[sqlite3.Row]:
        return self.fetch_all("SELECT * FROM positions WHERE status = 'OPEN'")

    def get_position_by_symbol(self, symbol: str, side: str) -> Optional[sqlite3.Row]:
        return self.fetch_one("SELECT * FROM positions WHERE symbol = ? AND side = ? AND status = 'OPEN'", (symbol, side))

    def insert_trade(self, trade_data: Dict[str, Any]) -> int:
        cursor = self.execute("""
            INSERT INTO trades (
                exchange_trade_id, order_id, symbol, side, price, quantity,
                commission, commission_asset, realized_pnl, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_data.get('exchange_trade_id'),
            trade_data.get('order_id'),
            trade_data['symbol'],
            trade_data['side'],
            trade_data['price'],
            trade_data['quantity'],
            trade_data['commission'],
            trade_data['commission_asset'],
            trade_data.get('realized_pnl', 0),
            trade_data.get('timestamp', datetime.utcnow())
        ))
        return cursor.lastrowid

    def insert_balance(self, asset: str, free: float, locked: float) -> None:
        self.execute("""
            INSERT INTO balance (asset, free, locked, total, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (asset, free, locked, free + locked, datetime.utcnow()))

    def get_latest_balance(self, asset: str) -> Optional[sqlite3.Row]:
        return self.fetch_one("SELECT * FROM balance WHERE asset = ? ORDER BY timestamp DESC LIMIT 1", (asset,))

    def save_setting(self, key: str, value: str) -> None:
        self.execute("""
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, datetime.utcnow()))

    def get_setting(self, key: str) -> Optional[str]:
        row = self.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row['value'] if row else None

    def log_event(self, event_type: str, source: str, data: str) -> None:
        self.execute("""
            INSERT INTO events_log (event_type, source, data, timestamp)
            VALUES (?, ?, ?, ?)
        """, (event_type, source, data, datetime.utcnow()))

    def log_error(self, error_type: str, message: str, traceback: str = None, context: str = None) -> None:
        self.execute("""
            INSERT INTO errors_log (error_type, message, traceback, context, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (error_type, message, traceback, context, datetime.utcnow()))

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
