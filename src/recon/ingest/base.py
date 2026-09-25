"""Idempotent ingestion.

Two layers of protection make re-ingesting safe:
  1. Batch level: a batch id is the hash of (source, file bytes). Re-submitting the same file is a no-op.
  2. Event level: an event id is the hash of (source, source record id). A record already stored with the
     same content is counted as unchanged; one with different content is stored as a new revision and
     becomes the current version, with every prior version kept in `event_revisions`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..models import LEDGER, ONCHAIN, PROCESSOR, ParseResult
from ..utils import canonical_json, dec_str, file_sha256, short_id, ts_str
from .ledger import parse_ledger_csv
from .onchain import parse_etherscan_file
from .processor import parse_processor_csv


@dataclass
class BatchResult:
    batch_id: str
    source: str
    origin: str
    already_ingested: bool = False
    n_seen: int = 0
    n_inserted: int = 0
    n_unchanged: int = 0
    n_revised: int = 0
    n_attestations: int = 0
    skipped: dict = field(default_factory=dict)


def parse_source(source: str, data: bytes, cfg: Config) -> ParseResult:
    text = data.decode("utf-8-sig")
    if source == PROCESSOR:
        return parse_processor_csv(text)
    if source == LEDGER:
        return parse_ledger_csv(text)
    if source == ONCHAIN:
        return parse_etherscan_file(text, cfg.tokens_by_contract())
    raise ValueError(f"unknown source {source!r}")


def ingest_file(conn: sqlite3.Connection, source: str, path: str | Path, cfg: Config) -> BatchResult:
    path = Path(path)
    data = path.read_bytes()
    return ingest_bytes(conn, source, data, origin=path.as_posix(), cfg=cfg)


def ingest_bytes(conn: sqlite3.Connection, source: str, data: bytes, origin: str, cfg: Config) -> BatchResult:
    digest = file_sha256(data)
    batch_id = short_id("batch", source, digest)
    result = BatchResult(batch_id=batch_id, source=source, origin=origin)

    if conn.execute("SELECT 1 FROM ingest_batches WHERE batch_id = ?", (batch_id,)).fetchone():
        result.already_ingested = True
        return result

    parsed = parse_source(source, data, cfg)
    result.skipped = dict(parsed.skipped)

    with conn:
        # Batch row first so revisions can reference it; counts are filled in at the end.
        conn.execute(
            "INSERT INTO ingest_batches VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '{}')",
            (batch_id, source, origin, digest, ts_str(datetime.now(timezone.utc))),
        )
        for event in parsed.events:
            result.n_seen += 1
            existing = conn.execute(
                "SELECT content_hash FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            content_hash = event.content_hash
            if existing and existing["content_hash"] == content_hash:
                result.n_unchanged += 1
                continue

            c = event.canonical()
            values = (
                event.event_id, c["source"], c["source_event_id"], c["rail"], c["txn_key"], c["amount"],
                c["asset"], c["occurred_at"], c["account"], content_hash, batch_id,
            )
            if existing:
                result.n_revised += 1
                conn.execute(
                    "UPDATE events SET source=?, source_event_id=?, rail=?, txn_key=?, amount=?, asset=?,"
                    " occurred_at=?, account=?, content_hash=?, batch_id=? WHERE event_id=?",
                    values[1:] + (event.event_id,),
                )
            else:
                result.n_inserted += 1
                conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
            conn.execute(
                "INSERT OR IGNORE INTO event_revisions VALUES (?, ?, ?, ?)",
                (event.event_id, content_hash, batch_id, canonical_json(event.raw)),
            )

        for att in parsed.attestations:
            cur = conn.execute(
                "INSERT OR IGNORE INTO balance_attestations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (att.attestation_id, att.address, att.asset, att.contract, dec_str(att.balance),
                 ts_str(att.observed_at), batch_id),
            )
            result.n_attestations += cur.rowcount

        conn.execute(
            "UPDATE ingest_batches SET n_seen=?, n_inserted=?, n_unchanged=?, n_revised=?, n_skipped=?,"
            " skipped_json=? WHERE batch_id=?",
            (result.n_seen, result.n_inserted, result.n_unchanged, result.n_revised,
             sum(parsed.skipped.values()), json.dumps(result.skipped, sort_keys=True), batch_id),
        )
    return result
