"""첫 실서버 점검 로직(F5·H1·H2). scripts/check_all.py 는 이 모듈을 실제 의존성으로 조립만 한다.

점검 항목: 모의 서버 · 계좌 구분 · 주문번호 필드 · 잔고 필드 · 원상복구 매도 ·
          **체결통보 주문번호 일치(H2-①)** · 실시간 필드 · **movolume 이 체결 1건 수량인지(H2-②)** · Jev · Grok
"""
from __future__ import annotations

import json
import queue
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.broker.nhplug_broker import NHPlugBroker, server_message
from harness.market.nh_feed import FeedFields
from harness.models import Market, OrderRequest, OrderStatus, OrderType, Side
from harness.risk import SESSIONS


@dataclass
class Deps:
    broker: NHPlugBroker
    feed_fields: FeedFields | None
    subscribe: Callable[..., int]                     # nhplug.realtime.subscribe 와 같은 시그니처
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    notice_feed: Callable[[], Any] | None = None      # NHNoticeFeed 와 같은 인터페이스(start · notices 큐)
    run_ping: Callable[[str], tuple[bool, str]] | None = None
    raw_dir: Path | None = None


@dataclass
class Report:
    results: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)        # 실측으로 확정한 사실(주문번호 일치 여부 등)
    order_ok: bool = False
    balance_ok: bool = False
    feed_ok: bool = False

    @property
    def apply_ok(self) -> bool:
        """verified_by_mock 을 true 로 바꿔도 되는가: 주문번호·잔고·실시간 필드와 거래량 의미가 모두 확인됨."""
        return self.order_ok and self.balance_ok and self.feed_ok


def _norm(v: object) -> str:
    return str(v or "").strip().lstrip("0")


def judge_volume(bodies: list[dict], vol_field: str, cum_field: str = "volume") -> tuple[bool | None, str]:
    """movolume(체결 1건 수량) 가정 검증: 연속 푸시의 누적거래량 증가분 == 다음 푸시의 movolume 인가.

    반환: (True=체결 1건 수량 / False=아님 / None=판정 불가, 설명)
    """
    rows = []
    for b in bodies:
        try:
            rows.append((int(str(b[cum_field]).replace(",", "")), int(str(b[vol_field]).replace(",", ""))))
        except (KeyError, ValueError):
            continue
    pairs = [(c2 - c1, m2) for (c1, _), (c2, m2) in zip(rows, rows[1:], strict=False) if c2 > c1]
    if len(pairs) < 3:
        return None, f"판정 불가 — 비교 가능한 연속 푸시 {len(pairs)}쌍(최소 3)"
    if all(m == c for c, m in rows):
        return False, f"{vol_field} 가 누적거래량({cum_field})과 같음 — 체결 1건 수량이 아님"
    hit = sum(1 for dv, m in pairs if dv == m)
    ratio = hit / len(pairs)
    if ratio >= 0.6:
        return True, f"누적 증가분과 일치 {hit}/{len(pairs)}쌍 — 체결 1건 수량"
    return False, f"누적 증가분과 일치 {hit}/{len(pairs)}쌍({ratio:.0%}) — 의미 불명확"


class Checker:
    def __init__(self, deps: Deps):
        self.d = deps
        self.r = Report()

    def step(self, name: str, ok: bool, detail: str, raw: object = None) -> bool:
        self.r.results.append({"name": name, "ok": ok, "detail": detail})
        print(f"{'✅' if ok else '❌'} {name}: {detail}", flush=True)
        if raw is not None and self.d.raw_dir is not None:
            self.d.raw_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^0-9A-Za-z가-힣]+", "_", name)
            (self.d.raw_dir / f"{len(self.r.results):02d}-{safe}.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return ok

    # ------------------------------------------------------------------ 잔고
    def _holding(self, symbol: str) -> tuple[bool, int, Any]:
        """(필드 일치, 수량, 원문)"""
        b = self.d.broker
        f = b.balance_fields
        if f is None:
            return False, 0, None
        data = b.raw_kr_balance()
        rows = data.get(f.holdings_list) if isinstance(data, dict) else None
        rows = rows if isinstance(rows, list) else ([rows] if rows else [])
        hit = [r for r in rows if isinstance(r, dict) and str(r.get(f.symbol_field, "")).strip() == symbol]
        if not hit:
            return False, 0, data
        missing = [k for k in (f.quantity_field, f.avg_price_field) if k not in hit[0]]
        try:
            qty = int(float(str(hit[0].get(f.quantity_field, 0)).replace(",", "") or 0))
        except ValueError:
            qty = 0
        return not missing, qty, data

    # ------------------------------------------------------------------ 실행
    def run(self, send: bool, symbol: str = "005930") -> Report:
        b = self.d.broker
        if not self.step("모의투자 서버", b.is_mock(), "moapi 확인"):
            return self.r
        try:
            acc = b.raw_accounts()
            rows = (acc.get("Output_0") or []) if isinstance(acc, dict) else []
            mine = [x for x in rows if str(x.get("acct_no")) == b.account_no]
            self.step("계좌", bool(mine) and str(mine[0].get("acct_type")) == "03",
                      f"acct_type={mine[0].get('acct_type') if mine else '없음'} (모의=03)", acc)
        except Exception as e:  # 점검은 다음 항목으로 계속
            self.step("계좌", False, str(e))

        if send:
            now = self.d.clock()
            kr = SESSIONS[Market.KR]
            if not (kr.is_open(now) and kr.minutes_since_open(now) >= 5 and kr.minutes_to_close(now) > 15):
                self.step("장중 확인", False,
                          "주문 점검은 국내 장중(09:05~15:15)에만 — 장외 시장가 주문은 다음 개장에 체결돼 계좌에 남는다")
            else:
                self._order_cycle(symbol)
        else:
            print("ℹ️  --send 없이 실행: 주문번호·잔고·체결통보 확인은 건너뜀")

        self._feed(symbol)
        if self.d.run_ping:
            for name in ("Jev", "Grok"):
                ok, out = self.d.run_ping(name)
                self.step(name, ok, out)
        return self.r

    def _order_cycle(self, symbol: str) -> None:
        b = self.d.broker
        notices = None
        if self.d.notice_feed is not None:
            notices = self.d.notice_feed()
            notices.start()
        cid = f"check-{uuid.uuid4().hex[:8]}"
        buy = b.place_order(OrderRequest(client_order_id=cid, market=Market.KR, symbol=symbol,
                                         side=Side.BUY, quantity=1, order_type=OrderType.MARKET))
        self.r.order_ok = self.step("주문번호 필드", buy.status is OrderStatus.ACCEPTED,
                                    f"{buy.status} 주문번호={buy.broker_order_id} · {buy.message}", buy.raw)
        qty, fields_ok, raw = 0, False, None
        try:
            for _ in range(6):  # 체결이 늦을 수 있어 최대 30초
                self.d.sleep(5)
                fields_ok, qty, raw = self._holding(symbol)
                if qty >= 1:
                    break
            if qty >= 1:
                self.r.balance_ok = self.step("잔고 필드", fields_ok,
                                              f"{symbol} {qty}주 · 필드 {'일치' if fields_ok else '누락'}", raw)
            else:
                self.step("잔고 필드", False, f"30초 안에 {symbol} 보유가 잔고에 보이지 않음 · {server_message(raw)}", raw)
        except Exception as e:
            self.step("잔고 조회", False, str(e))

        sell = None
        if buy.status in (OrderStatus.ACCEPTED, OrderStatus.UNKNOWN):
            # 수량을 못 읽어도 1주 매도는 시도(현금 계좌는 초과 매도 불가 → 보유 없으면 거부될 뿐)
            sell = b.place_order(OrderRequest(client_order_id=cid + "-s", market=Market.KR, symbol=symbol,
                                              side=Side.SELL, quantity=1, order_type=OrderType.MARKET))
            self.step("원상복구 매도", sell.status is OrderStatus.ACCEPTED, f"{sell.status} · {sell.message}", sell.raw)
            if sell.status is not OrderStatus.ACCEPTED or qty < 1:
                print("⚠️  모의계좌에 1주가 남았는지 HTS·MTS 로 확인하세요 — 남아 있으면 운영 시작 시 장부 불일치로 정지합니다.")
        if notices is not None:
            self._notices(notices, buy, sell)

    def _notices(self, feed: Any, buy, sell) -> None:
        """H2-①: 체결통보 orderno 가 주문 응답의 주문번호와 같은 번호 체계인가."""
        got = []
        for _ in range(10):  # 최대 10초 더 기다린다
            try:
                while True:
                    got.append(feed.notices.get_nowait())
            except queue.Empty:
                pass
            if len(got) >= 2:
                break
            self.d.sleep(1)
        ids = {_norm(x.broker_order_id) for x in (buy, sell) if x is not None and x.broker_order_id}
        received = [_norm(n.order_no) for n in got]
        if not got:
            self.r.facts["notice_order_no_matches"] = None
            self.step("체결통보 주문번호", False, "체결통보 미수신 — 잔고 대조(추정 체결가)로만 운영됨")
            return
        match = bool(ids & set(received))
        self.r.facts["notice_order_no_matches"] = match
        self.step("체결통보 주문번호", match,
                  f"주문번호 {sorted(ids)} · 통보 {received} → " + ("일치: 정확한 체결가 사용"
                  if match else "불일치: 체결통보 대조 불가 → strategy.yaml execution.fill_notices: false 권장"),
                  [n.__dict__ for n in got])

    def _feed(self, symbol: str) -> None:
        ff = self.d.feed_fields
        msgs: list[dict] = []
        try:
            self.d.subscribe([symbol], msgs.append, tr_cd="mc", max_messages=20, timeout=20)
        except Exception as e:
            self.step("실시간", False, str(e))
            return
        bodies = [m.get("body") or {} for m in msgs if isinstance(m, dict)]
        good = [x for x in bodies if ff and ff.price in x and ff.volume in x]
        fields_ok = self.step("실시간 필드", bool(good),
                              f"{len(msgs)}건 수신 · 필드 일치 {len(good)}건" if msgs else "0건(장 마감이면 정상)", msgs)
        if not (fields_ok and ff):
            return
        verdict, why = judge_volume(good, ff.volume)
        self.r.facts["volume_is_per_trade"] = verdict
        self.r.feed_ok = self.step("체결수량 의미", verdict is True, why)


def apply_verified(config_dir: Path) -> None:
    for name in ("broker_fields.yaml", "feed.yaml"):
        path = config_dir / name
        path.write_text(re.sub(r"(?m)^verified_by_mock: .*$", "verified_by_mock: true",
                               path.read_text(encoding="utf-8")), encoding="utf-8")
