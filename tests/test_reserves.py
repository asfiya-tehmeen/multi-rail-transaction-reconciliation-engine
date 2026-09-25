from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from conftest import T0, ev

from recon.models import LEDGER, ONCHAIN
from recon.reserves import compute_coverage


def test_daily_and_rolling_coverage():
    events = [
        ev(LEDGER, "l1", "0x1", "100", T0, rail=ONCHAIN), ev(ONCHAIN, "0x1:0", "0x1", "100", T0),
        # day 2: ledger claims a 100 deposit that never lands on-chain
        ev(LEDGER, "l2", "0x2", "100", T0 + timedelta(days=1), rail=ONCHAIN),
        # day 3: a real deposit, booked correctly
        ev(LEDGER, "l3", "0x3", "50", T0 + timedelta(days=2), rail=ONCHAIN),
        ev(ONCHAIN, "0x3:0", "0x3", "50", T0 + timedelta(days=2)),
    ]
    snaps = compute_coverage(events, (), window_days=2, as_of=T0 + timedelta(days=3))
    assert [s.coverage for s in snaps] == [Decimal("1"), Decimal("0.5"), Decimal("0.6"), Decimal("0.6")]
    assert [s.rolling_min for s in snaps] == [Decimal("1"), Decimal("0.5"), Decimal("0.5"), Decimal("0.6")]
    assert snaps[-1].claimed_balance == 250 and snaps[-1].verified_balance == 150
    assert snaps[-1].claimed_event_ids == () and snaps[2].verified_event_ids == (events[4].event_id,)


def test_zero_claimed_has_no_ratio():
    snaps = compute_coverage([ev(ONCHAIN, "0x1:0", "0x1", "10")], (), 7, T0)
    assert snaps[0].coverage is None and snaps[0].verified_balance == 10


def test_processor_rail_does_not_count_as_reserves():
    snaps = compute_coverage([ev(LEDGER, "l1", "ord_1", "10")], (), 7, T0)
    assert snaps == []
