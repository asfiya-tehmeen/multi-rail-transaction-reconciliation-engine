from __future__ import annotations

import random
from datetime import timedelta

from conftest import T0, ev

from recon.models import (
    AMOUNT_MISMATCH, BREAK, IN_TRANSIT, LEDGER, MATCHED, ONCHAIN, ORPHAN_EXTERNAL, PROCESSOR, TIMING_LAG,
    UNMATCHED_LEDGER,
)
from recon.matching import reconcile

AS_OF = T0 + timedelta(days=10)


def one(events, cfg, as_of=AS_OF):
    [result] = reconcile(events, cfg.tolerances, as_of)
    return result


def test_exact_match(cfg):
    r = one([ev(LEDGER, "l1", "ord_1", "10.00", T0 + timedelta(hours=1)), ev(PROCESSOR, "p1", "ord_1", "10")], cfg)
    assert (r.status, r.break_type, r.amount_delta, r.lag_seconds) == (MATCHED, None, 0, 3600)


def test_within_tolerance_matches(cfg):
    r = one([ev(LEDGER, "l1", "ord_1", "10.01"), ev(PROCESSOR, "p1", "ord_1", "10.00")], cfg)
    assert r.status == MATCHED


def test_amount_mismatch(cfg):
    r = one([ev(LEDGER, "l1", "ord_1", "9.41"), ev(PROCESSOR, "p1", "ord_1", "10.00")], cfg)
    assert (r.status, r.break_type, str(r.amount_delta)) == (BREAK, AMOUNT_MISMATCH, "-0.59")


def test_timing_lag(cfg):
    r = one([ev(LEDGER, "l1", "ord_1", "10", T0 + timedelta(days=5)), ev(PROCESSOR, "p1", "ord_1", "10")], cfg)
    assert (r.status, r.break_type) == (BREAK, TIMING_LAG)


def test_amount_takes_precedence_over_lag(cfg):
    r = one([ev(LEDGER, "l1", "ord_1", "12", T0 + timedelta(days=5)), ev(PROCESSOR, "p1", "ord_1", "10")], cfg)
    assert r.break_type == AMOUNT_MISMATCH and "also lagged" in r.detail


def test_unmatched_ledger_vs_in_transit(cfg):
    old = one([ev(LEDGER, "l1", "ord_1", "10")], cfg)
    fresh = one([ev(LEDGER, "l1", "ord_1", "10", AS_OF - timedelta(hours=1))], cfg)
    assert (old.status, old.break_type) == (BREAK, UNMATCHED_LEDGER)
    assert (fresh.status, fresh.break_type) == (IN_TRANSIT, None)


def test_orphan_external(cfg):
    r = one([ev(ONCHAIN, "0xabc:0", "0xabc", "500")], cfg)
    assert (r.status, r.break_type) == (BREAK, ORPHAN_EXTERNAL)


def test_rails_are_isolated(cfg):
    # Same key on different rails must not match each other.
    results = reconcile([ev(LEDGER, "l1", "k", "10", rail=ONCHAIN), ev(PROCESSOR, "p1", "k", "10")],
                        cfg.tolerances, AS_OF)
    assert sorted(r.break_type for r in results) == [ORPHAN_EXTERNAL, UNMATCHED_LEDGER]


def test_multi_leg_nets_by_key(cfg):
    events = [ev(LEDGER, "l1", "0xh", "100", rail=ONCHAIN), ev(ONCHAIN, "0xh:1", "0xh", "60"),
              ev(ONCHAIN, "0xh:2", "0xh", "40")]
    r = one(events, cfg)
    assert r.status == MATCHED and len(r.external_event_ids) == 2


def test_internal_rail_ignored(cfg):
    assert reconcile([ev(LEDGER, "l1", "j1", "5", rail="internal")], cfg.tolerances, AS_OF) == []


def test_order_independent(cfg):
    events = [ev(LEDGER, f"l{i}", f"ord_{i % 7}", i) for i in range(30)] + \
             [ev(PROCESSOR, f"p{i}", f"ord_{i % 9}", i) for i in range(30)]
    baseline = [r.digest_line() for r in reconcile(events, cfg.tolerances, AS_OF)]
    for seed in range(5):
        shuffled = events[:]
        random.Random(seed).shuffle(shuffled)
        assert [r.digest_line() for r in reconcile(shuffled, cfg.tolerances, AS_OF)] == baseline
