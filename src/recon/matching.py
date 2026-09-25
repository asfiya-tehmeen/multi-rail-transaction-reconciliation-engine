"""Key-based matching of ledger entries against external rails, with break classification.

`reconcile` is a pure function: same events + same tolerances + same as_of -> identical results,
regardless of input order. Everything that makes runs repeatable depends on that property.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .config import RailTolerance
from .models import (
    AMOUNT_MISMATCH, BREAK, IN_TRANSIT, LEDGER, MATCHED, ORPHAN_EXTERNAL, TIMING_LAG, UNMATCHED_LEDGER, Event,
)
from .utils import canonical_json, dec_str, short_id


@dataclass(frozen=True)
class MatchResult:
    rail: str
    txn_key: str
    status: str
    break_type: str | None
    asset: str
    ledger_amount: Decimal | None
    external_amount: Decimal | None
    amount_delta: Decimal | None  # ledger - external
    lag_seconds: int | None  # first ledger posting - first external event; positive = ledger booked later
    ledger_event_ids: tuple[str, ...]
    external_event_ids: tuple[str, ...]
    detail: str

    @property
    def result_id(self) -> str:
        # Identifies the group of events, not the verdict, so the same break keeps its id across runs.
        return short_id("result", self.rail, self.txn_key, *self.ledger_event_ids, "|", *self.external_event_ids)

    def canonical(self) -> dict:
        opt = lambda d: None if d is None else dec_str(d)  # noqa: E731
        return {
            "result_id": self.result_id, "rail": self.rail, "txn_key": self.txn_key, "status": self.status,
            "break_type": self.break_type, "asset": self.asset, "ledger_amount": opt(self.ledger_amount),
            "external_amount": opt(self.external_amount), "amount_delta": opt(self.amount_delta),
            "lag_seconds": self.lag_seconds,
        }

    def digest_line(self) -> str:
        return canonical_json(self.canonical())


def _fmt_age(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.1f}h"


def _classify(rail: str, key: str, ledger: list[Event], external: list[Event], tol: RailTolerance,
              as_of: datetime) -> MatchResult:
    ledger = sorted(ledger, key=lambda e: (e.occurred_at, e.event_id))
    external = sorted(external, key=lambda e: (e.occurred_at, e.event_id))
    l_amt = sum((e.amount for e in ledger), Decimal(0)) if ledger else None
    x_amt = sum((e.amount for e in external), Decimal(0)) if external else None
    assets = sorted({e.asset for e in ledger + external})

    def result(status, break_type, detail, delta=None, lag=None):
        return MatchResult(
            rail=rail, txn_key=key, status=status, break_type=break_type, asset=",".join(assets),
            ledger_amount=l_amt, external_amount=x_amt, amount_delta=delta, lag_seconds=lag,
            ledger_event_ids=tuple(sorted(e.event_id for e in ledger)),
            external_event_ids=tuple(sorted(e.event_id for e in external)),
            detail=detail,
        )

    max_lag_s = tol.max_lag.total_seconds()

    if not external:
        age = (as_of - ledger[0].occurred_at).total_seconds()
        if age <= max_lag_s:
            return result(IN_TRANSIT, None, f"ledger entry awaiting {rail} confirmation ({_fmt_age(age)} old)")
        return result(BREAK, UNMATCHED_LEDGER,
                      f"ledger claims {dec_str(l_amt)} {assets[0]} with no {rail} record after {_fmt_age(age)}")

    if not ledger:
        age = (as_of - external[0].occurred_at).total_seconds()
        if age <= max_lag_s:
            return result(IN_TRANSIT, None, f"{rail} event awaiting ledger booking ({_fmt_age(age)} old)")
        return result(BREAK, ORPHAN_EXTERNAL,
                      f"{rail} moved {dec_str(x_amt)} {assets[0]} with no ledger entry after {_fmt_age(age)}")

    lag = int((ledger[0].occurred_at - external[0].occurred_at).total_seconds())
    lagged = abs(lag) > max_lag_s
    multi = len(ledger) > 1 or len(external) > 1
    note_multi = f"; {len(ledger)} ledger vs {len(external)} {rail} events (possible duplicate)" if multi else ""

    if len(assets) > 1:
        return result(BREAK, AMOUNT_MISMATCH, f"asset mismatch {assets}{note_multi}", None, lag)

    delta = l_amt - x_amt
    if abs(delta) > tol.amount_tolerance:
        also = f"; also lagged {_fmt_age(abs(lag))}" if lagged else ""
        return result(BREAK, AMOUNT_MISMATCH,
                      f"ledger {dec_str(l_amt)} vs {rail} {dec_str(x_amt)} (delta {dec_str(delta)}){note_multi}{also}",
                      delta, lag)
    if lagged:
        direction = "after" if lag > 0 else "before"
        return result(BREAK, TIMING_LAG,
                      f"ledger booked {_fmt_age(abs(lag))} {direction} {rail} (limit {_fmt_age(max_lag_s)})",
                      delta, lag)
    return result(MATCHED, None, "matched" + note_multi, delta, lag)


def reconcile(events: Iterable[Event], tolerances: dict[str, RailTolerance], as_of: datetime) -> list[MatchResult]:
    ledger_by_rail: dict[str, dict[str, list[Event]]] = defaultdict(lambda: defaultdict(list))
    external_by_rail: dict[str, dict[str, list[Event]]] = defaultdict(lambda: defaultdict(list))
    for e in events:
        if e.rail not in tolerances:
            continue  # e.g. internal ledger transfers: stored, not reconciled
        if e.source == LEDGER:
            ledger_by_rail[e.rail][e.txn_key].append(e)
        elif e.source == e.rail:
            external_by_rail[e.rail][e.txn_key].append(e)

    results = []
    for rail in sorted(tolerances):
        ledger, external = ledger_by_rail[rail], external_by_rail[rail]
        for key in sorted(set(ledger) | set(external)):
            results.append(_classify(rail, key, ledger.get(key, []), external.get(key, []), tolerances[rail], as_of))
    return results
