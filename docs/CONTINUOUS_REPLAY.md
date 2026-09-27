# Continuous batch replay (15, 19)

`DeterministicRunEngine.replay_segments(segments, seed=...)` validates each nonempty segment
and strict chronological separation before account mutation, then replays the entire event
sequence **once**. The ledger, broker, unsettled amounts, orders, fees and strategy/risk state
therefore persist across segment boundaries. Events inside a segment use the normal replay
validation/order contract. Do not split events sharing the same `available_at` across segments.

This API describes one uninterrupted batch account. It does not serialize a checkpoint or
resume a live engine after process failure. Feed the complete frozen event stream on retry.

`tests/test_continuous_replay.py` compares A-share, futures and crypto golden fixtures
event-by-event against one-pass artifacts and terminal ledger snapshots. The cost test holds
orders/fills fixed and independently computes cash = initial cash − purchase notional − fees
using Decimal. A higher commission must reduce terminal NAV. This invariant does not apply
to different adaptive order paths. Existing corporate-action tests remain the split/dividend
regression basis; no external engine is claimed as an oracle.
