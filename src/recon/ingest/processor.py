"""Payment processor export (CSV) -> canonical events.

Expected columns: id, reference, type, amount, currency, status, created_at
  - `reference` is the shared transaction key the ledger books against.
  - `type` is `charge` (inflow) or `refund` (outflow); amounts are gross, in major units.
  - Only `succeeded` rows move money; everything else is skipped and counted.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from ..models import PROCESSOR, Event, ParseResult
from ..utils import parse_ts

REQUIRED_COLUMNS = {"id", "reference", "type", "amount", "currency", "status", "created_at"}
SETTLED_STATUSES = {"succeeded"}


def parse_processor_csv(text: str) -> ParseResult:
    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"processor file missing columns: {sorted(missing)}")

    result = ParseResult()
    for line_no, row in enumerate(reader, start=2):
        status = row["status"].strip().lower()
        if status not in SETTLED_STATUSES:
            result.skipped[f"status={status}"] += 1
            continue

        kind = row["type"].strip().lower()
        try:
            amount = abs(Decimal(row["amount"].strip()))
        except InvalidOperation as exc:
            raise ValueError(f"line {line_no}: bad amount {row['amount']!r}") from exc
        if kind == "refund":
            amount = -amount
        elif kind != "charge":
            result.skipped[f"type={kind}"] += 1
            continue

        result.events.append(
            Event(
                source=PROCESSOR,
                source_event_id=row["id"].strip(),
                rail=PROCESSOR,
                txn_key=row["reference"].strip(),
                amount=amount,
                asset=row["currency"].strip().upper(),
                occurred_at=parse_ts(row["created_at"]),
                account="processor",
                raw=dict(row),
            )
        )
    return result
