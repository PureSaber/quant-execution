# quant-execution

Deterministic execution, matching, risk hooks, and multi-currency ledger contracts for
PureSaber quantitative research, backtesting, and paper trading.

This project never sends live orders.

## v0.3.0 simulation runtime

The frozen M1 contracts remain compatible and are now backed by:

- idempotent `OrderIntent`, immutable `Order`, and complete `OrderEvent` facts;
- a fail-closed order state machine covering acceptance, partial fills, completion,
  cancellation, rejection, and expiry;
- independent `Fill`, signed `Fee`, `Funding`, and `Settlement` facts;
- exact fixed-point, per-currency double-entry `LedgerTransaction` records;
- JSON Schema and Arrow schemas with committed golden records;
- the public `Strategy`, `BrokerSimulator`, `RiskGate`, `MatchingModel`,
  `AccountLedger`, and `RunEngine` protocols.
- `DeterministicRunEngine` with stable event ordering, deterministic IDs,
  idempotent lifecycle operations and fail-closed replay;
- bar, Trade/BBO and L2 matching with latency, visible-liquidity limits,
  partial fills, IOC/FOK/DAY/GTC and conservative stop-limit semantics;
- an exact multi-currency journal whose cash, positions, cost basis, PnL,
  margin and NAV are derived from balanced `LedgerTransaction` records;
- `InstrumentSpec`-driven A-share/ETF, domestic futures, crypto spot and
  USDT linear perpetual rules, fees and pre/in-run risk checks.
- replay-time opening entries dated at the first causally available market event,
  with explicit derivative `StatusEvent(status="daily_settlement")` conversion into
  auditable `Settlement` facts exposed by `RunArtifacts.settlements`.

This package is strictly for research, backtesting and paper trading. It has no
broker credentials, network order adapter or live-order transmission path. L2 queue
position is a deterministic research approximation and is not a nanosecond exchange
queue claim.

Key assumptions live in `InstrumentSpec.metadata`, including lot/minimum sizes,
commission or maker/taker rates, price bands, margin rates and close-today fees.
Unknown or incomplete rule configuration is rejected rather than guessed.

## Deterministic replay

`DeterministicRunEngine.replay` sorts by availability/event time and stable stream
identity, rejects duplicate event IDs, matches only orders that existed before the
current event, and stops on the first invalid strategy, match or ledger mutation. An
intent emitted inside a strategy callback must use that event's exact `available_at`
as `created_at`; backdating and future-dating are both rejected.
For identical events, configuration, code and seed, order-event, fill, ledger and
result hashes are identical across repeated runs.

Daily mark settlement uses an explicitly versioned schema. Serialization defaults to
`1.1.0`, where `settlement_price` is optional. Passing `version="1.0.0"` emits the
unchanged legacy payload and rejects a non-null `settlement_price`. JSON readers accept
legacy payloads under the new optional-field schema; Arrow validation auto-detects the
exact `1.0.0` or `1.1.0` physical schema unless a version is explicitly requested.
When `settlement_type` is `daily_mark`, the cash amount must equal mark-to-market PnL
at that price and the cost basis is reset to the same price, preventing unrealized PnL
from being counted twice. FX snapshots are UTC-only, conflict-safe and versioned into
the ledger journal hash.

The three committed golden runs cover A-shares, domestic futures, and crypto spot plus
linear perpetual funding. They are regression fixtures, not performance marketing.

## Verification

```bash
python -m ruff check src tests benchmarks
python -m ruff format --check src tests benchmarks
python -m pytest --cov=quant_execution --cov-branch --cov-report=term-missing -q
python benchmarks/benchmark_replay.py --workload all --repeat 3 --require-rate 50000
```

The 50k-events/second replay objective is an explicit local performance gate. The
no-order workload passes; the exact 50%-fill workload remains below the gate while
retaining all 1,000 fills, 2,001 balanced transactions, risk checks and final hashes.
The measured shortfall and required follow-up architecture work are disclosed in
[`docs/performance-m3a.md`](docs/performance-m3a.md).
