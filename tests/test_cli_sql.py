from __future__ import annotations

from pathlib import Path

from recon.cli import _named_queries, main

DEMO_SQL = Path(__file__).resolve().parents[1] / "sql" / "demo_queries.sql"


def test_named_queries_split():
    blocks = _named_queries("-- header\n-- name: a\nSELECT 1;\n\n-- name: b\nSELECT 2;\n")
    assert blocks == [("a", "SELECT 1;"), ("b", "SELECT 2;")]


def test_demo_queries_run_and_db_is_read_only(tmp_path, capsys):
    db = tmp_path / "recon.db"
    assert main(["--db", str(db), "demo", "--out", str(tmp_path / "s"), "--reports", str(tmp_path / "r")]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "sql", str(DEMO_SQL), "--no-echo"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n== ") == 5 and "UNMATCHED_LEDGER" in out
    assert main(["--db", str(db), "sql", "DELETE FROM runs"]) == 1
    assert main(["--db", str(db), "sql", "SELECT COUNT(*) FROM runs"]) == 0
