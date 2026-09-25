"""Run reports and lineage tracing.

Every figure in a report carries the ids needed to walk back to source records:
    summary line -> result_ids -> event_ids -> source_event_id + raw record (event_revisions)
    coverage row -> snapshot_id -> every event up to that day
`trace` performs that walk and re-adds the amounts, so a traced number is checked, not just listed.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .utils import dec_str


def _d(v) -> Decimal:
    return Decimal(v) if v not in (None, "") else Decimal(0)


def build_summary(conn: sqlite3.Connection, run_id: str) -> dict:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    counts = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM events WHERE occurred_at <= ? GROUP BY source ORDER BY source",
        (run["as_of"],))}

    groups: dict = defaultdict(lambda: {"count": 0, "ledger_amount": Decimal(0), "external_amount": Decimal(0),
                                        "abs_delta": Decimal(0), "result_ids": []})
    for r in conn.execute("SELECT * FROM match_results WHERE run_id = ? ORDER BY rail, result_id", (run_id,)):
        g = groups[(r["rail"], r["asset"], r["break_type"] or r["status"])]
        g["count"] += 1
        g["ledger_amount"] += _d(r["ledger_amount"])
        g["external_amount"] += _d(r["external_amount"])
        g["abs_delta"] += abs(_d(r["amount_delta"]))
        g["result_ids"].append(r["result_id"])

    results: dict = defaultdict(dict)
    for (rail, asset, label), g in sorted(groups.items()):
        results[rail][f"{label}:{asset}"] = {
            "count": g["count"], "ledger_amount": dec_str(g["ledger_amount"]),
            "external_amount": dec_str(g["external_amount"]), "abs_delta": dec_str(g["abs_delta"]),
            "result_ids": g["result_ids"],
        }

    coverage = {}
    for row in conn.execute(
        "SELECT * FROM coverage_snapshots c WHERE run_id = ? AND as_of_date ="
        " (SELECT MAX(as_of_date) FROM coverage_snapshots WHERE run_id = c.run_id AND asset = c.asset)"
        " ORDER BY asset", (run_id,)):
        coverage[row["asset"]] = {k: row[k] for k in (
            "snapshot_id", "as_of_date", "claimed_balance", "verified_balance", "coverage",
            "rolling_min", "rolling_avg", "window_days")}

    attestations = []
    for a in conn.execute(
        "SELECT * FROM balance_attestations a WHERE observed_at ="
        " (SELECT MAX(observed_at) FROM balance_attestations WHERE address = a.address AND asset = a.asset)"
        " ORDER BY asset"):
        reconstructed = coverage.get(a["asset"], {}).get("verified_balance")
        attestations.append({
            "attestation_id": a["attestation_id"], "address": a["address"], "asset": a["asset"],
            "indexer_balance": a["balance"], "observed_at": a["observed_at"],
            "reconstructed_balance": reconstructed,
            "gap": dec_str(_d(a["balance"]) - _d(reconstructed)) if reconstructed is not None else None,
        })

    return {
        "run_id": run_id, "as_of": run["as_of"], "input_fingerprint": run["input_fingerprint"],
        "config_fingerprint": run["config_fingerprint"], "results_digest": run["results_digest"],
        "event_counts": counts, "results": dict(results), "reserves_coverage": coverage,
        "balance_attestations": attestations,
    }


RESULT_COLUMNS = ["result_id", "rail", "txn_key", "status", "break_type", "asset", "ledger_amount",
                  "external_amount", "amount_delta", "lag_seconds", "detail"]


def write_report(conn: sqlite3.Connection, run_id: str, out_dir: Path) -> Path:
    out = out_dir / run_id
    out.mkdir(parents=True, exist_ok=True)

    event_ids = defaultdict(lambda: {"ledger": [], "external": []})
    for r in conn.execute("SELECT result_id, event_id, side FROM result_events WHERE run_id = ? ORDER BY event_id",
                          (run_id,)):
        event_ids[r["result_id"]][r["side"]].append(r["event_id"])

    rows = conn.execute(f"SELECT {', '.join(RESULT_COLUMNS)} FROM match_results WHERE run_id = ?"
                        " ORDER BY status, break_type, rail, txn_key", (run_id,)).fetchall()
    for name, predicate in (("results.csv", lambda r: True), ("breaks.csv", lambda r: r["status"] == "BREAK")):
        with (out / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(RESULT_COLUMNS + ["ledger_event_ids", "external_event_ids"])
            for r in rows:
                if predicate(r):
                    ids = event_ids[r["result_id"]]
                    w.writerow([r[c] for c in RESULT_COLUMNS] + [" ".join(ids["ledger"]), " ".join(ids["external"])])

    cov_cols = ["snapshot_id", "asset", "as_of_date", "claimed_balance", "verified_balance", "coverage",
                "rolling_min", "rolling_avg", "window_days"]
    with (out / "coverage.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cov_cols)
        for r in conn.execute(f"SELECT {', '.join(cov_cols)} FROM coverage_snapshots WHERE run_id = ?"
                              " ORDER BY asset, as_of_date", (run_id,)):
            w.writerow([r[c] for c in cov_cols])

    (out / "summary.json").write_text(json.dumps(build_summary(conn, run_id), indent=2), encoding="utf-8")
    return out


def _event_rows(conn: sqlite3.Connection, where: str, params: tuple) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT e.event_id, e.source, e.source_event_id, e.txn_key, e.amount, e.asset, e.occurred_at,"
        f" e.content_hash, b.origin FROM events e JOIN ingest_batches b ON b.batch_id = e.batch_id"
        f" WHERE {where} ORDER BY e.occurred_at, e.event_id", params).fetchall()


def trace(conn: sqlite3.Connection, ident: str, run_id: str) -> dict:
    """Resolve an id prefix (result, snapshot, or event) to the source records behind it."""
    like = ident + "%"

    result = conn.execute("SELECT * FROM match_results WHERE run_id = ? AND result_id LIKE ?",
                          (run_id, like)).fetchall()
    if len(result) == 1:
        r = result[0]
        sides = {}
        for side in ("ledger", "external"):
            evs = _event_rows(conn, "e.event_id IN (SELECT event_id FROM result_events"
                                    " WHERE run_id = ? AND result_id = ? AND side = ?)",
                              (run_id, r["result_id"], side))
            sides[side] = evs
        recomputed = {s: sum((_d(e["amount"]) for e in evs), Decimal(0)) for s, evs in sides.items()}
        checks = {
            "ledger_amount": r["ledger_amount"] is None or recomputed["ledger"] == _d(r["ledger_amount"]),
            "external_amount": r["external_amount"] is None or recomputed["external"] == _d(r["external_amount"]),
        }
        return {"kind": "result", "record": dict(r), "events": {s: [dict(e) for e in v] for s, v in sides.items()},
                "checks": checks}

    snap = conn.execute("SELECT * FROM coverage_snapshots WHERE run_id = ? AND snapshot_id LIKE ?",
                        (run_id, like)).fetchall()
    if len(snap) == 1:
        s = snap[0]
        sides = {}
        for side in ("claimed", "verified"):
            sides[side] = _event_rows(
                conn,
                "e.event_id IN (SELECT se.event_id FROM snapshot_events se JOIN coverage_snapshots cs"
                " ON cs.run_id = se.run_id AND cs.snapshot_id = se.snapshot_id"
                " WHERE se.run_id = ? AND cs.asset = ? AND cs.as_of_date <= ? AND se.side = ?)",
                (run_id, s["asset"], s["as_of_date"], side))
        recomputed = {k: sum((_d(e["amount"]) for e in v), Decimal(0)) for k, v in sides.items()}
        checks = {
            "claimed_balance": recomputed["claimed"] == _d(s["claimed_balance"]),
            "verified_balance": recomputed["verified"] == _d(s["verified_balance"]),
        }
        return {"kind": "coverage_snapshot", "record": dict(s),
                "events": {k: [dict(e) for e in v] for k, v in sides.items()}, "checks": checks}

    events = _event_rows(conn, "e.event_id LIKE ?", (like,))
    if len(events) == 1:
        e = events[0]
        revisions = [dict(r) for r in conn.execute(
            "SELECT r.content_hash, r.batch_id, b.origin, b.ingested_at, r.raw_json FROM event_revisions r"
            " JOIN ingest_batches b ON b.batch_id = r.batch_id WHERE r.event_id = ? ORDER BY b.ingested_at",
            (e["event_id"],))]
        used_in = [dict(r) for r in conn.execute(
            "SELECT m.result_id, m.status, m.break_type FROM result_events re JOIN match_results m"
            " ON m.run_id = re.run_id AND m.result_id = re.result_id WHERE re.run_id = ? AND re.event_id = ?",
            (run_id, e["event_id"]))]
        return {"kind": "event", "record": dict(e), "revisions": revisions, "used_in": used_in, "checks": {}}

    matches = len(result) + len(snap) + len(events)
    raise LookupError(f"{ident!r} matched {matches} ids in run {run_id}; use a longer prefix" if matches
                      else f"{ident!r} not found in run {run_id}")
