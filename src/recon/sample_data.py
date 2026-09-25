"""Deterministic synthetic data for all three sources, with known, injected breaks.

Output files use the same formats as real inputs (processor CSV, ledger CSV, Etherscan snapshot JSON), so
the demo exercises the real parsers. The same seed always produces byte-identical files.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .utils import sha256_hex, ts_str

WALLET = "0x5afe00000000000000000000000000000000c0de"
USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_DECIMALS = 6
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
DAYS = 30
BASE_BLOCK = 20_500_000


@dataclass
class SampleManifest:
    """What was injected, so tests and the demo can check the engine found exactly these breaks."""

    processor: dict = field(default_factory=dict)
    onchain: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)


def _rand_time(rng: random.Random, start: datetime, days: int) -> datetime:
    return start + timedelta(seconds=rng.randrange(days * 86400))


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _split(rng: random.Random, items: list, sizes: dict[str, int]) -> dict[str, set]:
    pool = items[:]
    rng.shuffle(pool)
    out, i = {}, 0
    for name, n in sizes.items():
        out[name] = set(pool[i:i + n])
        i += n
    return out


def generate(out_dir: Path, seed: int = 7) -> SampleManifest:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = SampleManifest()
    fetched_at = START + timedelta(days=DAYS + 1)  # snapshot taken after every generated event
    ledger_rows = []

    # ---------------- processor rail (USD card/ACH payments) ----------------
    n_proc = 240
    proc_idx = list(range(n_proc))
    inj = _split(rng, proc_idx, {"amount_mismatch": 6, "timing_lag": 5, "unmatched_ledger": 4, "orphan": 4,
                                 "failed": 5, "refund": 12})
    manifest.processor = {k: sorted(f"ord_{i:05d}" for i in v) for k, v in inj.items()}

    proc_rows = []
    for i in proc_idx:
        ref = f"ord_{i:05d}"
        created = _rand_time(rng, START, DAYS - 4)  # leave room so injected lags stay in-window
        amount = Decimal(rng.randint(500, 50_000)) / 100
        kind = "refund" if i in inj["refund"] else "charge"
        status = "failed" if i in inj["failed"] else "succeeded"
        fee = (amount * Decimal("0.029") + Decimal("0.30")).quantize(Decimal("0.01"))

        if i not in inj["unmatched_ledger"]:
            proc_rows.append({"id": f"ch_{sha256_hex('ch', str(seed), ref)[:14]}", "reference": ref, "type": kind,
                              "amount": f"{amount:.2f}", "fee": f"{fee:.2f}", "currency": "usd",
                              "status": status, "created_at": ts_str(created)})
        if i in inj["orphan"] or i in inj["failed"]:
            continue

        signed = -amount if kind == "refund" else amount
        posted = created + timedelta(minutes=rng.randint(5, 20 * 60))
        if i in inj["amount_mismatch"]:
            signed = signed - fee  # booked net of processor fee instead of gross
        if i in inj["timing_lag"]:
            posted = created + timedelta(days=rng.randint(4, 8))
        if i in inj["unmatched_ledger"]:
            posted = created
        ledger_rows.append({"entry_id": f"le_p{i:05d}", "posted_at": ts_str(posted),
                            "account": "assets:processor_clearing", "rail": "processor", "external_ref": ref,
                            "amount": f"{signed:.2f}", "currency": "USD", "memo": f"{kind} {ref}"})

    proc_rows.sort(key=lambda r: r["created_at"])
    proc_path = out_dir / "processor_transactions.csv"
    with proc_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "reference", "type", "amount", "fee", "currency", "status",
                                           "created_at"])
        w.writeheader()
        w.writerows(proc_rows)

    # ---------------- on-chain rail (USDC reserve wallet) ----------------
    n_chain = 90
    chain_idx = list(range(n_chain))
    cinj = _split(rng, chain_idx, {"amount_mismatch": 3, "timing_lag": 3, "unmatched_ledger": 2, "orphan": 2})

    times = sorted(_rand_time(rng, START, DAYS - 2) for _ in chain_idx)
    balance = Decimal(0)
    transfers = []
    for i, ts in zip(chain_idx, times):
        amount = Decimal(rng.randint(1_000, 250_000)) + Decimal(rng.randint(0, 999_999)) / Decimal(10**6)
        inflow = i < 5 or rng.random() < 0.7 or amount > balance
        if i in cinj["unmatched_ledger"]:
            inflow = True  # a claimed deposit that never landed: overstates reserves
        if inflow:
            balance += amount if i not in cinj["unmatched_ledger"] else 0
        else:
            balance -= amount
        tx_hash = "0x" + sha256_hex("tx", str(seed), str(i))
        counterparty = "0x" + _hex(rng, 40)
        transfers.append((i, ts, tx_hash, counterparty, amount, inflow))

    manifest.onchain = {k: sorted(transfers[i][2] for i in v) for k, v in cinj.items()}

    token_rows = []
    for i, ts, tx_hash, counterparty, amount, inflow in transfers:
        signed = amount if inflow else -amount
        if i not in cinj["unmatched_ledger"]:
            base_units = int(amount * 10**USDC_DECIMALS)
            block = BASE_BLOCK + int((ts - START).total_seconds() // 12)
            token_rows.append({
                "blockNumber": str(block), "timeStamp": str(int(ts.timestamp())), "hash": tx_hash,
                "nonce": str(i), "blockHash": "0x" + sha256_hex("block", str(block)),
                "from": counterparty if inflow else WALLET, "to": WALLET if inflow else counterparty,
                "contractAddress": USDC_CONTRACT, "value": str(base_units), "tokenName": "USDC",
                "tokenSymbol": "USDC", "tokenDecimal": str(USDC_DECIMALS), "transactionIndex": str(i % 150),
                "logIndex": str(i % 300), "gas": "65000", "gasPrice": "12000000000", "gasUsed": "52000",
                "confirmations": "1000",
            })
        if i in cinj["orphan"]:
            continue
        posted = ts + timedelta(minutes=rng.randint(1, 45))
        if i in cinj["amount_mismatch"]:
            signed = signed * Decimal("1.005")  # fat-fingered booking, 0.5% off
        if i in cinj["timing_lag"]:
            posted = ts + timedelta(hours=rng.randint(6, 30))
        ledger_rows.append({"entry_id": f"le_c{i:05d}", "posted_at": ts_str(posted),
                            "account": "assets:reserves:usdc", "rail": "onchain", "external_ref": tx_hash,
                            "amount": format(signed.quantize(Decimal("0.000001")), "f"), "currency": "USDC",
                            "memo": ("deposit" if inflow else "withdrawal")})

    # A few internal entries: stored and traceable, but not reconciled against any rail.
    for n in range(3):
        ledger_rows.append({"entry_id": f"le_i{n:05d}", "posted_at": ts_str(START + timedelta(days=7 * n + 1)),
                            "account": "equity:transfers", "rail": "internal", "external_ref": f"jrnl_{n}",
                            "amount": "0", "currency": "USD", "memo": "month-end reclass"})

    ledger_rows.sort(key=lambda r: (r["posted_at"], r["entry_id"]))
    ledger_path = out_dir / "ledger_entries.csv"
    with ledger_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["entry_id", "posted_at", "account", "rail", "external_ref", "amount",
                                           "currency", "memo"])
        w.writeheader()
        w.writerows(ledger_rows)

    token = {"symbol": "USDC", "contract": USDC_CONTRACT, "decimals": USDC_DECIMALS}
    tx_path = out_dir / "etherscan_usdc_tokentx.json"
    tx_path.write_text(json.dumps({
        "address": WALLET, "chain_id": 1, "action": "tokentx", "token": token, "fetched_at": ts_str(fetched_at),
        "response": {"status": "1", "message": "OK", "result": token_rows},
    }, indent=1, sort_keys=True), encoding="utf-8")

    onchain_balance = sum((Decimal(r["value"]) for r in token_rows if r["to"] == WALLET), Decimal(0)) - sum(
        (Decimal(r["value"]) for r in token_rows if r["from"] == WALLET), Decimal(0))
    bal_path = out_dir / "etherscan_usdc_tokenbalance.json"
    bal_path.write_text(json.dumps({
        "address": WALLET, "chain_id": 1, "action": "tokenbalance", "token": token, "fetched_at": ts_str(fetched_at),
        "response": {"status": "1", "message": "OK", "result": str(int(onchain_balance))},
    }, indent=1, sort_keys=True), encoding="utf-8")

    manifest.files = {"processor": [proc_path], "ledger": [ledger_path], "onchain": [tx_path, bal_path]}
    return manifest
