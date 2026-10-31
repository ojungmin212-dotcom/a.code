"""지수 벤치마크(F7): 같은 기간 KOSPI 를 그냥 들고 있었을 때와 비교한다.

데이터: FinanceDataReader(KS11) 우선, 실패 시 pykrx(1001). pykrx 는 최근 KRX 로그인
(KRX_ID·KRX_PW 환경변수)이 필요해졌다. 둘 다 실패하면 None — 리포트는 '지수 미확인'으로 표시.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from harness.audit import AuditLog
from harness.risk.sessions import SESSIONS
from harness.models import Market


@dataclass
class BenchmarkResult:
    start: date
    end: date
    strategy_pct: float
    index_pct: float | None
    source: str | None

    @property
    def excess_pct(self) -> float | None:
        return None if self.index_pct is None else self.strategy_pct - self.index_pct


def _fdr(start: date, end: date) -> list[float]:
    import FinanceDataReader as fdr
    df = fdr.DataReader("KS11", start.isoformat(), end.isoformat())
    return [float(x) for x in df["Close"].tolist()]


def _pykrx(start: date, end: date) -> list[float]:
    from pykrx import stock
    df = stock.get_index_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001")
    return [float(x) for x in df["종가"].tolist()]


def kospi_return_pct(start: date, end: date,
                     loaders: list[tuple[str, Callable[[date, date], list[float]]]] | None = None
                     ) -> tuple[float | None, str | None]:
    """시작일 '전일 종가'가 아니라 시작일 종가 기준 — 장중 전략이 첫날 들고 있지 않았던 몫은 보수적으로 뺀다."""
    for name, load in loaders or [("FinanceDataReader", _fdr), ("pykrx", _pykrx)]:
        try:
            closes = load(start, end)
        except Exception:
            continue
        if len(closes) >= 2:
            return (closes[-1] / closes[0] - 1) * 100, name
    return None, None


def compare(audit: AuditLog, capital_krw: Decimal, market: str = "KR",
            loaders=None) -> BenchmarkResult | None:
    exits = [e for e in audit.events("exit") if e["market"] == market]
    if not exits:
        return None
    sess = SESSIONS[Market(market)]
    days = [sess.trading_date(datetime.fromisoformat(e["ts"])) for e in exits]
    pnl = sum(float(Decimal(str(e["payload"]["pnl_krw"]))) for e in exits)
    idx, src = kospi_return_pct(min(days), max(days), loaders)
    return BenchmarkResult(min(days), max(days), pnl / float(capital_krw) * 100, idx, src)
