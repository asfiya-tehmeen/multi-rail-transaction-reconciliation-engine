# Multi-Rail Transaction Reconciliation Engine

Matches money movements across three independent sources — a **payment processor**, the **internal ledger**,
and the **blockchain** — then classifies every disagreement and reports a rolling **reserves-coverage**
figure. Every number it reports can be traced back to the transactions behind it, and every run is safe
to repeat.

```
 processor export (CSV) ─┐
                         │   normalize        match on           classify         report + lineage
 ledger export (CSV) ────┼──► canonical ──►  transaction  ──►  MATCHED / BREAK ──► results, breaks,
                         │     events        keys, per rail     / IN_TRANSIT       coverage, trace
 Etherscan snapshot ─────┘   (SQLite)                                │
   (wallet activity)                                                 └──► reserves coverage:
                                                                          verified on-chain ÷ claimed ledger
```

## Why this exists

A controller looking at "reserves: 4.76M USDC" or "6 amount breaks" needs to answer two questions:
*where does this number come from?* and *will I get the same number if I run it again?* This engine is
built around those two questions:

| Guarantee | How |
|---|---|
| **Traceable** | Each match result stores the event ids it was built from; each event stores its source record id, the file it came from, and every raw version ever received. `recon trace <id>` walks that chain and **re-adds the amounts** to prove the stored figure. |
| **Idempotent ingestion** | A batch id is `hash(source, file bytes)`, so re-submitting a file is a no-op. An event id is `hash(source, source record id)`, so a re-delivered record is recognised; changed content is stored as a revision, never a duplicate. |
| **Idempotent matching** | A run id is `hash(every event id + content hash, config, as_of)`. The same event stream always gives the same run id, and re-running it does nothing. Any new or corrected event produces a *new* run; old runs are never overwritten. `recon match --verify` recomputes a run and confirms the results are byte-identical. |
| **Exact money** | Amounts are `Decimal` end to end and stored as text. Floats never touch a balance. |

## Break taxonomy

Ledger entries are matched to external events on a shared **transaction key**, per rail: the processor
`reference` for the processor rail, the **tx hash** for the on-chain rail. Events that share a key are
netted, so a multi-leg transaction still matches.

| Status / break | Meaning |
|---|---|
| `MATCHED` | Amounts agree within tolerance and were booked within the allowed lag. |
| `BREAK · AMOUNT_MISMATCH` | Both sides exist but amounts (or assets) disagree, e.g. fee booked net instead of gross, or a typo. |
| `BREAK · TIMING_LAG` | Amounts agree but the ledger booked the entry too far from when it actually settled. |
| `BREAK · UNMATCHED_LEDGER` | The ledger claims a movement the external rail never shows. Most dangerous for reserves: it overstates them. |
| `BREAK · ORPHAN_EXTERNAL` | Money moved on the rail with no ledger entry. |
| `IN_TRANSIT` | One side is missing but still within the rail's lag window, so this is not a break yet. |

Tolerances are set per rail (defaults: processor `0.01` / 72h, on-chain `0.000001` / 2h). See `recon.example.toml`.

## Reserves coverage

For each reserve asset and each UTC day:

- **claimed** = running ledger balance of entries booked on the on-chain rail
- **verified** = running balance rebuilt from transfers actually observed on-chain
- **coverage** = verified ÷ claimed, plus the **rolling minimum** and **average** over a trailing window (7 days by default)

Use the rolling minimum for reporting, because it is the conservative figure. The rebuilt on-chain balance is also
checked against the balance Etherscan reports for the wallet (`gap` in the report). A non-zero gap means
the transfer history is incomplete.

## Quick start

Requires **Python 3.11+**. There are no runtime dependencies (stdlib only); the tests use `pytest`.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

recon demo        # generates sample data with known breaks and runs the whole pipeline offline
pytest            # 27 tests
```

`recon demo` produces (abridged):

```
rail       outcome           asset  count  ledger          external        |delta|
onchain    AMOUNT_MISMATCH   USDC   3      275166.183152   273797.197167   3152.263073
onchain    MATCHED           USDC   80     4147477.485004  4147477.485004  0
onchain    ORPHAN_EXTERNAL   USDC   2      0               87664.319942    0
onchain    TIMING_LAG        USDC   3      252426.491482   252426.491482   0
onchain    UNMATCHED_LEDGER  USDC   2      274819.252992   0               0
processor  AMOUNT_MISMATCH   USD    6      1758.26         1812.62         54.36
processor  MATCHED           USD    216    47437.8         47437.8         0
...
Reserves coverage (verified on-chain / claimed ledger)
asset  date        claimed        verified        coverage  7d min    7d avg
USDC   2026-08-31  4949889.41263  4761365.493595  0.961914  0.960705  0.961712

Indexer balance check USDC: indexer=4761365.493595 reconstructed=4761365.493595 gap=0
```

The demo then ingests the same files again (every batch is a no-op), re-runs matching (same run id,
reused), and recomputes it with `--verify` (identical). The test suite checks that the engine finds
**exactly** the breaks the generator injected, no more and no fewer.

Trace any number:

```bash
recon --db data/demo/recon.db trace <result_id>     # a break -> its ledger + external events
recon --db data/demo/recon.db trace <snapshot_id>   # a coverage figure -> every event behind it
recon --db data/demo/recon.db trace <event_id>      # an event -> raw source record, revisions, where it's used
```

```
result in run 093e0fdcc7a52be1b1f6754b
  break_type         AMOUNT_MISMATCH
  ledger_amount      249524.471552
  external_amount    248283.056271
  ledger events (1):   183607c0...  ledger   le_c00060   249524.471552  ledger_entries.csv
  external events (1): faa2af47...  onchain  0x3793a5... 248283.056271  etherscan_usdc_tokentx.json
  arithmetic check: OK - stored figures equal the sum of the events above
```

## Using real data

```bash
cp recon.example.toml recon.toml     # set [wallet].address and tokens
cp .env.example .env                 # set ETHERSCAN_API_KEY

recon ingest processor exports/processor_2026-09.csv
recon ingest ledger    exports/ledger_2026-09.csv
recon fetch-onchain                  # snapshots wallet activity from Etherscan, then ingests it
recon match
recon report                         # -> reports/<run_id>/{results,breaks,coverage}.csv + summary.json
```

On-chain data is never ingested straight from the API. `fetch-onchain` writes each response to an immutable
snapshot file under `data/raw/etherscan/` first, and ingestion reads that file. You can re-run from the
snapshot without the network, and the file shows exactly what the chain reported at fetch time.

### Input formats

**Processor CSV** — `id, reference, type, amount, currency, status, created_at`
`type` is `charge` or `refund`. Amounts are gross, in major units. Only `succeeded` rows are counted;
the others are skipped and reported in the batch summary.

**Ledger CSV** — `entry_id, posted_at, account, rail, external_ref, amount, currency[, memo]`
`rail` is `processor` or `onchain` (anything else, e.g. `internal`, is stored but not reconciled).
`external_ref` is the processor reference or the tx hash. `amount` is signed: positive means inflow.

**Etherscan snapshot JSON** — written by `fetch-onchain`: the raw API response in an envelope with the wallet
address, action (`tokentx`, `txlist`, `tokenbalance`, `balance`), token metadata, and fetch time.

Timestamps may be ISO-8601 (naive values are treated as UTC) or unix seconds.

## CLI

| Command | What it does |
|---|---|
| `recon ingest {processor,ledger,onchain} FILE...` | Normalize and store. Safe to repeat. |
| `recon fetch-onchain [--address] [--start-block] [--native]` | Snapshot wallet token transfers and balances from Etherscan V2, then ingest them. |
| `recon match [--as-of TS] [--verify]` | Reconcile. `as_of` defaults to the latest event time, not the clock, so re-runs are repeatable. |
| `recon report [--run ID] [--out DIR]` | Write `results.csv`, `breaks.csv`, `coverage.csv`, `summary.json`. |
| `recon trace ID [--run ID] [--json]` | Show the source records behind a result, a coverage snapshot, or an event. |
| `recon demo [--seed N]` | Generate sample data and run everything offline (uses `data/demo/recon.db`). |

Global options: `--db PATH` (default `data/recon.db`) and `--config PATH` (default `./recon.toml` if present).

## Project layout

```
src/recon/
  models.py        canonical Event, statuses, break types
  config.py        tolerances, reserves settings, wallet/tokens; config fingerprint
  db.py            SQLite schema: events + revisions, runs, results, coverage, lineage tables
  ingest/          processor / ledger / on-chain normalizers + idempotent batch writer
  etherscan.py     stdlib Etherscan V2 client (pagination past the 10k window, rate limiting, retries)
  matching.py      pure, order-independent matching + break classification
  reserves.py      daily and rolling reserves coverage with per-day lineage
  engine.py        run fingerprinting, idempotent persistence, --verify
  reporting.py     summaries, CSV/JSON reports, trace with arithmetic checks
  sample_data.py   deterministic generator with injected breaks
  cli.py           `recon` command
tests/             matching rules, idempotency, lineage, coverage, Etherscan parsing
```

## Known limitations / roadmap

- On-chain rail: ERC-20 transfers and native ETH value transfers. Gas fees are not yet booked as outflows,
  so if you enable `--native`, expect ETH coverage to drift by the gas spent.
- Coverage assumes the full transfer history since the wallet was created. An opening-balance entry is not
  supported yet. Use `--start-block 0`, and treat the indexer `gap` as the check.
- Matching is exact on key. Fuzzy matching (amount + time window) for sources without shared keys is on the roadmap.
- Processor FX and fee accounting: amounts are compared gross in one currency.
- SQLite fits single-controller workloads. The schema maps directly to Postgres for larger volumes.
