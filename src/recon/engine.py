"""Run orchestration: fingerprint inputs, reconcile, compute coverage, persist with lineage.

A run id is hash(input fingerprint, config fingerprint, as_of). The input fingerprint covers the id and
content hash of every event in scope, so:
  - re-running on an unchanged event stream resolves to the same run id and is a no-op;
  - any new, revised, or removed event produces a new run id, never a silent overwrite.
`as_of` defaults to the latest event timestamp (not the wall clock) so the default is repeatable too.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .db import load_events
from .matching import MatchResult, reconcile
from .reserves import CoverageSnapshot, compute_coverage
from .utils import dec_str, parse_ts, sha256_hex, short_id, ts_str


@dataclass
class RunOutcome:
    run_id: str
    as_of: str
    n_events: int
    reused: bool
    verified: bool | None = None  # set when --verify recomputed an existing run


def _opt(d):
    return None if d is None else dec_str(d)


def results_digest(results: list[MatchResult], snapshots: list[CoverageSnapshot]) -> str:
    return sha256_hex(*sorted(r.digest_line() for r in results), "||", *sorted(s.digest_line() for s in snapshots))


def run_reconciliation(conn: sqlite3.Connection, cfg: Config, as_of: str | None = None,
                       verify: bool = False) -> RunOutcome:
    all_events = load_events(conn)
    if not all_events:
        raise RuntimeError("no events ingested yet; run `recon ingest ...` first")

    as_of_dt = parse_ts(as_of) if as_of else max(e.occurred_at for e in all_events)
    events = [e for e in all_events if e.occurred_at <= as_of_dt]
    as_of_s = ts_str(as_of_dt)

    input_fp = sha256_hex("inputs", *sorted(f"{e.event_id}:{e.content_hash}" for e in events))
    config_fp = cfg.fingerprint()
    run_id = short_id("run", input_fp, config_fp, as_of_s)

    existing = conn.execute("SELECT results_digest FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if existing and not verify:
        return RunOutcome(run_id, as_of_s, len(events), reused=True)

    results = reconcile(events, cfg.tolerances, as_of_dt)
    snapshots = compute_coverage(events, cfg.reserve_assets, cfg.rolling_window_days, as_of_dt)
    digest = results_digest(results, snapshots)

    if existing:
        return RunOutcome(run_id, as_of_s, len(events), reused=True, verified=digest == existing["results_digest"])

    with conn:
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, as_of_s, input_fp, config_fp, digest, len(events), ts_str(datetime.now(timezone.utc))),
        )
        for r in results:
            rid = r.result_id
            conn.execute(
                "INSERT INTO match_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, rid, r.rail, r.txn_key, r.status, r.break_type, r.asset, _opt(r.ledger_amount),
                 _opt(r.external_amount), _opt(r.amount_delta), r.lag_seconds, r.detail),
            )
            conn.executemany(
                "INSERT INTO result_events VALUES (?, ?, ?, ?)",
                [(run_id, rid, eid, "ledger") for eid in r.ledger_event_ids]
                + [(run_id, rid, eid, "external") for eid in r.external_event_ids],
            )
        for s in snapshots:
            sid = s.snapshot_id
            conn.execute(
                "INSERT INTO coverage_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, sid, s.asset, s.as_of_date.isoformat(), dec_str(s.claimed_balance),
                 dec_str(s.verified_balance), _opt(s.coverage), _opt(s.rolling_min), _opt(s.rolling_avg),
                 s.window_days),
            )
            conn.executemany(
                "INSERT INTO snapshot_events VALUES (?, ?, ?, ?)",
                [(run_id, sid, eid, "claimed") for eid in s.claimed_event_ids]
                + [(run_id, sid, eid, "verified") for eid in s.verified_event_ids],
            )
    return RunOutcome(run_id, as_of_s, len(events), reused=False)


def resolve_run_id(conn: sqlite3.Connection, run: str | None) -> str:
    if run in (None, "latest"):
        row = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("no runs yet; run `recon match` first")
        return row["run_id"]
    rows = conn.execute("SELECT run_id FROM runs WHERE run_id LIKE ?", (run + "%",)).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"run id {run!r} matched {len(rows)} runs")
    return rows[0]["run_id"]
