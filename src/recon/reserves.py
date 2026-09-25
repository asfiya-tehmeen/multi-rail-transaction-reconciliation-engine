"""Rolling reserves coverage: claimed ledger balance vs. verified on-chain balance.

For each reserve asset and each UTC day:
    claimed  = running sum of ledger entries booked on the on-chain rail
    verified = running sum of transfers actually observed on-chain for the wallet
    coverage = verified / claimed
The rolling figures are the minimum and mean of daily coverage over the trailing window. The minimum is
the conservative number a controller should quote.

Each snapshot records the events that moved it that day; its full lineage is the union over all days up
to and including it, which `recon trace` reconstructs and re-adds to prove the number.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable

from .models import LEDGER, ONCHAIN, Event
from .utils import canonical_json, dec_str, short_id

RATIO_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class CoverageSnapshot:
    asset: str
    as_of_date: date
    claimed_balance: Decimal
    verified_balance: Decimal
    coverage: Decimal | None
    rolling_min: Decimal | None
    rolling_avg: Decimal | None
    window_days: int
    claimed_event_ids: tuple[str, ...]  # events booked on this day only
    verified_event_ids: tuple[str, ...]

    @property
    def snapshot_id(self) -> str:
        return short_id("coverage", self.asset, self.as_of_date.isoformat())

    def digest_line(self) -> str:
        opt = lambda d: None if d is None else dec_str(d)  # noqa: E731
        return canonical_json({
            "snapshot_id": self.snapshot_id, "claimed": dec_str(self.claimed_balance),
            "verified": dec_str(self.verified_balance), "coverage": opt(self.coverage),
            "rolling_min": opt(self.rolling_min), "rolling_avg": opt(self.rolling_avg),
        })


def ratio(verified: Decimal, claimed: Decimal) -> Decimal | None:
    return (verified / claimed).quantize(RATIO_QUANTUM) if claimed > 0 else None


def compute_coverage(events: Iterable[Event], assets: tuple[str, ...], window_days: int,
                     as_of: datetime) -> list[CoverageSnapshot]:
    events = list(events)
    claimed_events = [e for e in events if e.source == LEDGER and e.rail == ONCHAIN]
    verified_events = [e for e in events if e.source == ONCHAIN]
    assets = assets or tuple(sorted({e.asset for e in claimed_events + verified_events}))

    snapshots = []
    for asset in assets:
        claimed_by_day = defaultdict(list)
        verified_by_day = defaultdict(list)
        for e in claimed_events:
            if e.asset == asset:
                claimed_by_day[e.occurred_at.date()].append(e)
        for e in verified_events:
            if e.asset == asset:
                verified_by_day[e.occurred_at.date()].append(e)
        if not claimed_by_day and not verified_by_day:
            continue

        day = min([*claimed_by_day, *verified_by_day])
        claimed = verified = Decimal(0)
        window: deque = deque(maxlen=window_days)
        while day <= as_of.date():
            day_claimed, day_verified = claimed_by_day[day], verified_by_day[day]
            claimed += sum((e.amount for e in day_claimed), Decimal(0))
            verified += sum((e.amount for e in day_verified), Decimal(0))
            cov = ratio(verified, claimed)
            window.append(cov)
            values = [v for v in window if v is not None]
            snapshots.append(
                CoverageSnapshot(
                    asset=asset, as_of_date=day, claimed_balance=claimed, verified_balance=verified,
                    coverage=cov,
                    rolling_min=min(values) if values else None,
                    rolling_avg=(sum(values) / len(values)).quantize(RATIO_QUANTUM) if values else None,
                    window_days=window_days,
                    claimed_event_ids=tuple(sorted(e.event_id for e in day_claimed)),
                    verified_event_ids=tuple(sorted(e.event_id for e in day_verified)),
                )
            )
            day += timedelta(days=1)
    return snapshots
