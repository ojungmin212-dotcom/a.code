"""미국 티커 → GIC 15자리 (해외 실시간 시세 tr_key). NH 종목마스터 m_gtsstock 사용."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def _rows(data: Any) -> list[dict]:
    return data.to_dict("records") if hasattr(data, "to_dict") else list(data)


def gic_map(tickers: Iterable[str], loader: Callable[[str], Any] | None = None) -> dict[str, str]:
    """{티커: GIC}. 미국(USA) 종목만 보고, 못 찾거나 중복이면 예외(잘못된 종목 구독 방지)."""
    if loader is None:
        from nhplug.instruments import load_master
        loader = load_master
    want = {t.upper() for t in tickers}
    found: dict[str, list[str]] = {}
    for r in _rows(loader("m_gtsstock")):
        sym = str(r.get("symbol", "")).strip().upper()
        if sym in want and str(r.get("country_code", "")).strip() == "USA":
            found.setdefault(sym, []).append(str(r.get("gic", "")).strip())
    missing = sorted(want - set(found))
    dup = sorted(s for s, g in found.items() if len(set(g)) > 1)
    if missing or dup:
        raise ValueError(f"종목마스터 매핑 실패 — 없음: {missing}, 중복: {dup}")
    return {s: g[0] for s, g in found.items()}
