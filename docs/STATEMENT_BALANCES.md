# Confirmed statement balances

`ExactAccountLedger.book_opening_position` imports a cash-asset position at the account opening timestamp with its quantity, average cost and acquisition date. It posts opening equity and position cost/quantity; it does not fabricate a fill. A positive cost, valid quantity step and acquisition date are required. Imports after account activity fail.

`book_external_cash` imports a base-currency deposit or withdrawal, with a unique transfer ID and UTC timestamp. It posts `assets:cash` against `equity:external_flows`, never income. Negative cash, time reversal, over-precision and changed-content retries fail. Identical transfer retries do not add cash again.

Both methods emit balanced existing `settlement` journal records with explicit `opening-position:`/`external:` reference IDs. Existing event schemas stay unchanged. They are intended for bounded statement reconciliation, not streaming replay; manual imports reject an active artifact stream. No credential or order transmission capability is introduced.
