"""Command-line interface: `recon <command>`. Run `recon -h` for help."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import sample_data
from .config import Config, load_config, load_dotenv
from .db import connect
from .engine import resolve_run_id, run_reconciliation
from .ingest import BatchResult, ingest_file
from .models import LEDGER, ONCHAIN, PROCESSOR
from .reporting import build_summary, trace, write_report

DEFAULT_DB = Path("data/recon.db")
DEMO_DB = Path("data/demo/recon.db")


def _table(rows: list[list], headers: list[str]) -> str:
    cells = [headers] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _print_batch(b: BatchResult) -> None:
    if b.already_ingested:
        print(f"[{b.source}] {b.origin}: already ingested as batch {b.batch_id} - no-op")
        return
    skipped = ", ".join(f"{k}={v}" for k, v in sorted(b.skipped.items())) or "none"
    extra = f", attestations={b.n_attestations}" if b.n_attestations else ""
    print(f"[{b.source}] {b.origin}: batch {b.batch_id} seen={b.n_seen} inserted={b.n_inserted} "
          f"unchanged={b.n_unchanged} revised={b.n_revised}{extra}; skipped: {skipped}")


def _print_summary(summary: dict) -> None:
    print(f"\nRun {summary['run_id']}  as_of {summary['as_of']}")
    print(f"  inputs  {summary['input_fingerprint'][:16]}...  config {summary['config_fingerprint'][:16]}...")
    print("  events  " + ", ".join(f"{k}={v}" for k, v in summary["event_counts"].items()))

    rows = []
    for rail, groups in summary["results"].items():
        for label, g in groups.items():
            outcome, asset = label.rsplit(":", 1)
            rows.append([rail, outcome, asset, g["count"], g["ledger_amount"], g["external_amount"], g["abs_delta"]])
    print("\n" + _table(rows, ["rail", "outcome", "asset", "count", "ledger", "external", "|delta|"]))

    if summary["reserves_coverage"]:
        window = next(iter(summary["reserves_coverage"].values()))["window_days"]
        rows = [[a, c["as_of_date"], c["claimed_balance"], c["verified_balance"], c["coverage"], c["rolling_min"],
                 c["rolling_avg"], c["snapshot_id"]] for a, c in summary["reserves_coverage"].items()]
        print("\nReserves coverage (verified on-chain / claimed ledger)")
        print(_table(rows, ["asset", "date", "claimed", "verified", "coverage", f"{window}d min", f"{window}d avg",
                            "snapshot_id"]))
    for a in summary["balance_attestations"]:
        print(f"\nIndexer balance check {a['asset']}: indexer={a['indexer_balance']} "
              f"reconstructed={a['reconstructed_balance']} gap={a['gap']} (observed {a['observed_at']})")


def cmd_ingest(args, cfg: Config) -> int:
    conn = connect(args.db)
    for f in args.files:
        _print_batch(ingest_file(conn, args.source, f, cfg))
    return 0


def cmd_fetch_onchain(args, cfg: Config) -> int:
    from .etherscan import EtherscanClient, fetch_wallet

    address = (args.address or cfg.wallet_address or "").lower()
    if not address:
        print("error: no wallet address; pass --address or set [wallet].address in recon.toml", file=sys.stderr)
        return 2
    client = EtherscanClient(os.environ.get("ETHERSCAN_API_KEY", ""), chain_id=cfg.chain_id)
    paths = fetch_wallet(client, address, cfg.tokens, Path(args.out), include_native=args.native,
                         start_block=args.start_block)
    for p in paths:
        print(f"snapshot written: {p.as_posix()}")
    if not args.no_ingest:
        conn = connect(args.db)
        for p in paths:
            _print_batch(ingest_file(conn, ONCHAIN, p, cfg))
    return 0


def cmd_match(args, cfg: Config) -> int:
    conn = connect(args.db)
    outcome = run_reconciliation(conn, cfg, as_of=args.as_of, verify=args.verify)
    if outcome.verified is not None:
        state = "identical" if outcome.verified else "DIFFERENT - determinism violated"
        print(f"run {outcome.run_id}: recomputed from {outcome.n_events} events, results {state}")
        return 0 if outcome.verified else 1
    verb = "unchanged inputs - reused existing" if outcome.reused else "created"
    print(f"run {outcome.run_id} ({verb}) as_of {outcome.as_of}, {outcome.n_events} events")
    if not args.quiet:
        _print_summary(build_summary(conn, outcome.run_id))
    return 0


def cmd_report(args, cfg: Config) -> int:
    conn = connect(args.db)
    run_id = resolve_run_id(conn, args.run)
    out = write_report(conn, run_id, Path(args.out))
    _print_summary(build_summary(conn, run_id))
    print(f"\nreport written to {out.as_posix()}/ (results.csv, breaks.csv, coverage.csv, summary.json)")
    return 0


def cmd_trace(args, cfg: Config) -> int:
    conn = connect(args.db)
    run_id = resolve_run_id(conn, args.run)
    t = trace(conn, args.id, run_id)
    if args.json:
        print(json.dumps(t, indent=2, default=str))
        return 0

    rec = t["record"]
    print(f"{t['kind']} in run {run_id}")
    for k, v in rec.items():
        if k != "run_id":
            print(f"  {k:<18} {v}")
    if t["kind"] == "event":
        print(f"\n  revisions ({len(t['revisions'])}):")
        for r in t["revisions"]:
            print(f"    {r['ingested_at']}  {r['content_hash'][:12]}  from {r['origin']}\n      raw: {r['raw_json']}")
        print("  used in: " + (", ".join(f"{u['result_id']} ({u['break_type'] or u['status']})"
                                         for u in t["used_in"]) or "no match result"))
        return 0

    for side, events in t["events"].items():
        print(f"\n  {side} events ({len(events)}):")
        rows = [[e["event_id"], e["source"], e["source_event_id"][:40], e["amount"], e["asset"], e["occurred_at"],
                 Path(e["origin"]).name] for e in events]
        if rows:
            print("    " + _table(rows, ["event_id", "source", "source_event_id", "amount", "asset", "occurred_at",
                                         "file"]).replace("\n", "\n    "))
    ok = all(t["checks"].values())
    print("\n  arithmetic check: " + ("OK - stored figures equal the sum of the events above" if ok
                                     else f"FAILED {t['checks']}"))
    return 0 if ok else 1


def cmd_demo(args, cfg: Config) -> int:
    out = Path(args.out)
    manifest = sample_data.generate(out, seed=args.seed)
    print(f"sample data written to {out.as_posix()}/ (seed {args.seed})\n")

    conn = connect(args.db)
    order = [(PROCESSOR, manifest.files["processor"]), (LEDGER, manifest.files["ledger"]),
             (ONCHAIN, manifest.files["onchain"])]
    print("== ingest")
    for source, files in order:
        for f in files:
            _print_batch(ingest_file(conn, source, f, cfg))

    print("\n== match")
    first = run_reconciliation(conn, cfg)
    print(f"run {first.run_id} ({'reused' if first.reused else 'created'}), {first.n_events} events")

    print("\n== replay: ingest the same files and match again")
    for source, files in order:
        for f in files:
            _print_batch(ingest_file(conn, source, f, cfg))
    second = run_reconciliation(conn, cfg)
    verified = run_reconciliation(conn, cfg, verify=True)
    print(f"run {second.run_id} ({'reused' if second.reused else 'created'}); "
          f"recompute {'identical' if verified.verified else 'DIFFERENT'}")

    report_dir = write_report(conn, first.run_id, Path(args.reports))
    _print_summary(build_summary(conn, first.run_id))
    print(f"\nreport written to {report_dir.as_posix()}/")
    print(f"try: recon --db {Path(args.db).as_posix()} trace <result_id | snapshot_id | event_id>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recon", description=__doc__)
    p.add_argument("--db", help=f"SQLite database (default {DEFAULT_DB.as_posix()}; demo: {DEMO_DB.as_posix()})")
    p.add_argument("--config", help="TOML config file (default ./recon.toml if present)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="ingest source files (safe to repeat)")
    s.add_argument("source", choices=[PROCESSOR, LEDGER, ONCHAIN])
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("fetch-onchain", help="snapshot wallet activity from Etherscan, then ingest it")
    s.add_argument("--address", help="wallet address (default [wallet].address in config)")
    s.add_argument("--start-block", type=int, default=0)
    s.add_argument("--native", action="store_true", help="also fetch native ETH transactions and balance")
    s.add_argument("--out", default="data/raw/etherscan")
    s.add_argument("--no-ingest", action="store_true", help="only write snapshots")
    s.set_defaults(func=cmd_fetch_onchain)

    s = sub.add_parser("match", help="reconcile all ingested events (idempotent)")
    s.add_argument("--as-of", help="cutoff timestamp (default: latest event time)")
    s.add_argument("--verify", action="store_true", help="recompute an existing run and confirm identical results")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("report", help="write CSV/JSON reports for a run")
    s.add_argument("--run", default="latest", help="run id or prefix (default latest)")
    s.add_argument("--out", default="reports")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("trace", help="show the source records behind any reported number")
    s.add_argument("id", help="result_id, snapshot_id, or event_id (prefix ok)")
    s.add_argument("--run", default="latest")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("demo", help="generate sample data and run the full pipeline offline")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", default="data/sample")
    s.add_argument("--reports", default="reports")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    args.db = Path(args.db) if args.db else (DEMO_DB if args.command == "demo" else DEFAULT_DB)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (RuntimeError, LookupError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
