"""설정 파일 → 운영 구성요소 조립 (run_mock / run_live 공용)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from harness.audit import AuditLog
from harness.broker.nhplug_broker import BalanceFields, NHPlugBroker, UsConfig
from harness.broker.paper import PaperBroker
from harness.decision import BaselinePolicy, JevClient, JevPolicy, StrategyConfig
from harness.decision.shadow import ShadowObserver
from harness.engine import TradingEngine, restore_ledger, unreconciled_symbols
from harness.market.fx import FxRate, FxStale
from harness.market.instruments import gic_map
from harness.market.nh_feed import FeedFields
from harness.models import Market, OrderType
from harness.notify import make_notifier
from harness.research import ThesisStore
from harness.research.repo import ThesisRepo
from harness.research.service import ResearchConfig
from harness.risk import Ledger, RiskGate, RiskLimits
from harness.runner import Lane, LoopConfig, MarketSpec

ROOT = Path(__file__).resolve().parent.parent
_BAL_KEYS = ("holdings_list", "symbol_field", "quantity_field", "avg_price_field")


class StartupBlocked(RuntimeError):
    pass


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8")) or {}


def feed_fields(market: Market = Market.KR) -> FeedFields | None:
    f = load_yaml("feed.yaml")
    pre = "" if market is Market.KR else "us_"
    price, vol = f.get(f"{pre}price_field"), f.get(f"{pre}volume_field")
    return FeedFields(price, vol) if price and vol else None


def _balance(d: dict) -> BalanceFields | None:
    return BalanceFields(**{k: d[k] for k in _BAL_KEYS}) if all(d.get(k) for k in _BAL_KEYS) else None


def broker_fields() -> tuple[list[str], BalanceFields | None]:
    f = load_yaml("broker_fields.yaml")
    return list(f.get("order_no_fields") or []), _balance(f)


def us_config() -> UsConfig | None:
    us = load_yaml("broker_fields.yaml").get("us") or {}
    return UsConfig(us["sell_path"], _balance(us)) if us.get("sell_path") else None


def fields_ready(markets: tuple[Market, ...] = (Market.KR,)) -> bool:
    """승격·실거래용: 필드가 채워져 있고 모의투자 실측 확인(verified_by_mock)까지 끝났는가."""
    verified = load_yaml("broker_fields.yaml").get("verified_by_mock") and load_yaml("feed.yaml").get("verified_by_mock")
    order_no, bf = broker_fields()
    ok = bool(verified and order_no and bf and feed_fields(Market.KR))
    if Market.US in markets:
        us = us_config()
        ok = ok and bool(us and us.balance_fields and feed_fields(Market.US))
    return ok


def fx_source(now: datetime) -> dict[Market, Decimal]:
    try:
        return {Market.US: FxRate.from_dict(load_yaml("fx.yaml")).check(now)}
    except FxStale:
        return {}


@dataclass
class Assembly:
    lanes: list[Lane]
    loop: LoopConfig

    @property
    def broker_lane(self) -> Lane | None:
        return next((ln for ln in self.lanes if not ln.is_paper), None)


def p4_enabled() -> bool:
    """D1: 미국(P4)은 로드맵에서 보류. config/promotion.yaml p4_enabled 로만 해제."""
    return bool(load_yaml("promotion.yaml").get("p4_enabled"))


def assemble(*, mode: str, theses_path: str, policies: list[str], broker_policy: str,
             risk_file: str, send: bool, entry_order_type: OrderType | None = None,
             markets: tuple[Market, ...] = (Market.KR,), allow_live: bool = False,
             audit_names: dict[str, str] | None = None) -> Assembly:
    if broker_policy not in policies:
        raise StartupBlocked(f"브로커 레인 정책({broker_policy})이 실행 정책 목록에 없습니다")
    if Market.US in markets and not p4_enabled():
        raise StartupBlocked("미국(P4)은 보류 중입니다(D1). 재개하려면 config/promotion.yaml 의 p4_enabled: true")
    order_no, bfields = broker_fields()
    us = us_config() if Market.US in markets else None
    if Market.US in markets:
        if us is None:
            raise StartupBlocked("미국 매도 경로(us.sell_path) 미설정 — 미국 운영 불가")
        try:
            FxRate.from_dict(load_yaml("fx.yaml")).check(datetime.now(timezone.utc))
        except FxStale as e:
            raise StartupBlocked(f"환율: {e}") from e

    limits = RiskLimits.from_yaml(ROOT / "config" / risk_file)
    cfg = StrategyConfig.from_yaml(ROOT / "config" / "strategy.yaml")
    if entry_order_type is None:
        entry_order_type = OrderType.LIMIT if cfg.execution.entry_order == "limit" else OrderType.MARKET
    repo = ThesisRepo(theses_path) if theses_path.endswith(".sqlite") else None
    theses = repo.store() if repo else ThesisStore.from_yaml(theses_path)
    audit_names = audit_names or {}

    specs = []
    for m in markets:
        fields = feed_fields(m)
        if fields is None:
            raise StartupBlocked(f"config/feed.yaml 의 {m} 시세 필드를 먼저 채우세요 (scripts/capture_feed.py)")
        if repo:
            tradable = [u.symbol for u in ResearchConfig.from_yaml(ROOT / "config" / "research.yaml").universe
                        if u.market is m]
        else:
            tradable = [s for mk, s in theses.watchlist(datetime.now(timezone.utc)) if mk is m]
        if not tradable:
            raise StartupBlocked(f"{m} 매매 대상 종목이 없습니다")
        extra = []
        if cfg.shadow and cfg.shadow.enabled and "jev" in policies:
            extra = [x for x in cfg.shadow.extra_symbols.get(m.value, []) if x not in tradable]
        symbols = tradable + extra
        keys = gic_map(symbols) if m is Market.US else {}
        specs.append(MarketSpec(m, symbols, fields, "RC" if m is Market.US else "mc", keys, tradable))

    lanes = []
    for name in policies:
        audit = AuditLog(ROOT / "data" / audit_names.get(name, f"audit-{name}.sqlite"))
        left = unreconciled_symbols(audit)
        if left:
            raise StartupBlocked(f"[{name}] 이전 실행의 미청산/결과불명: {left}. 브로커 화면 확인 후 "
                                 "scripts/resolve_pending.py 로 기록하고 다시 시작하세요.")
        if name == broker_policy:
            broker = NHPlugBroker(os.environ.get("NHPLUG_DEFAULT_ACCOUNT", ""), dry_run=not send,
                                  allow_live=allow_live, order_no_field=order_no, guard=audit.guard(),
                                  balance_fields=bfields, us=us)
        else:
            broker = PaperBroker(limits.initial_capital_krw, guard=audit.guard(),
                                 slippage_bps=Decimal(str(cfg.paper_slippage_bps)))
        policy = (JevPolicy(cfg, JevClient(cfg.jev.model, timeout_sec=cfg.jev.timeout_sec))
                  if name == "jev" else BaselinePolicy(cfg))
        shadow = (ShadowObserver(policy, audit, cfg.shadow)
                  if name == "jev" and cfg.shadow and cfg.shadow.enabled else None)
        gate = RiskGate(limits, Ledger())
        restore_ledger(audit, gate)
        tag = f"{mode}/{name}" + ("(paper)" if isinstance(broker, PaperBroker) else "")
        engine = TradingEngine(gate=gate, broker=broker, policy=policy, theses=theses,
                               min_bars=cfg.min_bars, audit=audit, entry_order_type=entry_order_type,
                               notify=make_notifier(tag), fx_rates={Market.KR: Decimal(1)},
                               shadow=shadow)
        lanes.append(Lane(engine, broker))

    loop = LoopConfig(specs, ROOT / "data" / "KILL", repo=repo,
                      fx_source=fx_source if Market.US in markets else None,
                      pending_timeout_sec=cfg.execution.pending_timeout_sec,
                      fill_notices=cfg.execution.fill_notices,
                      tick_dir=ROOT / "data" / "ticks" if cfg.operations.record_ticks else None,
                      heartbeat_min=cfg.operations.heartbeat_min)
    return Assembly(lanes, loop)
