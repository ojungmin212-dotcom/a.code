"""USD/KRW 환율 — 수동 설정 + 신선도 검사. 낡은 환율로는 미국 매매를 하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class FxStale(RuntimeError):
    pass


@dataclass(frozen=True)
class FxRate:
    usd_krw: Decimal | None
    as_of: date | None
    max_age_days: int

    @classmethod
    def from_dict(cls, d: dict) -> FxRate:
        rate = Decimal(str(d["usd_krw"])) if d.get("usd_krw") else None
        as_of = d.get("as_of")
        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        return cls(rate, as_of, int(d.get("max_age_days", 3)))

    def check(self, now: datetime) -> Decimal:
        if self.usd_krw is None or self.as_of is None:
            raise FxStale("config/fx.yaml 환율 미설정")
        if not Decimal(800) < self.usd_krw < Decimal(2500):
            raise FxStale(f"환율 값 이상: {self.usd_krw}")
        age = (now.astimezone(KST).date() - self.as_of).days
        if age > self.max_age_days or age < 0:
            raise FxStale(f"환율이 {age}일 지남(기준 {self.max_age_days}일)")
        return self.usd_krw
