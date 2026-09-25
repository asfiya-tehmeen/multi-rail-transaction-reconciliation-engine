"""Engine configuration: matching tolerances, reserves settings, and the wallet/tokens to pull on-chain."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .models import ONCHAIN, PROCESSOR
from .utils import canonical_json, dec_str, sha256_hex

DEFAULT_CONFIG_PATH = Path("recon.toml")


@dataclass(frozen=True)
class TokenConfig:
    symbol: str
    contract: str
    decimals: int


@dataclass(frozen=True)
class RailTolerance:
    amount_tolerance: Decimal
    max_lag: timedelta


@dataclass(frozen=True)
class Config:
    wallet_address: str | None = None
    chain_id: int = 1
    tokens: tuple[TokenConfig, ...] = (
        TokenConfig("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
    )
    tolerances: dict[str, RailTolerance] = field(
        default_factory=lambda: {
            PROCESSOR: RailTolerance(Decimal("0.01"), timedelta(hours=72)),
            ONCHAIN: RailTolerance(Decimal("0.000001"), timedelta(hours=2)),
        }
    )
    reserve_assets: tuple[str, ...] = ()
    rolling_window_days: int = 7

    def tokens_by_contract(self) -> dict[str, TokenConfig]:
        return {t.contract.lower(): t for t in self.tokens}

    def fingerprint(self) -> str:
        """Hash of every setting that can change a reconciliation result. Part of the run id."""
        return sha256_hex(
            canonical_json(
                {
                    "tolerances": {
                        rail: {
                            "amount_tolerance": dec_str(t.amount_tolerance),
                            "max_lag_seconds": int(t.max_lag.total_seconds()),
                        }
                        for rail, t in self.tolerances.items()
                    },
                    "reserve_assets": list(self.reserve_assets),
                    "rolling_window_days": self.rolling_window_days,
                }
            )
        )


def load_config(path: str | Path | None = None) -> Config:
    """Load TOML config over the defaults. With no path, ./recon.toml is used if it exists."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if path:
            raise FileNotFoundError(f"config file not found: {cfg_path}")
        return Config()

    with cfg_path.open("rb") as fh:
        doc = tomllib.load(fh)

    base = Config()
    wallet = doc.get("wallet", {})
    tokens = tuple(
        TokenConfig(t["symbol"].upper(), t["contract"].lower(), int(t["decimals"]))
        for t in wallet.get("tokens", [])
    ) or base.tokens

    tolerances = dict(base.tolerances)
    for rail, section in doc.get("matching", {}).items():
        current = tolerances.get(rail, RailTolerance(Decimal("0"), timedelta(0)))
        tolerances[rail] = RailTolerance(
            Decimal(str(section.get("amount_tolerance", current.amount_tolerance))),
            timedelta(hours=float(section["max_lag_hours"])) if "max_lag_hours" in section else current.max_lag,
        )

    reserves = doc.get("reserves", {})
    return Config(
        wallet_address=(wallet.get("address") or None) and wallet["address"].lower(),
        chain_id=int(wallet.get("chain_id", base.chain_id)),
        tokens=tokens,
        tolerances=tolerances,
        reserve_assets=tuple(a.upper() for a in reserves.get("assets", [])),
        rolling_window_days=int(reserves.get("rolling_window_days", base.rolling_window_days)),
    )


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, never overrides variables already set in the environment."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
