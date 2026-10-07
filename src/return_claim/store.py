"""SQLite 存储层：单连接 + 显式写事务，保证批准退货等复合操作的原子性。"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE defect_batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no    TEXT NOT NULL UNIQUE,
    material    TEXT NOT NULL,
    supplier    TEXT NOT NULL,
    defect_qty  TEXT NOT NULL,
    unit_price  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE return_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no    TEXT NOT NULL UNIQUE,
    state       TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE return_line (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES return_order(id),
    batch_id     INTEGER NOT NULL REFERENCES defect_batch(id),
    request_qty  TEXT NOT NULL,
    unit_price   TEXT NOT NULL,
    state        TEXT NOT NULL,
    close_reason TEXT
);

-- 库存流水：隔离区/在途/良品仓/已退供应商 的数量变动，全部留痕。
CREATE TABLE inventory_movement (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES defect_batch(id),
    line_id     INTEGER REFERENCES return_line(id),
    account     TEXT NOT NULL,
    qty_delta   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    basis_type  TEXT NOT NULL,
    basis_id    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 索赔分录：只追加、不修改；qty/amount 带符号（计提为正，冲减为负）。
CREATE TABLE claim_entry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id     INTEGER NOT NULL REFERENCES return_line(id),
    kind        TEXT NOT NULL,
    qty         TEXT NOT NULL,
    unit_price  TEXT NOT NULL,
    amount      TEXT NOT NULL,
    basis_type  TEXT NOT NULL,
    basis_id    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE logistics_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id     INTEGER NOT NULL REFERENCES return_line(id),
    kind        TEXT NOT NULL,
    qty         TEXT NOT NULL,
    carrier     TEXT NOT NULL DEFAULT '',
    tracking_no TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE liability_determination (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id        INTEGER NOT NULL REFERENCES return_line(id),
    determined_qty TEXT NOT NULL,
    reason         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE compensation_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id     INTEGER NOT NULL REFERENCES return_line(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class Store:
    """仓储：持有唯一连接，写操作必须走 transaction()。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：块内任一异常即整体回滚，保证库存与索赔联动调整的原子性。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """读临界区：与写事务互斥，避免并发读写同一连接。"""
        with self._lock:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()
