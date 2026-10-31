"""실시간 체결통보(국내 d2) → (주문번호, 체결수량, 체결가, 거부 여부). F2

필드는 NH 공식 명세 x-realtime-channels 의 푸시 예시 기준:
  orderno(주문번호) · concgty(체결수량, 0 채움 문자열) · concprc(체결가) · rejgb(거부구분, "0"=정상)
⚠️ rejgb·ucgb 코드표는 명세에 없다 — "0" 이 아니면 거부로 본다(보수적). 모의투자로 확인 필요.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class FillNotice:
    order_no: str
    symbol: str
    qty: int
    price: Decimal
    rejected: bool


def parse_fill_notice(msg: dict) -> FillNotice | None:
    body = msg.get("body") if isinstance(msg, dict) else None
    if not isinstance(body, dict) or not body.get("orderno"):
        return None
    try:
        qty = int(str(body.get("concgty") or "0").strip() or 0)
        price = Decimal(str(body.get("concprc") or "0").strip() or "0")
    except (ValueError, InvalidOperation):
        return None
    rejected = str(body.get("rejgb") or "0").strip() not in ("", "0")
    if not rejected and (qty <= 0 or price <= 0):
        return None  # 접수·확인 통보 등 체결이 아닌 메시지
    return FillNotice(str(body["orderno"]).strip(), str(body.get("issuecd") or "").strip(),
                      qty, price, rejected)


class NHNoticeFeed:
    """체결통보 구독(웹소켓 세션 1개 사용 — 시세 1개 + 통보 1개 = 앱키 한도 2개)."""

    def __init__(self, subscribe: Callable[..., int] | None = None, tr_cd: str = "d2"):
        if subscribe is None:
            from nhplug.realtime import subscribe as _sub
            subscribe = _sub
        self._subscribe, self.tr_cd = subscribe, tr_cd
        self.notices: queue.Queue[FillNotice] = queue.Queue()
        self._thread: threading.Thread | None = None

    def _on_message(self, msg: dict) -> None:
        n = parse_fill_notice(msg)
        if n is not None:
            self.notices.put(n)

    def start(self, timeout: int = 900) -> None:
        self._thread = threading.Thread(target=self._subscribe, args=([], self._on_message),
                                        kwargs={"tr_cd": self.tr_cd, "timeout": timeout}, daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
