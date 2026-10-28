"""NH 나무 PLUG 브로커 어댑터.

안전장치(우회 금지):
1. **모의투자 서버(moapi.)에서만 주문.** nhplug SDK 의 기본 BASE_URL 은 *운영(api.)* 이다.
   운영 주문은 allow_live=True 와 환경변수 HARNESS_ALLOW_LIVE=I_ACCEPT_REAL_MONEY 가 모두 있어야 한다.
2. **dry_run 기본값 True.** 전송하지 않고 payload 만 돌려준다.
3. **성공 판정을 HTTP 200 으로 하지 않는다.** SDK 규약상 rsp_cd 비교도 금지.
   응답 Output_0 에서 주문번호 필드(order_no_field)를 확인해야만 ACCEPTED. 그 외는 UNKNOWN.
   ⚠️ 주문번호 필드명은 아직 확인하지 못했다 → 모의투자 첫 주문 응답(scripts/mock_smoke.py)으로 확정할 것.
4. **UNKNOWN·네트워크 오류는 자동 재시도하지 않는다.** 이미 접수됐을 수 있어 중복 주문이 된다.
5. **미국 주식 주문 미지원(P0).** SDK 샘플에 해외 매도 경로가 없어 청산 경로가 검증되지 않았다.
   청산 못 하는 포지션을 여는 것을 막기 위해 의도적으로 막았다.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from harness.models import Market, OrderRequest, OrderResult, OrderStatus, OrderType, Side

from .base import Guard, IdempotencyGuard

LIVE_ENV_VAR = "HARNESS_ALLOW_LIVE"
LIVE_ENV_VALUE = "I_ACCEPT_REAL_MONEY"

_KR_PATHS = {Side.BUY: "/krstock/order/v1/cashBuy", Side.SELL: "/krstock/order/v1/cashSell"}


class LiveTradingBlocked(RuntimeError):
    pass


def _host(base_url: str) -> str:
    return base_url.split("//")[-1].split("/")[0].split(":")[0].lower()


class NHPlugBroker:
    def __init__(self, account_no: str, *, dry_run: bool = True, allow_live: bool = False,
                 order_no_field: str | None = None,
                 call: Callable[..., Any] | None = None,
                 base_url: Callable[[], str] | None = None,
                 guard: Guard | None = None):
        if not account_no:
            raise ValueError("account_no 가 필요합니다")
        if call is None or base_url is None:
            import nhplug  # 지연 import: 테스트에서는 주입한다
            call = call or nhplug.call
            base_url = base_url or nhplug.get_base_url
        self.account_no = account_no
        self.dry_run = dry_run
        self.allow_live = allow_live
        self.order_no_field = order_no_field
        self._call = call
        self._base_url = base_url
        self._guard = guard or IdempotencyGuard()
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
        if req.market is not Market.KR:
            raise NotImplementedError("P0 는 국내주식만 지원합니다(미국 매도 경로 미검증)")
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
        msg = data.get("rsp_msg")
        out = data.get("Output_0")
        row = out[0] if isinstance(out, list) and out else out
        order_no = None
        if self.order_no_field and isinstance(row, dict):
            order_no = str(row.get(self.order_no_field) or "").strip() or None
        status = OrderStatus.ACCEPTED if order_no else OrderStatus.UNKNOWN
        return OrderResult(client_order_id=cid, status=status, broker_order_id=order_no,
                           message=msg, raw=data)

    # ------------------------------------------------------------------ 조회(원본)
    def raw_accounts(self) -> dict:
        return self._call("/n2/acctinfo", {})

    def raw_kr_balance(self) -> dict:
        """잔고 원본. 응답 필드 파싱은 모의 계좌 실측 후 P1 에서 구현."""
        return self._call("/krstock/inquiry/v1/balance", {
            "act_no": self.account_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
            "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1",
        })
