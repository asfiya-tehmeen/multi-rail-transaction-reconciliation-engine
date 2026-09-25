"""Etherscan snapshot files -> canonical events / balance attestations.

On-chain data is never ingested straight from the API. `recon fetch-onchain` first writes each API
response to an immutable snapshot file, and ingestion reads that file. Re-running ingestion therefore
replays exactly the same bytes, which is what makes on-chain ingestion idempotent and auditable.

Snapshot envelope:
    {"address": "0x..", "chain_id": 1, "action": "tokentx" | "txlist" | "tokenbalance" | "balance",
     "token": {"symbol": "USDC", "contract": "0x..", "decimals": 6},   # token actions only
     "fetched_at": "2026-09-01T00:00:00Z", "response": {<raw Etherscan JSON>}}
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal

from ..config import TokenConfig
from ..models import ONCHAIN, BalanceAttestation, Event, ParseResult
from ..utils import parse_ts

NATIVE = "native"
TRANSFER_ACTIONS = {"tokentx", "txlist"}
BALANCE_ACTIONS = {"tokenbalance", "balance"}


def _token_meta(doc: dict, contract: str | None, allowed: dict[str, TokenConfig] | None, row: dict | None):
    if allowed and contract in allowed:
        t = allowed[contract]
        return t.symbol, t.decimals
    if doc.get("token"):
        return doc["token"]["symbol"].upper(), int(doc["token"]["decimals"])
    if row is not None:
        return row["tokenSymbol"].upper(), int(row["tokenDecimal"])
    raise ValueError("token metadata missing from snapshot")


def parse_etherscan_file(text: str, allowed_tokens: dict[str, TokenConfig] | None = None) -> ParseResult:
    doc = json.loads(text)
    for key in ("address", "action", "response", "fetched_at"):
        if key not in doc:
            raise ValueError(f"etherscan snapshot missing {key!r}")

    address = doc["address"].lower()
    action = doc["action"]
    response = doc["response"]
    result = ParseResult()

    if action in BALANCE_ACTIONS:
        if response.get("status") != "1":
            raise ValueError(f"etherscan error in snapshot: {response.get('message')}: {response.get('result')}")
        if action == "balance":
            asset, decimals, contract = "ETH", 18, NATIVE
        else:
            contract = doc["token"]["contract"].lower()
            asset, decimals = _token_meta(doc, contract, allowed_tokens, None)
        result.attestations.append(
            BalanceAttestation(
                address=address,
                asset=asset,
                contract=contract,
                balance=Decimal(int(response["result"])).scaleb(-decimals),
                observed_at=parse_ts(doc["fetched_at"]),
            )
        )
        return result

    if action not in TRANSFER_ACTIONS:
        raise ValueError(f"unsupported etherscan action {action!r}")

    rows = response.get("result")
    if not isinstance(rows, list):
        raise ValueError(f"etherscan error in snapshot: {response.get('message')}: {rows}")

    occurrences: Counter = Counter()
    for row in rows:
        frm, to = row["from"].lower(), (row.get("to") or "").lower()

        if action == "txlist":
            if row.get("isError", "0") != "0" or row.get("txreceipt_status", "1") == "0":
                result.skipped["failed tx"] += 1
                continue
            asset, decimals, contract = "ETH", 18, NATIVE
        else:
            contract = row["contractAddress"].lower()
            # Only trust contracts we configured: scam tokens routinely reuse symbols like "USDC".
            if allowed_tokens and contract not in allowed_tokens:
                result.skipped["unlisted token contract"] += 1
                continue
            asset, decimals = _token_meta(doc, contract, allowed_tokens, row)

        if frm == address and to == address:
            result.skipped["self transfer"] += 1
            continue
        if to == address:
            sign = 1
        elif frm == address:
            sign = -1
        else:
            result.skipped["not involving wallet"] += 1
            continue

        value = Decimal(int(row["value"])).scaleb(-decimals)
        if value == 0:
            result.skipped["zero value"] += 1
            continue

        tx_hash = row["hash"].lower()
        if row.get("logIndex") not in (None, ""):
            base_id = f"{tx_hash}:{row['logIndex']}"
        else:
            base_id = f"{tx_hash}:{contract}:{frm}:{to}:{row['value']}"
        occurrences[base_id] += 1
        source_event_id = base_id if occurrences[base_id] == 1 else f"{base_id}#{occurrences[base_id]}"

        result.events.append(
            Event(
                source=ONCHAIN,
                source_event_id=source_event_id,
                rail=ONCHAIN,
                txn_key=tx_hash,
                amount=sign * value,
                asset=asset,
                occurred_at=parse_ts(row["timeStamp"]),
                account=address,
                raw=row,
            )
        )
    return result
