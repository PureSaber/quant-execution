# Share credits and odd-lot exits

The native ledger accepts the optional QDataKit `ShareEntitlement` on a corporate
action. The resolver uses exact integer credited quantity or an explicitly named
sensitivity rule. Unresolved rights fail before any mutation. Dividends use
record-date eligible quantity. Reference price is independent of share quantity.
Credited lots remain unavailable before their declared sellable date; ordinary
same-day purchases retain T+1. FixedPoint cash journals balance exactly.

The original event identity makes retries idempotent. Different content for an
applied identity is rejected. Capture/restore preserves the ledger's applied
events and lots; revisions require restoration before the event and a separately
identified replay. This does not rewrite the original historical account.

A-share buy requests require board lots. Sell requests may additionally dispose
of the entire odd remainder of the actual holding, subject to reservations and
T+1. After a legal order partially fills, its accepted remainder may continue;
a newly submitted buy request is validated again and cannot claim this exemption.

Restore the pre-upgrade software lock and immutable account checkpoint to roll
back. Keep all new event and ledger evidence separately. Old execution readers
cannot consume the new entitlement extension.
