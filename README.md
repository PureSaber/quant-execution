# quant-execution

Deterministic execution, matching, risk hooks, and multi-currency ledger contracts for
PureSaber quantitative research, backtesting, and paper trading.

This project never sends live orders.

## v0.2.0 simulation runtime

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
current event, and stops on the first invalid strategy, match or ledger mutation.
For identical events, configuration, code and seed, order-event, fill, ledger and
result hashes are identical across repeated runs.

The three committed golden runs cover A-shares, domestic futures, and crypto spot plus
linear perpetual funding. They are regression fixtures, not performance marketing.

## Verification

```bash
python -m ruff check src tests
python -m ruff format --check src tests
python -m pytest --cov=quant_execution --cov-branch --cov-report=term-missing -q
```

The 50k-events/second replay objective remains an explicit performance gate. Current
measurements must be recorded as evidence; no test lowers or bypasses that target.
The current M3a measurements and the still-open throughput debt are recorded in
[`docs/performance-m3a.md`](docs/performance-m3a.md).
