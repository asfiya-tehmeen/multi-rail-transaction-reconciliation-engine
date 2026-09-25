"""SQLite storage. Events are append-with-revisions; run outputs are keyed by a deterministic run id."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from .models import Event
from .utils import parse_ts, ts_str

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id      TEXT PRIMARY KEY,   -- hash(source, file sha256): the same file can only land once
    source        TEXT NOT NULL,
    origin        TEXT NOT NULL,
    file_sha256   TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    n_seen        INTEGER NOT NULL,
    n_inserted    INTEGER NOT NULL,
    n_unchanged   INTEGER NOT NULL,
    n_revised     INTEGER NOT NULL,
    n_skipped     INTEGER NOT NULL,
    skipped_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    rail            TEXT NOT NULL,
    txn_key         TEXT NOT NULL,
    amount          TEXT NOT NULL,    -- Decimal as text; never stored as float
    asset           TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    account         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    batch_id        TEXT NOT NULL REFERENCES ingest_batches(batch_id),
    UNIQUE (source, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_rail_key ON events (rail, txn_key);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (occurred_at);

-- Every distinct version of every event ever seen, with the raw source record.
CREATE TABLE IF NOT EXISTS event_revisions (
    event_id      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    batch_id      TEXT NOT NULL REFERENCES ingest_batches(batch_id),
    raw_json      TEXT NOT NULL,
    PRIMARY KEY (event_id, content_hash)
);

CREATE TABLE IF NOT EXISTS balance_attestations (
    attestation_id TEXT PRIMARY KEY,
    address        TEXT NOT NULL,
    asset          TEXT NOT NULL,
    contract       TEXT NOT NULL,
    balance        TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    batch_id       TEXT NOT NULL REFERENCES ingest_batches(batch_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,  -- hash(input fingerprint, config fingerprint, as_of)
    as_of              TEXT NOT NULL,
    input_fingerprint  TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    results_digest     TEXT NOT NULL,
    n_events           INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_results (
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    result_id        TEXT NOT NULL,
    rail             TEXT NOT NULL,
    txn_key          TEXT NOT NULL,
    status           TEXT NOT NULL,
    break_type       TEXT,
    asset            TEXT NOT NULL,
    ledger_amount    TEXT,
    external_amount  TEXT,
    amount_delta     TEXT,
    lag_seconds      INTEGER,
    detail           TEXT NOT NULL,
    PRIMARY KEY (run_id, result_id)
);

CREATE TABLE IF NOT EXISTS result_events (
    run_id    TEXT NOT NULL,
    result_id TEXT NOT NULL,
    event_id  TEXT NOT NULL REFERENCES events(event_id),
    side      TEXT NOT NULL,              -- 'ledger' or 'external'
    PRIMARY KEY (run_id, result_id, event_id)
);

CREATE TABLE IF NOT EXISTS coverage_snapshots (
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    snapshot_id      TEXT NOT NULL,
    asset            TEXT NOT NULL,
    as_of_date       TEXT NOT NULL,
    claimed_balance  TEXT NOT NULL,
    verified_balance TEXT NOT NULL,
    coverage         TEXT,
    rolling_min      TEXT,
    rolling_avg      TEXT,
    window_days      INTEGER NOT NULL,
    PRIMARY KEY (run_id, snapshot_id)
);

-- Events that moved each daily snapshot. A snapshot's full lineage is the union over all prior days.
CREATE TABLE IF NOT EXISTS snapshot_events (
    run_id      TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    event_id    TEXT NOT NULL REFERENCES events(event_id),
    side        TEXT NOT NULL,            -- 'claimed' or 'verified'
    PRIMARY KEY (run_id, snapshot_id, event_id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        source=row["source"],
        source_event_id=row["source_event_id"],
        rail=row["rail"],
        txn_key=row["txn_key"],
        amount=Decimal(row["amount"]),
        asset=row["asset"],
        occurred_at=parse_ts(row["occurred_at"]),
        account=row["account"],
    )


EVENT_COLUMNS = "event_id, source, source_event_id, rail, txn_key, amount, asset, occurred_at, account, content_hash"


def load_events(conn: sqlite3.Connection, as_of=None) -> list[Event]:
    if as_of is None:
        rows = conn.execute(f"SELECT {EVENT_COLUMNS} FROM events ORDER BY event_id")
    else:
        rows = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE occurred_at <= ? ORDER BY event_id", (ts_str(as_of),)
        )
    return [row_to_event(r) for r in rows]
