from datetime import datetime
from decimal import Decimal

import pytest
import requests

from harness.decision import BaselinePolicy, DecisionContext, JevClient, JevError, JevPolicy, StrategyConfig
from harness.decision.policy import QUESTIONS, build_signal
from harness.market import compute_indicators
from harness.models import Market

from .conftest import KST, ROOT
from .helpers import make_bars, store

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=KST)


@pytest.fixture
def cfg():
    return StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")


def ctx(closes, **kw):
    bars = make_bars(closes, **kw)
    return DecisionContext(Market.KR, "005930", NOW, store().active(Market.KR, "005930", NOW),
                           bars, compute_indicators(bars))


def answers(p_align=0.8, momentum=3.2, p_volatile=0.1):
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 500, "output_tokens": 0}, "answers": {
        "thesis_aligned": {"type": "noul", "noul": p_align},
        "momentum": {"type": "score", "score": momentum, "confidence": 0.8,
                     "probabilities": {}, "legend": {}},
        "regime": {"type": "choice", "choice": "trend", "confidence": 0.7, "probabilities":
                   {"trend": 1 - p_volatile, "range": 0.0, "volatile": p_volatile}},
    }}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body, str(body)

    @property
    def ok(self):
        return self.status_code == 200

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def client(resp=None, exc=None):
    sent = []

    def post(url, **kw):
        sent.append((url, kw))
        if exc:
            raise exc
        return resp

    return JevClient("jev-1.13.0", api_key="k", post=post), sent


def test_jev_request_shape():
    c, sent = client(FakeResp(body=answers()))
    c.ask({"a": 1}, QUESTIONS)
    url, kw = sent[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert kw["json"]["model"] == "jev-1.13.0" and set(kw["json"]["questions"]) == set(QUESTIONS)
    assert kw["headers"]["Authorization"] == "Bearer k" and kw["timeout"] == 3.0


@pytest.mark.parametrize("resp,exc", [
    (FakeResp(500, {"e": 1}), None),
    (FakeResp(200, None), None),
    (FakeResp(200, {"answers": {}}), None),
    (FakeResp(200, {"answers": {**answers()["answers"],
                                "thesis_aligned": {"type": "noul", "noul": 1.5}}}), None),
    (None, requests.Timeout("t")),
])
def test_jev_failures_raise(resp, exc):
    c, _ = client(resp, exc)
    with pytest.raises(JevError):
        c.ask({}, QUESTIONS)


def test_jev_requires_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError):
        JevClient("m").ask({}, QUESTIONS)


RISING = [50000 + 100 * i for i in range(25)]


def test_jev_policy_enters(cfg):
    c, _ = client(FakeResp(body=answers()))
    d = JevPolicy(cfg, c).decide(ctx(RISING))
    assert d.signal is not None
    assert d.signal.confidence == pytest.approx(0.8 * 0.9)
    assert d.detail["model"] == "jev-1.13.0"


@pytest.mark.parametrize("kw,reason", [
    ({"p_align": 0.5}, "논지 정합"), ({"momentum": 2.0}, "모멘텀"), ({"p_volatile": 0.4}, "급변"),
])
def test_jev_policy_thresholds(cfg, kw, reason):
    c, _ = client(FakeResp(body=answers(**kw)))
    d = JevPolicy(cfg, c).decide(ctx(RISING))
    assert d.signal is None and reason in d.reason


def test_jev_failure_holds(cfg):
    c, _ = client(exc=requests.ConnectionError("x"))
    d = JevPolicy(cfg, c).decide(ctx(RISING))
    assert d.signal is None and "HOLD" in d.reason


def test_signal_prices_on_krx_ticks(cfg):
    # 종가 52,400 (호가단위 100), ATR 200 → 손절폭 400 → 손절 52,000, 익절 52,400+1,000
    sig = build_signal(ctx(RISING), cfg, 0.7)
    assert sig.entry_price == Decimal(52400)
    assert sig.stop_price == Decimal(52000) and sig.take_profit_price == Decimal(53400)
    assert sig.stop_price % 100 == 0 and sig.take_profit_price % 100 == 0


def test_baseline(cfg):
    assert BaselinePolicy(cfg).decide(ctx(RISING, last_volume=2000)).signal is not None
    assert BaselinePolicy(cfg).decide(ctx(RISING)).signal is None           # 거래량 증가 없음
    assert BaselinePolicy(cfg).decide(ctx(RISING[::-1], last_volume=2000)).signal is None  # 하락
