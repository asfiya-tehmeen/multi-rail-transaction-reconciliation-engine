"""Canonical data model every source is normalized into."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .utils import canonical_json, dec_str, sha256_hex, short_id, ts_str

# Sources
PROCESSOR = "processor"
LEDGER = "ledger"
ONCHAIN = "onchain"

# External rails the ledger is reconciled against. Processor and on-chain events live on the rail
# named after their source; ledger entries declare which rail they claim to have settled on.
EXTERNAL_RAILS = (PROCESSOR, ONCHAIN)

# Match statuses
MATCHED = "MATCHED"
IN_TRANSIT = "IN_TRANSIT"
BREAK = "BREAK"

# Break types
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
TIMING_LAG = "TIMING_LAG"
UNMATCHED_LEDGER = "UNMATCHED_LEDGER"
ORPHAN_EXTERNAL = "ORPHAN_EXTERNAL"


@dataclass(frozen=True)
class Event:
    """One money movement as reported by one source.

    `amount` is signed from our perspective: positive = inflow to us, negative = outflow.
    """

    source: str
    source_event_id: str
    rail: str
    txn_key: str
    amount: Decimal
    asset: str
    occurred_at: datetime
    account: str = ""
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def event_id(self) -> str:
        # Identity depends only on where the event came from, never on its content, so a corrected
        # re-delivery of the same source record is recognised as a revision rather than a new event.
        return short_id("event", self.source, self.source_event_id)

    def canonical(self) -> dict:
        return {
            "source": self.source,
            "source_event_id": self.source_event_id,
            "rail": self.rail,
            "txn_key": self.txn_key,
            "amount": dec_str(self.amount),
            "asset": self.asset,
            "occurred_at": ts_str(self.occurred_at),
            "account": self.account,
        }

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.canonical()))


@dataclass(frozen=True)
class BalanceAttestation:
    """A balance reported directly by a chain indexer, used to check our reconstructed balance."""

    address: str
    asset: str
    contract: str
    balance: Decimal
    observed_at: datetime

    @property
    def attestation_id(self) -> str:
        return short_id("attestation", self.address, self.asset, ts_str(self.observed_at), dec_str(self.balance))


@dataclass
class ParseResult:
    events: list[Event] = field(default_factory=list)
    attestations: list[BalanceAttestation] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
