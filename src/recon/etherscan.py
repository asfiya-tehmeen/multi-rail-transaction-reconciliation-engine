"""Minimal Etherscan V2 client (stdlib only) that writes immutable snapshot files for ingestion."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import TokenConfig
from .utils import ts_str

BASE_URL = "https://api.etherscan.io/v2/api"
PAGE_SIZE = 1000
RESULT_WINDOW = 10_000  # Etherscan refuses page * offset beyond this; we slide the start block instead.
MIN_INTERVAL_S = 0.25  # stays under the free tier's 5 calls/second
MAX_RETRIES = 5


class EtherscanError(RuntimeError):
    pass


class EtherscanClient:
    def __init__(self, api_key: str, chain_id: int = 1, base_url: str = BASE_URL):
        if not api_key:
            raise EtherscanError("ETHERSCAN_API_KEY is not set (see .env.example)")
        self.api_key = api_key
        self.chain_id = chain_id
        self.base_url = base_url
        self._last_call = 0.0

    def _get(self, params: dict) -> dict:
        query = {"chainid": self.chain_id, **params, "apikey": self.api_key}
        url = f"{self.base_url}?{urllib.parse.urlencode(query)}"
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == MAX_RETRIES - 1:
                    raise EtherscanError(f"request failed: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if payload.get("status") == "1":
                return payload
            message = f"{payload.get('message')}: {payload.get('result')}"
            if payload.get("message") == "No transactions found":
                return {**payload, "result": []}
            if "rate limit" in message.lower() and attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise EtherscanError(message)
        raise EtherscanError("exhausted retries")

    def _paged(self, params: dict, start_block: int, end_block: int) -> list[dict]:
        rows: list[dict] = []
        seen: set[tuple] = set()
        start = start_block
        while True:
            page = 1
            while True:
                batch = self._get(
                    {**params, "startblock": start, "endblock": end_block, "page": page,
                     "offset": PAGE_SIZE, "sort": "asc"}
                )["result"]
                for r in batch:
                    key = (r["hash"], r.get("logIndex"), r["from"], r.get("to"), r["value"], r.get("contractAddress"))
                    if key not in seen:
                        seen.add(key)
                        rows.append(r)
                if len(batch) < PAGE_SIZE:
                    return rows
                if page * PAGE_SIZE >= RESULT_WINDOW:
                    # Restart the window at the last block seen; overlap is removed by `seen`.
                    start = int(batch[-1]["blockNumber"])
                    break
                page += 1

    def token_transfers(self, address: str, contract: str, start_block: int = 0, end_block: int = 99_999_999):
        params = {"module": "account", "action": "tokentx", "address": address, "contractaddress": contract}
        return {"status": "1", "message": "OK", "result": self._paged(params, start_block, end_block)}

    def native_transactions(self, address: str, start_block: int = 0, end_block: int = 99_999_999):
        params = {"module": "account", "action": "txlist", "address": address}
        return {"status": "1", "message": "OK", "result": self._paged(params, start_block, end_block)}

    def token_balance(self, address: str, contract: str):
        return self._get({"module": "account", "action": "tokenbalance", "address": address,
                          "contractaddress": contract, "tag": "latest"})

    def native_balance(self, address: str):
        return self._get({"module": "account", "action": "balance", "address": address, "tag": "latest"})


def write_snapshot(out_dir: Path, address: str, chain_id: int, action: str, response: dict,
                   token: TokenConfig | None = None) -> Path:
    fetched_at = datetime.now(timezone.utc)
    doc = {
        "address": address.lower(),
        "chain_id": chain_id,
        "action": action,
        "fetched_at": ts_str(fetched_at),
        "response": response,
    }
    label = "native"
    if token:
        doc["token"] = {"symbol": token.symbol, "contract": token.contract, "decimals": token.decimals}
        label = token.symbol.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{address.lower()[:10]}_{label}_{action}_{fetched_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return path


def fetch_wallet(client: EtherscanClient, address: str, tokens: tuple[TokenConfig, ...], out_dir: Path,
                 include_native: bool = False, start_block: int = 0) -> list[Path]:
    """Snapshot transfers and current balances for every configured token (and optionally ETH)."""
    paths = []
    for token in tokens:
        paths.append(write_snapshot(out_dir, address, client.chain_id, "tokentx",
                                    client.token_transfers(address, token.contract, start_block), token))
        paths.append(write_snapshot(out_dir, address, client.chain_id, "tokenbalance",
                                    client.token_balance(address, token.contract), token))
    if include_native:
        paths.append(write_snapshot(out_dir, address, client.chain_id, "txlist",
                                    client.native_transactions(address, start_block)))
        paths.append(write_snapshot(out_dir, address, client.chain_id, "balance", client.native_balance(address)))
    return paths
