"""Internal ledger export (CSV) -> canonical events.

Expected columns: entry_id, posted_at, account, rail, external_ref, amount, currency  (memo optional)
  - `rail` says which external source should confirm the entry: `processor` or `onchain`.
    Entries on any other rail (e.g. `internal`) are stored but not reconciled.
  - `external_ref` is the shared transaction key: the processor reference, or the on-chain tx hash.
  - `amount` is signed: positive = inflow to us.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from ..models import LEDGER, ONCHAIN, Event, ParseResult
from ..utils import parse_ts

REQUIRED_COLUMNS = {"entry_id", "posted_at", "account", "rail", "external_ref", "amount", "currency"}


def parse_ledger_csv(text: str) -> ParseResult:
    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"ledger file missing columns: {sorted(missing)}")

    result = ParseResult()
    for line_no, row in enumerate(reader, start=2):
        rail = row["rail"].strip().lower()
        key = row["external_ref"].strip()
        if rail == ONCHAIN:
            key = key.lower()  # tx hashes are case-insensitive hex
        if not key:
            result.skipped["missing external_ref"] += 1
            continue
        try:
            amount = Decimal(row["amount"].strip())
        except InvalidOperation as exc:
            raise ValueError(f"line {line_no}: bad amount {row['amount']!r}") from exc

        result.events.append(
            Event(
                source=LEDGER,
                source_event_id=row["entry_id"].strip(),
                rail=rail,
                txn_key=key,
                amount=amount,
                asset=row["currency"].strip().upper(),
                occurred_at=parse_ts(row["posted_at"]),
                account=row["account"].strip(),
                raw=dict(row),
            )
        )
    return result
