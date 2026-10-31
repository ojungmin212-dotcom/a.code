"""틱 원본 기록(F13): data/ticks/YYYYMMDD-시장.csv — 모델 학습 컷오프 이후의 자체 데이터셋."""
from __future__ import annotations

from pathlib import Path
from typing import TextIO

from .bars import Tick


class TickRecorder:
    def __init__(self, directory: Path):
        self.dir = directory
        self._key: tuple[str, str] | None = None
        self._f: TextIO | None = None

    def write(self, t: Tick) -> None:
        key = (t.ts.strftime("%Y%m%d"), t.market.value)
        if key != self._key:
            self.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{key[0]}-{key[1]}.csv"
            new = not path.exists()
            self._f = path.open("a", encoding="utf-8", buffering=1)  # 줄 단위 버퍼(비정상 종료에도 보존)
            if new:
                self._f.write("ts,market,symbol,price,volume\n")
            self._key = key
        self._f.write(f"{t.ts.isoformat()},{t.market.value},{t.symbol},{t.price},{t.volume}\n")

    def close(self) -> None:
        if self._f:
            self._f.close()
        self._f, self._key = None, None
