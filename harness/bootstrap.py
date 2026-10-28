"""설정 파일 → 운영 구성요소 조립 (run_mock / run_live 공용)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from harness.audit import AuditLog
from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker
from harness.decision import BaselinePolicy, JevClient, JevPolicy, StrategyConfig
from harness.engine import TradingEngine, restore_ledger, unreconciled_symbols
from harness.market.nh_feed import FeedFields
from harness.models import Market, OrderType
from harness.notify import make_notifier
from harness.research import ThesisStore
from harness.research.repo import ThesisRepo
from harness.research.service import ResearchConfig
from harness.risk import Ledger, RiskGate, RiskLimits
from harness.runner import LoopConfig

ROOT = Path(__file__).resolve().parent.parent


class StartupBlocked(RuntimeError):
    pass


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8")) or {}


def feed_fields() -> FeedFields | None:
    f = load_yaml("feed.yaml")
    return FeedFields(f["price_field"], f["volume_field"]) if f.get("price_field") and f.get("volume_field") else None


def broker_fields() -> tuple[str | None, BalanceFields | None]:
    f = load_yaml("broker_fields.yaml")
    keys = ("holdings_list", "symbol_field", "quantity_field", "avg_price_field")
    bf = BalanceFields(**{k: f[k] for k in keys}) if all(f.get(k) for k in keys) else None
    return f.get("order_no_field"), bf


def fields_ready() -> bool:
    order_no, bf = broker_fields()
    return bool(order_no and bf and feed_fields())


@dataclass
class Assembly:
    engine: TradingEngine
    broker: NHPlugBroker
    loop: LoopConfig


def assemble(*, mode: str, theses_path: str, policy_name: str, risk_file: str, send: bool,
             audit_name: str, entry_order_type: OrderType, allow_live: bool = False) -> Assembly:
    fields = feed_fields()
    if fields is None:
        raise StartupBlocked("config/feed.yaml 의 price_field / volume_field 를 먼저 채우세요 (scripts/capture_feed.py)")
    order_no, bfields = broker_fields()

    limits = RiskLimits.from_yaml(ROOT / "config" / risk_file)
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    repo = ThesisRepo(theses_path) if theses_path.endswith(".sqlite") else None
    theses = repo.store() if repo else ThesisStore.from_yaml(theses_path)
    audit = AuditLog(ROOT / "data" / audit_name)
    notify = make_notifier(f"{mode}/{policy_name}")

    left = unreconciled_symbols(audit)
    if left:
        raise StartupBlocked(f"이전 실행의 미청산/결과불명: {left}. 브로커 화면 확인 후 "
                             "scripts/resolve_pending.py 로 기록하고 다시 시작하세요.")

    broker = NHPlugBroker(os.environ.get("NHPLUG_DEFAULT_ACCOUNT", ""), dry_run=not send,
                          allow_live=allow_live, order_no_field=order_no, guard=audit.guard(),
                          balance_fields=bfields)
    policy = (JevPolicy(cfg, JevClient(cfg.jev.model, timeout_sec=cfg.jev.timeout_sec))
              if policy_name == "jev" else BaselinePolicy(cfg))
    gate = RiskGate(limits, Ledger())
    restore_ledger(audit, gate)
    engine = TradingEngine(gate=gate, broker=broker, policy=policy, theses=theses,
                           min_bars=cfg.min_bars, audit=audit, entry_order_type=entry_order_type,
                           notify=notify)

    if repo:
        rcfg = ResearchConfig.from_yaml(ROOT / "config" / "research.yaml")
        symbols = [u.symbol for u in rcfg.universe if u.market is Market.KR]
    else:
        symbols = [s for m, s in theses.watchlist(datetime.now(timezone.utc)) if m is Market.KR]
    if not symbols:
        raise StartupBlocked("매매 대상 국내 종목이 없습니다")
    loop = LoopConfig(symbols, fields, ROOT / "data" / "KILL", repo=repo)
    return Assembly(engine, broker, loop)
