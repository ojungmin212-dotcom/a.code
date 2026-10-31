"""NH 나무 PLUG 브로커 어댑터.

안전장치(우회 금지):
1. **모의투자 서버(moapi.)에서만 주문.** nhplug SDK 의 기본 BASE_URL 은 *운영(api.)* 이다.
   운영 주문은 allow_live=True 와 환경변수 HARNESS_ALLOW_LIVE=I_ACCEPT_REAL_MONEY 가 모두 있어야 한다.
2. **dry_run 기본값 True.** 전송하지 않고 payload 만 돌려준다.
3. **성공 판정을 HTTP 200 으로 하지 않는다.** SDK 규약상 rsp_cd 비교도 금지.
   응답 Output_0 에서 주문번호 필드(order_no_fields 중 하나)를 확인해야만 ACCEPTED. 그 외는 UNKNOWN.
   필드명은 NH 공식 명세 기준(국내 mkt_orr_no · 미국 orr_no) — scripts/check_all.py 로 실측 확인.
4. **UNKNOWN·네트워크 오류는 자동 재시도하지 않는다.** 이미 접수됐을 수 있어 중복 주문이 된다.
5. **미국 주식은 매도 경로(us.sell_path)가 설정돼야만 매수도 허용.** SDK 샘플에 해외 매도 경로가 없어
   모의투자로 확인해 config/broker_fields.yaml 에 넣어야 한다. 청산 못 하는 포지션을 여는 것을 막는다.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from harness.models import Market, OrderRequest, OrderResult, OrderStatus, OrderType, Side

from .base import Guard, IdempotencyGuard

LIVE_ENV_VAR = "HARNESS_ALLOW_LIVE"
LIVE_ENV_VALUE = "I_ACCEPT_REAL_MONEY"

_KR_PATHS = {Side.BUY: "/krstock/order/v1/cashBuy", Side.SELL: "/krstock/order/v1/cashSell"}


class LiveTradingBlocked(RuntimeError):
    pass


class NotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class BalanceFields:
    holdings_list: str
    symbol_field: str
    quantity_field: str
    avg_price_field: str


@dataclass(frozen=True)
class UsConfig:
    sell_path: str                      # 미확인 — 모의투자로 검증 후 설정
    balance_fields: BalanceFields | None = None
    nation_code: str = "200"            # 200 = 미국


def server_message(data: Any) -> str | None:
    """서버 메시지. SDK 문서는 rsp_msg, 공식 명세는 message.usr_msg — 둘 다 본다(판정용 아님, 기록용)."""
    if not isinstance(data, dict):
        return None
    m = data.get("message")
    parts = [data.get("rsp_msg")]
    if isinstance(m, dict):
        parts += [m.get("usr_msg"), m.get("msg_code")]
    text = " | ".join(str(x) for x in parts if x)
    return text or None


def _num(v: Any) -> Decimal:
    return Decimal(str(v).replace(",", "").strip() or "0")


def _host(base_url: str) -> str:
    return base_url.split("//")[-1].split("/")[0].split(":")[0].lower()


class NHPlugBroker:
    def __init__(self, account_no: str, *, dry_run: bool = True, allow_live: bool = False,
                 order_no_field: str | tuple[str, ...] | list[str] | None = None,
                 call: Callable[..., Any] | None = None,
                 base_url: Callable[[], str] | None = None,
                 guard: Guard | None = None,
                 balance_fields: BalanceFields | None = None,
                 us: UsConfig | None = None):
        if not account_no:
            raise ValueError("account_no 가 필요합니다")
        if call is None or base_url is None:
            import nhplug  # 지연 import: 테스트에서는 주입한다
            call = call or nhplug.call
            base_url = base_url or nhplug.get_base_url
        self.account_no = account_no
        self.dry_run = dry_run
        self.allow_live = allow_live
        if isinstance(order_no_field, str):
            order_no_field = (order_no_field,)
        self.order_no_fields = tuple(order_no_field or ())
        self._call = call
        self._base_url = base_url
        self._guard = guard or IdempotencyGuard()
        self.balance_fields = balance_fields
        self.us = us
        self._assert_environment()

    # ------------------------------------------------------------------ 환경 가드
    def is_mock(self) -> bool:
        return _host(self._base_url()).startswith("moapi.")

    def _assert_environment(self) -> None:
        if self.is_mock():
            return
        if self.allow_live and os.environ.get(LIVE_ENV_VAR) == LIVE_ENV_VALUE:
            return
        raise LiveTradingBlocked(
            f"운영 서버({_host(self._base_url())})로는 주문할 수 없습니다. "
            f"NHPLUG_BASE_URL=https://moapi.nhplug.com:8443 로 설정하세요."
        )

    # ------------------------------------------------------------------ 주문
    def build_payload(self, req: OrderRequest) -> tuple[str, dict]:
        if req.market is Market.US:
            return self._build_us(req)
        is_market = req.order_type is OrderType.MARKET
        input_0: dict[str, Any] = {
            "act_no": self.account_no,
            "iem_cd": req.symbol,
            "orr_qty": req.quantity,
            "nmn_pr_tp_cd": "05" if is_market else "01",   # 05 시장가 / 01 보통가(지정가)
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
        if not is_market:
            if req.limit_price != req.limit_price.to_integral_value():
                raise ValueError("국내주식 지정가는 원 단위 정수여야 합니다")
            input_0["orr_pr"] = int(req.limit_price)
        return _KR_PATHS[req.side], input_0

    def _build_us(self, req: OrderRequest) -> tuple[str, dict]:
        if self.us is None:
            raise NotImplementedError("미국 매도 경로 미설정 → 미국 주문 비활성 (config/broker_fields.yaml us.sell_path)")
        is_market = req.order_type is OrderType.MARKET
        input_0: dict[str, Any] = {
            "act_no": self.account_no,
            "fc_sec_trd_nat_cd": self.us.nation_code,
            "iem_cd": req.symbol,                              # 티커(AAPL)
            "orr_qty": req.quantity,
            "ahi_nmn_pr_tp_cd": "03" if is_market else "00",   # 03 시장가 / 00 지정가
            "wtm_cur_knd_cd": "2",                             # 2 = 원화(통합증거금)
        }
        if not is_market:
            input_0["fc_orr_uit_pr"] = float(req.limit_price.quantize(Decimal("0.01")))
        path = "/gbstock/order/v1/buy" if req.side is Side.BUY else self.us.sell_path
        return path, input_0

    def place_order(self, req: OrderRequest) -> OrderResult:
        path, input_0 = self.build_payload(req)   # 지원하지 않는 주문은 id 를 소모하기 전에 실패
        self._guard.claim(req.client_order_id)
        cid = req.client_order_id
        if self.dry_run:
            return OrderResult(client_order_id=cid, status=OrderStatus.DRY_RUN,
                               raw={"path": path, "Input_0": input_0})

        self._assert_environment()  # 전송 직전 재확인(.env 가 바뀌었을 수 있음)

        from nhplug import NhplugError
        try:
            data = self._call(path, input_0)
        except NhplugError as e:
            # config·auth·rate_limit 은 서버가 주문을 처리하기 전에 막힌 것. 나머지는 접수 여부 불명.
            not_processed = e.category in ("config", "auth", "rate_limit")
            return OrderResult(client_order_id=cid,
                               status=OrderStatus.REJECTED if not_processed else OrderStatus.UNKNOWN,
                               message=str(e), raw=e.raw if isinstance(e.raw, dict) else None)

        return self._interpret(cid, data)

    def _interpret(self, cid: str, data: Any) -> OrderResult:
        if not isinstance(data, dict):
            return OrderResult(client_order_id=cid, status=OrderStatus.UNKNOWN,
                               message=f"예상치 못한 응답: {str(data)[:200]}")
        msg = server_message(data)
        out = data.get("Output_0")
        row = out[0] if isinstance(out, list) and out else out
        order_no = None
        if isinstance(row, dict):
            for f in self.order_no_fields:
                v = str(row.get(f) or "").strip()
                if v and v.strip("0"):  # 0 / 빈 값은 주문번호가 아님
                    order_no = v
                    break
        status = OrderStatus.ACCEPTED if order_no else OrderStatus.UNKNOWN
        return OrderResult(client_order_id=cid, status=status, broker_order_id=order_no,
                           message=msg, raw=data)

    def cancel(self, market: Market, symbol: str, order_no: str) -> dict:
        """미체결 잔량 전량 취소. dry_run 이면 payload 만 돌려준다. 결과는 판정하지 않고 원문 반환."""
        if market is Market.KR:
            path, input_0 = "/krstock/order/v1/cancel", {
                "act_no": self.account_no, "org_mkt_orr_no": int(order_no),
                "all_pat_dit_cd": "1", "iem_cd": symbol}
        else:
            if self.us is None:
                raise NotImplementedError("미국 주문 비활성")
            path, input_0 = "/gbstock/order/v1/cancel", {
                "act_no": self.account_no, "org_orr_no": int(order_no),
                "fc_sec_trd_nat_cd": self.us.nation_code, "iem_cd": symbol, "all_pat_dit_cd": "1"}
        if self.dry_run:
            return {"dry_run": True, "path": path, "Input_0": input_0}
        self._assert_environment()
        return self._call(path, input_0)

    # ------------------------------------------------------------------ 조회(원본)
    def raw_accounts(self) -> dict:
        return self._call("/n2/acctinfo", {})

    def raw_kr_balance(self) -> dict:
        """잔고 원본. 응답 필드 파싱은 모의 계좌 실측 후 P1 에서 구현."""
        return self._call("/krstock/inquiry/v1/balance", {
            "act_no": self.account_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
            "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1",
        })

    def raw_us_balance(self) -> dict:
        nat = self.us.nation_code if self.us else "200"
        return self._call("/gbstock/inquiry/v1/balance", {
            "act_no": self.account_no, "qut_iqr_dit_cd": "9", "fc_sec_trd_nat_cd": nat,
            "cur_cd": "USD", "xns_dit_cd": "1",
        })

    @staticmethod
    def _parse_holdings(data: Any, f: BalanceFields, market: Market) -> dict[tuple[Market, str], tuple[int, Decimal]]:
        if not isinstance(data, dict) or f.holdings_list not in data:
            msg = server_message(data) if isinstance(data, dict) else data
            raise RuntimeError(f"{market} 잔고 응답에 {f.holdings_list} 없음: {msg}")
        rows = data[f.holdings_list] or []
        rows = rows if isinstance(rows, list) else [rows]
        out: dict[tuple[Market, str], tuple[int, Decimal]] = {}
        for r in rows:
            sym = str(r[f.symbol_field]).strip()
            qty = int(_num(r[f.quantity_field]))
            if sym and qty > 0:
                out[(market, sym)] = (qty, _num(r[f.avg_price_field]))
        return out

    def holdings(self) -> dict[tuple[Market, str], tuple[int, Decimal]]:
        """보유 잔고 → {(시장, 종목): (수량, 평균단가)}. 형식이 기대와 다르면 예외(조용히 0 으로 보지 않음).

        미국 잔고 필드가 설정돼 있으면 함께 조회한다. 평균단가는 현지 통화 기준.
        """
        if self.balance_fields is None:
            raise NotConfigured("config/broker_fields.yaml 의 잔고 필드명이 비어 있습니다")
        out = self._parse_holdings(self.raw_kr_balance(), self.balance_fields, Market.KR)
        if self.us and self.us.balance_fields:
            out.update(self._parse_holdings(self.raw_us_balance(), self.us.balance_fields, Market.US))
        return out
