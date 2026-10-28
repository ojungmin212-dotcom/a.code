"""지표는 코드로 계산해 의사결정 계층에 넘긴다(LLM·Jev 에 계산을 맡기지 않는다)."""
from __future__ import annotations

from collections.abc import Sequence

from .bars import Bar


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def atr(bars: Sequence[Bar], n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        h, l, pc = float(cur.high), float(cur.low), float(prev.close)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def compute_indicators(bars: Sequence[Bar]) -> dict[str, float]:
    """최근 봉 기준 지표. 봉이 부족하면 있는 만큼으로 계산한다(호출자가 최소 봉 수를 보장)."""
    closes = [float(b.close) for b in bars]
    vols = [b.volume for b in bars]
    last = closes[-1]

    def ret(k: int) -> float:
        return _pct(last, closes[-k - 1]) if len(closes) > k else 0.0

    def ma(k: int) -> float:
        w = closes[-k:]
        return sum(w) / len(w)

    pv = sum(float(b.close) * b.volume for b in bars)
    tv = sum(vols)
    prior = vols[-21:-1]
    avg_vol = sum(prior) / len(prior) if prior else 0.0
    a = atr(bars)
    return {
        "last": last,
        "ret_5m_pct": round(ret(5), 4),
        "ret_15m_pct": round(ret(15), 4),
        "ma20": round(ma(20), 4),
        "dist_ma20_pct": round(_pct(last, ma(20)), 4),
        "vwap": round(pv / tv, 4) if tv else last,
        "dist_vwap_pct": round(_pct(last, pv / tv), 4) if tv else 0.0,
        "atr14": round(a, 4),
        "atr14_pct": round(a / last * 100, 4) if last else 0.0,
        "volume_ratio": round(vols[-1] / avg_vol, 4) if avg_vol else 0.0,
    }
