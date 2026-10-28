"""나무 PLUG 실시간 체결가(국내 mc · 미국 RC) → Tick 변환.

⚠️ 푸시 본문의 가격·수량 필드명은 아직 확인하지 못했다. FeedFields 로 주입받고,
   scripts/capture_feed.py 로 실제 푸시를 저장해 필드명을 확정한 뒤 설정한다.
   시각은 수신 시각을 쓴다(거래소 체결시각 필드 확정 전까지).
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from harness.models import Market

from .bars import Tick


@dataclass(frozen=True)
class FeedFields:
    price: str
    volume: str  # 이 체결의 수량 필드(누적거래량이 아닌 것)


def parse_push(msg: dict, fields: FeedFields, received_at: datetime | None = None, *,
               market: Market = Market.KR, key_to_symbol: dict[str, str] | None = None) -> Tick | None:
    header, body = msg.get("header"), msg.get("body")
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    key = str(header.get("tr_key") or "").strip()
    symbol = key_to_symbol.get(key, "") if key_to_symbol is not None else key
    try:
        price = Decimal(str(body[fields.price]).replace(",", "").lstrip("+-"))
        volume = int(str(body[fields.volume]).replace(",", ""))
    except (KeyError, InvalidOperation, ValueError):
        return None
    if not symbol or price <= 0 or volume < 0:
        return None
    return Tick(market, symbol, received_at or datetime.now(timezone.utc), price, volume)


class NHTickFeed:
    """SDK subscribe() 를 백그라운드 스레드로 돌리고 Tick 을 큐로 넘긴다."""

    def __init__(self, symbols: list[str], fields: FeedFields,
                 subscribe: Callable[..., int] | None = None, *, market: Market = Market.KR,
                 tr_cd: str = "mc", keys: dict[str, str] | None = None):
        """keys: {종목: 구독키}. 미국은 {티커: GIC}. 없으면 종목코드를 그대로 구독키로 쓴다(국내)."""
        if subscribe is None:
            from nhplug.realtime import subscribe as _sub
            subscribe = _sub
        self.symbols, self.fields, self._subscribe = symbols, fields, subscribe
        self.market, self.tr_cd = market, tr_cd
        self._keys = keys or {s: s for s in symbols}
        self._reverse = {v: k for k, v in self._keys.items()}
        self.ticks: queue.Queue[Tick] = queue.Queue()
        self.dropped = 0
        self._thread: threading.Thread | None = None

    def _on_message(self, msg: dict) -> None:
        t = parse_push(msg, self.fields, market=self.market, key_to_symbol=self._reverse)
        if t is None:
            self.dropped += 1
        else:
            self.ticks.put(t)

    def start(self, timeout: int = 120) -> None:
        self._thread = threading.Thread(
            target=self._subscribe, args=([self._keys[s] for s in self.symbols], self._on_message),
            kwargs={"tr_cd": self.tr_cd, "timeout": timeout}, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
