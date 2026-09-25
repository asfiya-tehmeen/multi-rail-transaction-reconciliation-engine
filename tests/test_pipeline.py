"""End-to-end: sample data -> ingest -> match -> report -> trace, plus idempotency guarantees."""

from __future__ import annotations

import csv
from collections import Counter

from recon.engine import run_reconciliation
from recon.ingest import ingest_file
from recon.ingest.base import ingest_bytes
from recon.models import LEDGER, ONCHAIN, PROCESSOR
from recon.reporting import build_summary, trace, write_report


def breaks_by(conn, run_id):
    rows = conn.execute("SELECT rail, break_type, txn_key FROM match_results WHERE run_id = ? AND status = 'BREAK'",
                        (run_id,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["rail"], r["break_type"]), set()).add(r["txn_key"])
    return out


def test_finds_exactly_the_injected_breaks(loaded_db, sample, cfg):
    run = run_reconciliation(loaded_db, cfg)
    found = breaks_by(loaded_db, run.run_id)
    for rail, injected in ((PROCESSOR, sample.processor), (ONCHAIN, sample.onchain)):
        for kind in ("amount_mismatch", "timing_lag", "unmatched_ledger", "orphan"):
            label = {"orphan": "ORPHAN_EXTERNAL"}.get(kind, kind.upper())
            assert found.get((rail, label), set()) == set(injected[kind]), (rail, kind)


def test_reingest_same_file_is_noop(loaded_db, sample, cfg):
    before = loaded_db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    for source in (PROCESSOR, LEDGER, ONCHAIN):
        for f in sample.files[source]:
            assert ingest_file(loaded_db, source, f, cfg).already_ingested
    assert loaded_db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_reshaped_file_with_same_records_is_unchanged(loaded_db, sample, cfg):
    # Same records, different bytes (reordered rows): new batch, but zero inserts or revisions.
    path = sample.files[PROCESSOR][0]
    header, *rows = path.read_text(encoding="utf-8").splitlines()
    result = ingest_bytes(loaded_db, PROCESSOR, "\n".join([header, *reversed(rows)]).encode(), "reordered", cfg)
    assert not result.already_ingested
    assert (result.n_inserted, result.n_revised, result.n_unchanged) == (0, 0, result.n_seen)


def test_rerun_reuses_run_and_verifies(loaded_db, cfg):
    first = run_reconciliation(loaded_db, cfg)
    second = run_reconciliation(loaded_db, cfg)
    verified = run_reconciliation(loaded_db, cfg, verify=True)
    assert not first.reused and second.reused and second.run_id == first.run_id
    assert verified.verified is True
    assert loaded_db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_revised_event_creates_new_run_and_keeps_history(loaded_db, sample, cfg):
    first = run_reconciliation(loaded_db, cfg)
    path = sample.files[LEDGER][0]
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    target = next(r for r in rows if r["rail"] == "processor")
    target["amount"] = "999999.99"
    text = ",".join(rows[0].keys()) + "\n" + "\n".join(",".join(r.values()) for r in rows)
    result = ingest_bytes(loaded_db, LEDGER, text.encode(), "corrected", cfg)
    assert result.n_revised == 1

    second = run_reconciliation(loaded_db, cfg)
    assert second.run_id != first.run_id and not second.reused
    revisions = loaded_db.execute("SELECT COUNT(*) FROM event_revisions er JOIN events e USING (event_id)"
                                  " WHERE e.source_event_id = ?", (target["entry_id"],)).fetchone()[0]
    assert revisions == 2
    # The original run is untouched.
    assert loaded_db.execute("SELECT COUNT(*) FROM match_results WHERE run_id = ?", (first.run_id,)).fetchone()[0]


def test_every_result_and_snapshot_traces_and_adds_up(loaded_db, cfg):
    run = run_reconciliation(loaded_db, cfg)
    ids = [r[0] for r in loaded_db.execute("SELECT result_id FROM match_results WHERE run_id = ?", (run.run_id,))]
    ids += [r[0] for r in loaded_db.execute("SELECT snapshot_id FROM coverage_snapshots WHERE run_id = ?",
                                            (run.run_id,))]
    for ident in ids:
        t = trace(loaded_db, ident, run.run_id)
        assert all(t["checks"].values()), ident


def test_summary_counts_equal_result_ids(loaded_db, cfg, tmp_path):
    run = run_reconciliation(loaded_db, cfg)
    summary = build_summary(loaded_db, run.run_id)
    for groups in summary["results"].values():
        for g in groups.values():
            assert g["count"] == len(g["result_ids"])
    out = write_report(loaded_db, run.run_id, tmp_path / "reports")
    assert {p.name for p in out.iterdir()} == {"results.csv", "breaks.csv", "coverage.csv", "summary.json"}


def test_reconstructed_balance_matches_indexer(loaded_db, cfg):
    run = run_reconciliation(loaded_db, cfg)
    [att] = build_summary(loaded_db, run.run_id)["balance_attestations"]
    assert att["gap"] == "0"


def test_sample_generation_is_deterministic(tmp_path):
    from recon import sample_data

    a = sample_data.generate(tmp_path / "a", seed=11)
    b = sample_data.generate(tmp_path / "b", seed=11)
    for source in a.files:
        for fa, fb in zip(a.files[source], b.files[source]):
            assert fa.read_bytes() == fb.read_bytes()
    assert Counter(len(v) for v in a.processor.values()) == Counter(len(v) for v in b.processor.values())
