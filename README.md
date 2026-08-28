# quant-execution

Deterministic execution, matching, risk hooks, and multi-currency ledger contracts for
PureSaber quantitative research, backtesting, and paper trading.

This project never sends live orders.

## M1 contract preview

The first contract release freezes:

- idempotent `OrderIntent`, immutable `Order`, and complete `OrderEvent` facts;
- a fail-closed order state machine covering acceptance, partial fills, completion,
  cancellation, rejection, and expiry;
- independent `Fill`, signed `Fee`, `Funding`, and `Settlement` facts;
- exact fixed-point, per-currency double-entry `LedgerTransaction` records;
- JSON Schema and Arrow schemas with committed golden records;
- the public `Strategy`, `BrokerSimulator`, `RiskGate`, `MatchingModel`,
  `AccountLedger`, and `RunEngine` protocols.

Implementation of the event loop, matching models, asset plugins, and persistent ledger
belongs to M3. The M1 package contains contracts only.

## Verification

```bash
python -m ruff check src tests
python -m pytest -q
```
