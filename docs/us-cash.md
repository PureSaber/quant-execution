# US cash research account

`quant_execution.us_cash.USCashAccount` uses `ExactAccountLedger` for fills, fees, corporate actions and valuation. Cash is booked on trade date; `buying_power` separately subtracts unsettled sale proceeds. This is a conservative cash account using settled funds, not a margin/PDT or live brokerage model.

Standard equity settlement uses T+2 before 2024-05-28 and T+1 from that date. Same-day resale is allowed for holdings bought using settled funds. Account units permit six decimal places for normalized-price and fractional-share research; no broker fillability claim is implied.

The daily model releases buying power on the settlement date, without intraday clearing or broker-specific holds. Account mutations and cash queries cannot precede the current ledger state; an identical already-applied trade remains idempotent. Instruments must be USD cash equities/ETFs with unit multiplier.

Explicit `us_equity`/`us_etf` product types select `USCashEquityRule`. Legacy A-share product types preserve their prior rules. Costs are supplied assumptions, not a fixed regulatory fee schedule.

`terminal_cash` corporate events retire all shares and remove book cost, posting a final known cash amount and realized P&L. They require a zero share ratio and nonnegative cash in the settlement currency. A zero recovery must be explicit upstream evidence, never a substitute for missing prices.

Fractional equity sells now quantize posted cash and removed book cost before computing P&L. This corrects a one-unit rounding imbalance caused by independently quantizing all three amounts.
