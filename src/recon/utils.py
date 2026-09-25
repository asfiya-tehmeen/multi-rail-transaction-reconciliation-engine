"""Small helpers shared across the engine: hashing, canonical JSON, time and decimal formatting."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

ID_LENGTH = 24


def sha256_hex(*parts: str) -> str:
    """Hash an ordered sequence of strings. A separator byte keeps ("ab", "c") distinct from ("a", "bc")."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def short_id(*parts: str) -> str:
    return sha256_hex(*parts)[:ID_LENGTH]


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def dec_str(value: Decimal) -> str:
    """Canonical decimal string: '10.50' and '10.5' both become '10.5', never scientific notation."""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def parse_ts(value) -> datetime:
    """Parse ISO-8601 (naive values are treated as UTC) or unix epoch seconds into an aware UTC datetime."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def ts_str(dt: datetime) -> str:
    """Fixed-width UTC timestamp, so lexical order in SQLite equals chronological order."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
