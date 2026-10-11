# ResearchLifecycle

Additive offline stock/vanilla-option account, reusing existing FixedPoint, Posting,
LedgerTransaction and execution_payload contracts. No live transport and no schema
changes. Price/quantity precision is eight decimals; every transaction balances.

Supported events: deposit, locate, fill, mark, accrue, dividend_ex, dividend_pay,
split, margin and exercise. Fills can reverse signed positions. A valid dated
borrow locate is mandatory for short stock. Apply financing before each state
change inside an interval; quant-options lifecycle does this automatically.
Unchanged event IDs are idempotent; conflicts reject; failures roll back atomically.

Cash/physical exercise settles full positions at expiry using the exact expiry
fixing. Early exercise requires American contracts. Partial early exercise
invalidates the remaining option mark, since exercise intrinsic is not its fair
value. Pure splits require explicit value-preserving option adjustments. Margin
segregation never becomes a PnL expense; shortfalls are reported for funding or
liquidation, without invented forced fills.

`snapshot()` reports missing/stale marks and excludes no held position silently.
`journal()` uses the existing public exact ledger schema, with quantity deltas.
`tests/test_research_lifecycle.py` validates ledger reconciliation, reversals,
assignment, rollback, funding, dividends, splits and margin.
