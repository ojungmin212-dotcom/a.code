import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import requests

from harness.audit import AuditLog
from harness.evaluation import jev_calibration, trade_stats
from harness.models import Market
from harness.research.aggregate import AggregateSettings, aggregate
from harness.research.bots import BearOpinion, BotOpinion
from harness.research.budget import BudgetExceeded, BudgetGuard
from harness.research.grok import GrokClient, GrokError
from harness.research.repo import ThesisRepo
from harness.research.service import ResearchConfig, ResearchService

from .conftest import KST, ROOT

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=KST)


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self._b, self.text = status, body, json.dumps(body)

    @property
    def ok(self):
        return self.status_code == 200

    def json(self):
        return self._b


def nested(obj, usage=None, url="https://example.com/a"):
    return {"output": [
        {"type": "web_search_call"},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj),
                                         "annotations": [{"type": "url_citation", "url": url}]}]},
    ], "usage": usage or {"input_tokens": 1000, "output_tokens": 200}}


def fake_client(responses):
    sent = []
    it = iter(responses)

    def post(url, **kw):
        sent.append(kw["json"])
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return GrokClient("grok-test", api_key="k", post=post), sent


LONG = {"stance": "LONG", "confidence": 0.8, "reasons": ["호재"], "risks": ["변동성"]}
BEAR_LOW = {"invalidation_prob": 0.2, "counterarguments": ["이미 반영"]}


# ---------------------------------------------------------------- Grok 클라이언트
def test_grok_request_and_nested_parse():
    c, sent = fake_client([Resp(nested(LONG))])
    r = c.ask("sys", "user", {"type": "object"}, "opinion", tools=["web_search", "x_search"])
    assert r.data == LONG and r.citations == ["https://example.com/a"]
    body = sent[0]
    assert body["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"]


def test_grok_top_level_output_text():
    c, _ = fake_client([Resp({"output_text": json.dumps(LONG), "citations": ["u1"]})])
    r = c.ask("s", "u", {}, "x")
    assert r.data == LONG and r.citations == ["u1"]


@pytest.mark.parametrize("resp", [
    Resp({"e": 1}, status=429), Resp({"output": []}), Resp({"output_text": "not json"}),
    Resp({"output_text": "[1,2]"}), requests.Timeout("t"),
])
def test_grok_errors(resp):
    c, _ = fake_client([resp])
    with pytest.raises(GrokError):
        c.ask("s", "u", {}, "x")


# ---------------------------------------------------------------- 예산
def test_budget_reserve_charge_and_month_rollover():
    a = AuditLog()
    b = BudgetGuard(a, monthly_budget_krw=1000, usd_krw=1400)
    price = {"input": 3.0, "output": 15.0}
    krw = b.charge("sentiment", 0.2, {"input_tokens": 1000, "output_tokens": 0}, price, NOW)
    assert krw == pytest.approx(280)  # 추정 0.2$ > 토큰 0.003$
    krw = b.charge("x", 0.0, {"input_tokens": 200_000, "output_tokens": 0}, price, NOW)
    assert krw == pytest.approx(840)  # 실측이 크면 실측
    with pytest.raises(BudgetExceeded):
        b.reserve(0.01, NOW)
    b.reserve(0.5, datetime(2026, 11, 1, 8, 0, tzinfo=KST))  # 다음 달은 새 예산


# ---------------------------------------------------------------- 결합
S = AggregateSettings(weights={"sentiment": 0.4, "fundamental": 0.6}, min_score=0.45,
                      avoid_veto_conf=0.6, bear_veto_prob=0.5)


def ops(s="LONG", sc=0.8, f="LONG", fc=0.8):
    return {"sentiment": BotOpinion(stance=s, confidence=sc, reasons=["a"], risks=[]),
            "fundamental": BotOpinion(stance=f, confidence=fc, reasons=["b"], risks=[])}


def test_aggregate_creates_thesis_clipped_to_close():
    now = datetime(2026, 10, 28, 14, 0, tzinfo=KST)
    t, reason, score = aggregate(Market.KR, "005930", ops(), BearOpinion(**BEAR_LOW), S, now, 180)
    assert t is not None and score == pytest.approx(0.8 * 0.8)
    assert t.valid_until == datetime(2026, 10, 28, 15, 30, tzinfo=KST)
    assert "이미 반영" in t.rationale


@pytest.mark.parametrize("o,bear,reason", [
    (ops(s="AVOID", sc=0.7), 0.1, "거부권"),
    (ops(), 0.6, "무효화"),
    (ops(s="NEUTRAL", f="LONG", fc=0.6), 0.1, "미달"),  # 0.36*0.9
])
def test_aggregate_rejections(o, bear, reason):
    t, r, _ = aggregate(Market.KR, "005930", o, BearOpinion(invalidation_prob=bear,
                                                            counterarguments=[]), S, NOW, 180)
    assert t is None and reason in r


def test_bot_opinion_trims_long_lists():
    o = BotOpinion(stance="LONG", confidence=0.5, reasons=["x" * 500] * 9, risks=[])
    assert len(o.reasons) == 5 and len(o.reasons[0]) == 200


# ---------------------------------------------------------------- 서비스
def cfg():
    return ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")


def test_service_end_to_end(tmp_path):
    c = cfg().model_copy(update={"universe": cfg().universe[:1]})
    client, sent = fake_client([Resp(nested(LONG)), Resp(nested(LONG)), Resp(nested(BEAR_LOW))])
    repo, audit = ThesisRepo(tmp_path / "t.sqlite"), AuditLog()
    out = ResearchService(c, client, repo, audit).run(NOW)
    assert out == {"005930": "논지 생성"}
    assert "x_search" in json.dumps(sent[0]["tools"]) and "tools" not in sent[2]
    store = ThesisRepo(tmp_path / "t.sqlite").store()  # 다른 프로세스가 읽는 경로
    assert store.active(Market.KR, "005930", NOW + timedelta(minutes=90)) is not None
    assert len(audit.events("research_cost")) == 3


def test_service_failure_expires_old_thesis(tmp_path):
    c = cfg().model_copy(update={"universe": cfg().universe[:1]})
    repo, audit = ThesisRepo(tmp_path / "t.sqlite"), AuditLog()
    client, _ = fake_client([Resp(nested(LONG)), Resp(nested(LONG)), Resp(nested(BEAR_LOW))])
    ResearchService(c, client, repo, audit).run(NOW)
    later = NOW + timedelta(minutes=30)
    client2, _ = fake_client([requests.ConnectionError("x")])
    out = ResearchService(c, client2, repo, audit).run(later)
    assert "실패" in out["005930"]
    assert repo.store().active(Market.KR, "005930", later) is None


def test_service_stops_at_budget(tmp_path):
    c = cfg().model_copy(update={"universe": cfg().universe[:1], "monthly_budget_krw": 100})
    client, sent = fake_client([])
    out = ResearchService(c, client, ThesisRepo(":memory:"), AuditLog()).run(NOW)
    assert "예산" in out["005930"] and sent == []


# ---------------------------------------------------------------- 평가
def test_trade_stats_drawdown():
    a = AuditLog()
    for p in (1000, -3000, 500, -1000, 4000):
        a.record("exit", {"pnl_krw": Decimal(p)}, market="KR", symbol="X")
    s = trade_stats(a)
    assert s.trades == 5 and s.total_krw == 1500 and s.win_rate == 0.6
    assert s.max_drawdown_krw == 3500 and s.profit_factor == pytest.approx(5500 / 4000)


def test_jev_calibration():
    a = AuditLog()
    t0 = datetime(2026, 10, 28, 10, 0, tzinfo=KST)
    for i in range(60):
        a.record("bar", {"start": t0 + timedelta(minutes=i), "c": 100 + i}, market="KR", symbol="X")
    for i, p in enumerate((0.9, 0.9, 0.1)):
        a.record("decision", {"detail": {"answers": {"thesis_aligned": {"noul": p}},
                                         "state": {"indicators": {"last": 100 + i}}}},
                 market="KR", symbol="X", ts=t0 + timedelta(minutes=i + 1))
    # 결과 없음(데이터 끝 이후) 결정은 제외
    a.record("decision", {"detail": {"answers": {"thesis_aligned": {"noul": 0.5}},
                                     "state": {"indicators": {"last": 1}}}},
             market="KR", symbol="X", ts=t0 + timedelta(minutes=59))
    c = jev_calibration(a)
    assert c.n == 3  # 셋 다 상승(y=1)
    assert c.brier == pytest.approx((0.01 + 0.01 + 0.81) / 3)
    assert c.base_brier == 0 and c.skill is None
