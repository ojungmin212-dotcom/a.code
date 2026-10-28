"""논지 저장소(SQLite). 리서치 프로세스가 쓰고, Jev·비교군 매매 프로세스가 같은 논지를 읽는다
(같은 논지로 비교해야 공정하고, Grok 비용도 한 번만 든다)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .thesis import Thesis, ThesisStore


class ThesisRepo:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS theses (id INTEGER PRIMARY KEY, "
                         "valid_until TEXT NOT NULL, body TEXT NOT NULL)")

    def add(self, t: Thesis) -> None:
        with self._db:
            self._db.execute("INSERT INTO theses(valid_until, body) VALUES (?,?)",
                             (t.valid_until.isoformat(), t.model_dump_json()))

    def expire(self, market: str, symbol: str, now: datetime) -> None:
        """새 리서치가 논지를 만들지 못하면 기존 논지도 즉시 끝낸다(낡은 근거로 매매 금지)."""
        rows = self._db.execute("SELECT id, body FROM theses").fetchall()
        with self._db:
            for rid, body in rows:
                t = Thesis.model_validate_json(body)
                if t.market.value == market and t.symbol == symbol and t.valid_until > now:
                    t2 = t.model_copy(update={"valid_until": max(now, t.valid_from)})
                    self._db.execute("UPDATE theses SET valid_until=?, body=? WHERE id=?",
                                     (t2.valid_until.isoformat(), t2.model_dump_json(), rid))

    def store(self) -> ThesisStore:
        return ThesisStore([Thesis.model_validate_json(b)
                            for (b,) in self._db.execute("SELECT body FROM theses")])
