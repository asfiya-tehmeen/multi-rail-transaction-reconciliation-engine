from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from recon import sample_data
from recon.config import Config
from recon.db import connect
from recon.ingest import ingest_file
from recon.models import LEDGER, ONCHAIN, PROCESSOR, Event

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def ev(source, sid, key, amount, at=T0, rail=None, asset=None):
    rail = rail or (source if source != LEDGER else PROCESSOR)
    asset = asset or ("USDC" if rail == ONCHAIN else "USD")
    return Event(source=source, source_event_id=sid, rail=rail, txn_key=key, amount=Decimal(str(amount)),
                 asset=asset, occurred_at=at)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def sample(tmp_path):
    return sample_data.generate(tmp_path / "sample", seed=7)


@pytest.fixture
def loaded_db(tmp_path, sample, cfg):
    conn = connect(tmp_path / "recon.db")
    for source in (PROCESSOR, LEDGER, ONCHAIN):
        for f in sample.files[source]:
            ingest_file(conn, source, f, cfg)
    return conn
