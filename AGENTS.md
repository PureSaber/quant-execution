# quant-execution

Deterministic execution and ledger layer for research, backtesting, and paper trading.

## Invariants

- Never add live broker order transmission or credentials.
- Preserve deterministic replay and idempotency.
- Use `FixedPoint` for price, quantity, cash, fees, funding, and ledger postings.
- Every ledger transaction balances exactly per currency.
- Reject illegal order transitions and timezone-naive timestamps.
- Keep JSON and Arrow schemas synchronized with committed golden records.

## Commands

```bash
python -m ruff check src tests
python -m pytest -q
```
