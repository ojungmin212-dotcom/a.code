from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from harness.models import AccountState, EntrySignal, Market
from harness.risk import Ledger, RiskGate, RiskLimits

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits.from_yaml(ROOT / "config" / "risk.yaml")


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()


@pytest.fixture
def gate(limits, ledger) -> RiskGate:
    return RiskGate(limits, ledger)


@pytest.fixture
def kr_open() -> datetime:
    return datetime(2026, 10, 28, 10, 30, tzinfo=KST)  # 수요일 장중


@pytest.fixture
def cash() -> AccountState:
    return AccountState(cash_krw=Decimal(500000))


def kr_signal(now: datetime, **kw) -> EntrySignal:
    base = dict(market=Market.KR, symbol="005930", entry_price=Decimal(50000),
                stop_price=Decimal(49000), take_profit_price=Decimal(52000),
                confidence=0.8, created_at=now)
    base.update(kw)
    return EntrySignal(**base)
