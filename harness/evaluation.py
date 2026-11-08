"""감사 로그 기반 성과·보정 리포트 (포워드 테스트 평가, docs §6).

- 매매 성과: 청산 손익으로 승률·손익비·최대낙폭(실현 기준)
- Jev 보정: 결정 시점의 thesis_aligned 확률 vs 이후 H분 수익률>0 여부 → Brier 점수와 구간별 적중률
  (H분은 판단 시각을 분 단위로 내린 시각부터 잰다. 결과는 결정 이후 봉에서만, 같은 거래일 안에서만 잡는다
   — 장 마감을 넘기면 표본에서 뺀다)
  (기준선 = 항상 평균 적중률을 말하는 예측. skill > 0 이어야 Jev 가 정보가 있다는 뜻)
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from harness.audit import AuditLog
from harness.models import Market
from harness.risk.sessions import SESSIONS


@dataclass
class TradeStats:
    trades: int
    total_krw: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_krw: float


@dataclass
class Calibration:
    n: int
    brier: float | None
    base_brier: float | None
    skill: float | None
    bins: list[tuple[str, int, float]] = field(default_factory=list)  # (구간, 표본수, 실제 적중률)


def trade_stats(audit: AuditLog, market: str | None = None) -> TradeStats:
    pnls = [float(Decimal(e["payload"]["pnl_krw"])) for e in audit.events("exit")
            if market is None or e["market"] == market]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    peak = cum = mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return TradeStats(
        trades=len(pnls), total_krw=sum(pnls),
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        profit_factor=(sum(wins) / sum(losses)) if losses else None,
        max_drawdown_krw=mdd,
    )


def _bar_index(audit: AuditLog) -> dict[str, tuple[list[datetime], list[float]]]:
    idx: dict[str, list[tuple[datetime, float]]] = {}
    for e in audit.events("bar"):
        idx.setdefault(e["symbol"], []).append(
            (datetime.fromisoformat(e["payload"]["start"]), float(e["payload"]["c"])))
    out = {}
    for sym, rows in idx.items():
        rows.sort()
        out[sym] = ([t for t, _ in rows], [c for _, c in rows])
    return out


def jev_calibration(audit: AuditLog, horizon_min: int = 15,
                    has_thesis: bool | None = None) -> Calibration:
    """실제 판단(decision)과 섀도 판단(shadow)을 함께 쓴다. has_thesis 로 논지 유무별 분리 가능."""
    bars = _bar_index(audit)
    pairs: list[tuple[float, int]] = []
    events = audit.events("decision") + audit.events("shadow")
    for e in events:
        with_thesis = e["payload"].get("has_thesis", True)
        if has_thesis is not None and with_thesis != has_thesis:
            continue
        d = e["payload"].get("detail") or {}
        answers = d.get("answers")
        if not answers or e["symbol"] not in bars:
            continue
        p = float(answers["thesis_aligned"]["noul"])
        entry = float(d["state"]["indicators"]["last"])
        times, closes = bars[e["symbol"]]
        # 운영 루프는 '다음 분 첫 틱 시각'(10:01:00.3)을, 리플레이는 봉 마감 시각(10:01:00)을 판단 시각으로 남긴다.
        # 분 단위로 내려 맞춰야 둘 다 H분 결과가 된다(안 내리면 실서버만 H+1분 뒤 봉을 라벨로 씀).
        ts = datetime.fromisoformat(e["ts"]).replace(second=0, microsecond=0)
        target = ts + timedelta(minutes=horizon_min - 1)
        i = bisect.bisect_left(times, target)
        if i >= len(times):
            continue  # 아직 결과를 모름
        day = SESSIONS[Market(e["market"])].trading_date
        if day(times[i]) != day(ts):
            continue  # 당일 장 안에 결과 봉이 없음 → 다음 날 가격(밤사이 갭)을 H분 결과로 쓰지 않는다
        pairs.append((p, 1 if closes[i] > entry else 0))

    if not pairs:
        return Calibration(0, None, None, None)
    n = len(pairs)
    rate = sum(y for _, y in pairs) / n
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    base = sum((rate - y) ** 2 for _, y in pairs) / n
    bins = []
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        hi = lo + 0.2
        ys = [y for p, y in pairs if lo <= p < hi or (hi >= 1.0 and p == 1.0)]
        if ys:
            bins.append((f"{lo:.1f}-{hi:.1f}", len(ys), sum(ys) / len(ys)))
    return Calibration(n, brier, base, (1 - brier / base) if base else None, bins)


def slippage_bps(audit: AuditLog) -> list[float]:
    """진입 체결가 vs 신호 가격(봉 종가) 차이, bp. 양수 = 불리하게 체결. 모의·실거래 괴리 점검용."""
    out = []
    for e in audit.events("fill"):
        p = e["payload"]
        sig = float(Decimal(str(p["signal_price"])))
        out.append((float(Decimal(str(p["price"]))) / sig - 1) * 10_000)
    return out
