from __future__ import annotations

import json
from decimal import Decimal

import pytest

from recon.config import TokenConfig
from recon.ingest.onchain import parse_etherscan_file

WALLET = "0x00000000000000000000000000000000000000aa"
OTHER = "0x00000000000000000000000000000000000000bb"
USDC = TokenConfig("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6)
FAKE = "0x000000000000000000000000000000000000dead"


def snapshot(rows, action="tokentx"):
    return json.dumps({"address": WALLET.upper().replace("0X", "0x"), "chain_id": 1, "action": action,
                       "fetched_at": "1790000000", "response": {"status": "1", "message": "OK", "result": rows}})


def row(frm, to, value, contract=USDC.contract, h="0xAB", log="1", symbol="USDC"):
    return {"hash": h, "from": frm, "to": to, "value": str(value), "contractAddress": contract, "logIndex": log,
            "tokenSymbol": symbol, "tokenDecimal": "6", "timeStamp": "1785000000", "blockNumber": "1"}


def test_signs_scaling_and_filters():
    rows = [
        row(OTHER, WALLET, 1_500_000, log="1"),          # +1.5 in
        row(WALLET, OTHER, 250_000, log="2"),            # -0.25 out
        row(OTHER, WALLET, 9_999_999, contract=FAKE),    # spoofed "USDC"
        row(WALLET, WALLET, 1, log="3"),                 # self transfer
        row(OTHER, OTHER, 1, log="4"),                   # unrelated
    ]
    res = parse_etherscan_file(snapshot(rows), {USDC.contract: USDC})
    assert [e.amount for e in res.events] == [Decimal("1.5"), Decimal("-0.25")]
    assert all(e.txn_key == "0xab" and e.asset == "USDC" for e in res.events)
    assert res.skipped == {"unlisted token contract": 1, "self transfer": 1, "not involving wallet": 1}


def test_duplicate_transfers_without_log_index_get_distinct_ids():
    rows = [row(OTHER, WALLET, 5, log=""), row(OTHER, WALLET, 5, log="")]
    ids = [e.source_event_id for e in parse_etherscan_file(snapshot(rows), {USDC.contract: USDC}).events]
    assert len(set(ids)) == 2


def test_error_response_raises():
    bad = json.dumps({"address": WALLET, "action": "tokentx", "fetched_at": "1",
                      "response": {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}})
    with pytest.raises(ValueError, match="Invalid API Key"):
        parse_etherscan_file(bad)


def test_balance_attestation():
    doc = json.dumps({"address": WALLET, "action": "tokenbalance", "fetched_at": "1790000000",
                      "token": {"symbol": "USDC", "contract": USDC.contract, "decimals": 6},
                      "response": {"status": "1", "message": "OK", "result": "123450000"}})
    [att] = parse_etherscan_file(doc).attestations
    assert att.balance == Decimal("123.45") and att.asset == "USDC"
