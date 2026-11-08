"""인과성(미래 정보 누출 금지) 테스트.

판단 시각 t 의 결정은 t 에 이미 '마감된' 데이터만으로 정해져야 한다. 확인하는 성질:
1. 1분봉 집계: 봉은 그 분이 끝난 뒤에만 나오고, 봉을 내보낸 틱(다음 분 첫 틱)과 그 뒤 틱은 담지 않는다.
2. 엔진 전체(리플레이): t 이후 봉을 아무렇게나 바꿔도 t 까지의 감사 기록(판단·심사·주문·체결·청산)이 같다.
   Jev 에 넘기는 상태(build_state)에는 t 에 이미 마감된 봉만 들어간다.
3. 논지: t 이후에 시작하는 논지는 t 의 판단에 영향을 주지 않는다.
4. Jev 보정 라벨: 결과는 결정 이후 봉에서만, 같은 거래일 안에서만 잡는다(밤사이 갭을 H분 결과로 쓰지 않음).
"""
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import accumulate

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from harness.audit import AuditLog
from harness.decision.policy import JevPolicy, StrategyConfig
from harness.evaluation import jev_calibration
from harness.market.bars import Bar, BarAggregator, Tick
from harness.models import Market
from harness.replay import run_replay
from harness.research.thesis import Thesis, ThesisStore
from harness.risk import RiskLimits

from .conftest import KST, ROOT

LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")
CFG = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
MIN = timedelta(minutes=1)
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=KST)  # 수요일 거래일 개장
SYMS = ("005930", "000660")


# ---------------------------------------------------------------- 1. 1분봉 집계
@st.composite
def tick_streams(draw):
    """수신 순서대로의 틱. 가끔 늦게 도착한 틱(시각이 앞선 틱)을 섞는다."""
    out, sec = [], 0
    for _ in range(draw(st.integers(1, 120))):
        sec += draw(st.integers(0, 40))
        lag = draw(st.sampled_from([0] * 8 + [1, 30, 75]))
        out.append(Tick(Market.KR, draw(st.sampled_from(SYMS)), T0 + timedelta(seconds=max(0, sec - lag)),
                        Decimal(draw(st.integers(1_000, 1_100))), draw(st.integers(1, 50))))
    return out


@settings(max_examples=300, deadline=None)
@given(tick_streams())
def test_bar_is_emitted_only_after_close_without_later_ticks(ticks):
    agg = BarAggregator()
    last_start: dict[tuple[Market, str], datetime] = {}
    for j, tick in enumerate(ticks):
        bar = agg.add(tick)
        if bar is None:
            continue
        key = (bar.market, bar.symbol)
        # 그 분이 끝난 뒤에만 나온다
        assert bar.start + MIN <= tick.ts
        # 같은 종목 봉은 시간순으로만 나온다(엔진 이력 순서의 전제)
        assert key not in last_start or bar.start > last_start[key]
        last_start[key] = bar.start
        # 내보낸 틱과 그 뒤 틱은 담기지 않는다: 직전 틱까지만 넣고 강제 마감한 봉과 같다
        ref = BarAggregator()
        for prev in ticks[:j]:
            ref.add(prev)
        assert ref.flush(*key) == bar


# ---------------------------------------------------------------- 2. 엔진 전체(리플레이)
class CausalFakeJev:
    """Jev 대역: 받은 상태만으로 답을 정한다(결정적). 상태에 마감 전 봉이 있으면 즉시 실패."""

    def __init__(self) -> None:
        self.calls = 0

    def ask(self, state: dict, questions: dict) -> dict:
        now = datetime.fromisoformat(state["time"])
        local = now.astimezone(KST)
        for b in state["recent_bars"]:
            hh, mm = map(int, b["t"].split(":"))
            start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            assert start + MIN <= now, f"마감 전 봉이 Jev 상태에 들어감: {b['t']} @ {state['time']}"
        self.calls += 1
        ind = state["indicators"]
        return {"model": "fake",
                "answers": {"thesis_aligned": {"noul": 0.9 if ind["ret_5m_pct"] > 0 else 0.3},
                            "momentum": {"score": 3.0 if ind["ret_15m_pct"] > 0 else 1.0},
                            "regime": {"probabilities": {"trend": 0.6, "range": 0.3, "volatile": 0.1}}},
                "usage": {"input_tokens": 100}}


def _theses() -> ThesisStore:
    return ThesisStore([Thesis(market=Market.KR, symbol=s, direction="LONG", rationale="테스트",
                               valid_from=T0 - timedelta(hours=1), valid_until=T0 + timedelta(hours=6, minutes=30))
                        for s in SYMS])


def _bars(symbol: str, closes: list[int], volumes: list[int]) -> list[Bar]:
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        o, cl = Decimal(prev), Decimal(c)
        out.append(Bar(Market.KR, symbol, T0 + i * MIN, o, max(o, cl) + 100, min(o, cl) - 100, cl, v))
        prev = c
    return out


def _run(bars: list[Bar]) -> tuple[AuditLog, CausalFakeJev]:
    fake = CausalFakeJev()
    res = run_replay(bars, JevPolicy(CFG, fake), _theses(), LIMITS, CFG.min_bars)  # type: ignore[arg-type]
    return res.audit, fake


def _until(audit: AuditLog, cutoff: datetime) -> list[dict]:
    return [e for e in audit.events() if datetime.fromisoformat(e["ts"]) <= cutoff]


def _assert_future_does_not_matter(series_a: dict, series_b: dict, k: int) -> list[dict]:
    """봉 0..k 는 같고 k+1.. 만 다른 두 경로 → 봉 k 마감 시각까지의 기록이 같아야 한다."""
    audit_a, fake_a = _run([b for s in SYMS for b in _bars(s, *series_a[s])])
    audit_b, fake_b = _run([b for s in SYMS for b in _bars(s, *series_b[s])])
    cutoff = T0 + (k + 1) * MIN
    before = _until(audit_a, cutoff)
    assert before == _until(audit_b, cutoff)
    assert fake_a.calls > 0 and fake_b.calls > 0
    assert any(e["kind"] == "decision" for e in before)  # 비교가 공허하지 않음
    return before


steps = st.integers(-300, 300)
vols = st.integers(100, 5_000)


@st.composite
def replay_cases(draw):
    n = draw(st.integers(CFG.min_bars + 5, 90))
    k = draw(st.integers(CFG.min_bars + 1, n - 2))  # 봉 0..k 는 공통, k+1.. 은 경로마다 다름
    a, b = {}, {}
    for s in SYMS:
        start = draw(st.integers(20_000, 200_000))
        st_a = draw(st.lists(steps, min_size=n, max_size=n))
        st_b = st_a[:k + 1] + draw(st.lists(steps, min_size=n - k - 1, max_size=n - k - 1))
        v_a = draw(st.lists(vols, min_size=n, max_size=n))
        v_b = v_a[:k + 1] + draw(st.lists(vols, min_size=n - k - 1, max_size=n - k - 1))
        a[s] = ([max(1_000, start + x * 10) for x in accumulate(st_a)], v_a)
        b[s] = ([max(1_000, start + x * 10) for x in accumulate(st_b)], v_b)
    return a, b, k


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(replay_cases())
def test_replay_decisions_ignore_future_bars(case):
    a, b, k = case
    _assert_future_does_not_matter(a, b, k)


def test_future_invariance_covers_orders_and_exits():
    """무작위 경로가 주문까지 안 갈 수 있으므로, 주문·청산이 확실히 나는 경로로 한 번 더 확인한다."""
    n, k = 70, 45
    up = [50_000 + 300 * i for i in range(n)]            # 꾸준한 상승 → 진입
    crash = up[:k + 1] + [up[k] - 2_000 * (i + 1) for i in range(n - k - 1)]  # k 이후만 급락
    vol = [1_000] * n
    before = _assert_future_does_not_matter({s: (up, vol) for s in SYMS}, {s: (crash, vol) for s in SYMS}, k)
    kinds = {e["kind"] for e in before}
    assert {"order", "fill"} <= kinds


# ---------------------------------------------------------------- 3. 논지
windows = st.lists(st.tuples(st.integers(-600, 600), st.integers(1, 600)), max_size=6)


@settings(max_examples=300, deadline=None)
@given(windows, windows, st.integers(-700, 700))
def test_thesis_starting_later_never_affects_now(present, future, at):
    now = T0 + at * MIN
    mk = lambda i, a, d, tag: Thesis(market=Market.KR, symbol="005930", direction="LONG", rationale=f"{tag}{i}",
                                     valid_from=T0 + a * MIN, valid_until=T0 + (a + d) * MIN)
    base = [mk(i, a, d, "p") for i, (a, d) in enumerate(present)]
    # now 이후에 시작하는 논지만 추가한다
    later = [mk(i, at + 1 + abs(a), d, "f") for i, (a, d) in enumerate(future)]
    got = ThesisStore(base).active(Market.KR, "005930", now)
    assert ThesisStore(base + later).active(Market.KR, "005930", now) == got
    if got is not None:
        assert got.valid_from <= now < got.valid_until


# ---------------------------------------------------------------- 4. Jev 보정 라벨
DAYS = (datetime(2026, 10, 28, 9, 0, tzinfo=KST), datetime(2026, 10, 29, 9, 0, tzinfo=KST))


def _det(p: float, entry: int) -> dict:
    return {"answers": {"thesis_aligned": {"noul": p}}, "state": {"indicators": {"last": entry}}}


@st.composite
def calib_cases(draw):
    h = draw(st.integers(1, 30))
    bars = []
    for d in DAYS:  # 09:00~15:30, 거래 없는 분은 봉 없음
        for m in draw(st.lists(st.integers(0, 390), unique=True, max_size=60)):
            bars.append((d + m * MIN, draw(st.integers(90, 110))))
    decisions = [(DAYS[draw(st.integers(0, 1))] + draw(st.integers(1, 391)) * MIN,
                  draw(st.floats(0, 1)), draw(st.integers(90, 110)), draw(st.sampled_from(["decision", "shadow"])))
                 for _ in range(draw(st.integers(0, 20)))]
    return h, bars, decisions


@settings(max_examples=300, deadline=None)
@given(calib_cases())
def test_calibration_label_comes_from_later_bar_in_same_session(case):
    h, bars, decisions = case
    a = AuditLog()
    for t, c in bars:
        a.record("bar", {"start": t, "c": c}, market="KR", symbol="X", ts=t + MIN)
    for ts, p, entry, kind in decisions:
        a.record(kind, {"has_thesis": True, "detail": _det(p, entry)}, market="KR", symbol="X", ts=ts)

    # 기준 구현: 결정 이후 H분 시점 이후의 첫 봉, 단 같은 날(같은 장)일 때만
    ordered = sorted(bars)
    pairs = []
    for ts, p, entry, _ in decisions:
        target = ts + (h - 1) * MIN
        nxt = next(((t, c) for t, c in ordered if t >= target), None)
        if nxt is None or nxt[0].date() != ts.date():
            continue
        assert nxt[0] >= ts  # 결과 봉은 결정 시각 이후에 시작한다(결정 때 이미 알던 가격이 아님)
        pairs.append((p, 1 if nxt[1] > entry else 0))

    c = jev_calibration(a, horizon_min=h)
    assert c.n == len(pairs)
    if pairs:
        assert c.brier == pytest.approx(sum((p - y) ** 2 for p, y in pairs) / len(pairs))


def test_calibration_skips_label_across_market_close():
    """회귀: 15:20 결정의 15분 결과를 다음 날 09:00 가격으로 매기던 버그."""
    a = AuditLog()
    for i in range(20):  # 15:00~15:19
        t = DAYS[0] + timedelta(hours=6, minutes=i)
        a.record("bar", {"start": t, "c": 100}, market="KR", symbol="X", ts=t + MIN)
    a.record("bar", {"start": DAYS[1], "c": 130}, market="KR", symbol="X", ts=DAYS[1] + MIN)
    a.record("shadow", {"has_thesis": True, "detail": _det(0.9, 100)}, market="KR", symbol="X",
             ts=DAYS[0] + timedelta(hours=6, minutes=20))
    assert jev_calibration(a).n == 0
