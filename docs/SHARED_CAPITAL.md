# Shared capital continuous research

`quant_execution.continuous.SharedCapitalAccount` is an additive, offline API.
It owns one authoritative `ExactAccountLedger`. Sleeve attribution books receive
disjoint cash allocations whose sum is the total account's opening cash. Fills,
fees, FX conversions and outside flows post to both the total ledger and the
owning attribution book; transfers post only to the two attribution books with
offsetting clearing entries. Attribution books never create additional NAV or
buying power. Per-currency cash, instrument quantities, NAV and every balanced
transaction are reconciled after every accepted event.

Construct the account using QDK `InstrumentSpec` objects, `owners` mapping each
instrument to exactly one sleeve, `allocations` mapping sleeves to currency cash,
`base_currency`, UTC `opened_at`, and explicit `fx_rates` to base. Only cash
equities/ETFs (long only) and linear futures are supported. Futures require explicit
`initial_margin_rate` and `maintenance_margin_rate`, each positive and at most one.
One instrument cannot be netted across sleeves. No broker, market impact, order
matching, automatic liquidation, inverse derivative, option, tax or corporate
action inference is provided. The caller declares actual research fills and fees.

Every event has `event_id`, UTC `at`, UTC `available_at <= at`, nonnegative integer
`sequence` and `kind`. Unknown fields are rejected. Money/price/quantity values
are exact decimal strings (or integers), with at most eight decimal places.

| kind | Additional fields |
| --- | --- |
| mark | instrument_id, price |
| fx_mark | currency, rate (base currency units per currency unit) |
| fill | sleeve, instrument_id, signed quantity, price, nonnegative fee, settles_at; optional order_id |
| settle | none; releases holds matured at this instant |
| transfer | source sleeve, target sleeve, currency, positive amount |
| external_cash | sleeve, currency, signed nonzero amount |
| fx_convert | sleeve, source_currency, target_currency, positive source_amount, target_amount |
| charge | sleeve, instrument_id, nonnegative amount in settlement currency |
| variation_margin | sleeve, instrument_id, price, signed amount (checked by QExec against futures PnL) |

```python
account.apply({
    "event_id": "buy-1", "kind": "fill", "sleeve": "trend",
    "instrument_id": "ETF-A", "quantity": "5", "price": "100", "fee": "1",
    "at": "2026-01-02T10:00:00Z", "available_at": "2026-01-02T10:00:00Z",
    "sequence": 3, "settles_at": "2026-01-05T10:00:00Z",
})
```

`replay(events)` sorts a batch by `(at, sequence)`. `apply` requires strict order
across chunks. Exact same-ID retries are no-ops; conflicting IDs or tied keys
fail. Each event is atomic, including failed ledger postings. A failed batch
leaves its previously accepted events, so use checkpoint restore for a clean retry.
`checkpoint()` binds the accepted journal and the full opening configuration
(instrument terms, owners, allocations, currency, time, FX). `restore` on a fresh
account verifies that identity before applying any event; failed restore is atomic.

Purchased cash units cannot be sold before settlement. Sale proceeds count in NAV
but cannot be spent, converted or transferred until their declared settlement.
Buys and fees debit cash immediately. Due holds release before each event and are
listed in history; no holiday calendar is silently inferred. Reinvestment uses
the resulting settled balances. Futures positive unrealized PnL is unavailable
until variation settlement; negative unrealized PnL reduces collateral immediately.
No cross-currency collateral credit is allowed; use explicit funded FX conversion.
The difference between actual conversion amounts and marked FX changes economic
PnL, never external flows. Internal transfers are PnL-neutral.

`initial_margin_shortfall` uses initial margin and blocks increased risk.
`margin_call` uses maintenance margin. Marks, charges and variation settlement are
economic facts: losses are retained and calls exposed. Explicit risk reductions,
positive deposits and internal transfers may partly cure a deficit if no affected
budget deficit worsens; there is no automatic trade or account recovery.

History records external-flow-adjusted event returns; cumulative return is their
product. A nonpositive prior NAV makes the subsequent return unavailable. Economic
PnL remains final NAV minus initial NAV minus contemporaneously FX-valued external
flows. Cash/units reconcile exactly; summed base NAV may differ by at most one
1e-8 rounding quantum per sleeve. Tests use separate Decimal scalar expectations
for cash, FX, futures and PnL, plus shared-cash competition and chunk identity.

The portfolio `continuous-study` CLI supplies a frozen notional-to-lot policy,
next-observation declared executions, immutable revision pairs and the common
measurement declaration. Existing QExec APIs and schema versions are unchanged.
Rollback is a revert of this additive module and its consumers; preserve outputs.
