"""감사 로그와 영속 중복주문 방지 (SQLite).

모든 판단·주문·체결을 한 테이블에 시간순으로 남긴다. 사후 분석과 Jev 보정 측정의 원천 데이터.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness.broker.base import DuplicateOrderError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    market TEXT,
    symbol TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS client_orders (
    client_order_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);
"""


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "value"):
        return o.value
    raise TypeError(type(o).__name__)


class AuditLog:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def record(self, kind: str, payload: dict, *, market: str | None = None,
               symbol: str | None = None, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        with self._db:
            self._db.execute(
                "INSERT INTO events(ts, kind, market, symbol, payload) VALUES (?,?,?,?,?)",
                (ts.isoformat(), kind, market, symbol,
                 json.dumps(payload, ensure_ascii=False, default=_default)),
            )

    def events(self, kind: str | None = None) -> list[dict]:
        q = "SELECT ts, kind, market, symbol, payload FROM events"
        rows = self._db.execute(q + (" WHERE kind=? ORDER BY id" if kind else " ORDER BY id"),
                                (kind,) if kind else ()).fetchall()
        return [{"ts": r[0], "kind": r[1], "market": r[2], "symbol": r[3],
                 "payload": json.loads(r[4])} for r in rows]

    def guard(self) -> SqliteIdempotencyGuard:
        return SqliteIdempotencyGuard(self._db)


class SqliteIdempotencyGuard:
    """재시작해도 유지되는 client_order_id 중복 차단."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def claim(self, client_order_id: str) -> None:
        try:
            with self._db:
                self._db.execute("INSERT INTO client_orders(client_order_id, ts) VALUES (?,?)",
                                 (client_order_id, datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError as e:
            raise DuplicateOrderError(f"이미 제출된 client_order_id: {client_order_id}") from e
