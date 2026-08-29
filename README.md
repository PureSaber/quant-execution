# quant-execution

Deterministic execution, matching, risk hooks, and multi-currency ledger contracts for
PureSaber quantitative research, backtesting, and paper trading.

This project never sends live orders.

## Install

```bash
python -m venv .venv
python -m pip install --requirement requirements.lock
python -m pip check
python -m pip install --no-deps --no-build-isolation --editable .
python -m pip check
```

## v0.5.0 M7 bounded replay artifacts

`DeterministicRunEngine.replay_to_sink` provides a bounded-memory Arrow path for long event
replays while preserving the public in-memory `replay` reference. A completed artifact directory
is immutable: its canonical manifest records logical stream hashes, physical file hashes, byte
sizes, counts and run-result metadata. `load_stored_artifacts` verifies the manifest, every Arrow
schema, contiguous sequence, byte size, physical hash and logical hash before exposing any facts.
Publication is atomic and refuses to overwrite an existing manifest; failed runs retain
`FAILED.json` and never receive a complete manifest.

The M7 certification workload is explicit rather than inferred: one order/fill is produced every
20 market events (5% fill density), and every timed run includes event materialization, strategy,
risk, matching, fee, exact double-entry ledger, Arrow writing, logical hashing and immutable
manifest close. The 50%-fill workload remains a separately reported stress workload. Both paths
are research/backtest/paper-trading only and contain no live-order transport.

## v0.4.1 M6 dependency governance

The package declares the `execution` layer through `[tool.quant-workspace]`, publishes the ten
`puresaber.execution.*` schemas at version `1.1.0`, and identifies `requirements.lock` as its
externally resolved dependency set. The lock covers runtime dependencies, the `dev` extra, and
editable-build requirements for Python3.10-3.12. Every registry package is fixed to one exact
version. The `dev` extra names Python3.10's conditional compatibility dependencies explicitly so
a lock compiled on Python3.12 remains complete for the whole matrix. The internal package is also
fixed by its released annotated tag:
`quant-data-kit@v0.6.1`, from `https://github.com/PureSaber/quant-data-kit.git`, resolving to
commit `edf1351690dc60691cc6330390adcdbf8bc79c5f`.

Regenerate the lock only after reviewing dependency changes in `pyproject.toml`:

```bash
python -m pip install "pip-tools==7.6.1"
pip-compile --extra dev --build-deps-for editable --allow-unsafe --strip-extras \
  --resolver backtracking --index-url https://pypi.org/simple \
  --constraint requirements-constraints.txt \
  --output-file requirements.lock pyproject.toml
```

Validate a rebuilt lock in clean Python3.10,3.11, and3.12 environments. In each environment,
install `requirements.lock` first, run `pip check`, then install the repository editable with
`--no-deps --no-build-isolation` and run `pip check` again. The project declaration and lock are
one review unit; do not hand-edit an isolated transitive pin or install CI extras outside the lock.
`requirements-constraints.txt` contains only cross-interpreter resolver limits and is not a second
installation input.

Rollback is a Git revert of the governance change, restoring `pyproject.toml`,
`src/quant_execution/__init__.py`, `requirements-constraints.txt`, `requirements.lock`, CI, and
this documentation together. Existing release tags and historical lock hashes are immutable:
never move, overwrite, or rebuild an old tag to repair dependency resolution.

## v0.4.0 simulation runtime and portfolio-risk context

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
- immutable `PositionRiskSnapshot`, `PortfolioRiskSnapshot` and `RiskCheckContext`
  views derived from the exact ledger using causally available marks and FX;
- ordered, fail-closed `PortfolioRiskPolicy` composition inside `RuleBookRiskGate`,
  including projected base-currency order notional and deterministic runtime checks.

This package is strictly for research, backtesting and paper trading. It has no
broker credentials, network order adapter or live-order transmission path. L2 queue
position is a deterministic research approximation and is not a nanosecond exchange
queue claim.

Key assumptions live in `InstrumentSpec.metadata`, including lot/minimum sizes,
commission or maker/taker rates, price bands, margin rates and close-today fees.
Unknown or incomplete rule configuration is rejected rather than guessed.
Risk policies are read-only plugins: built-in asset/cash/position/margin checks run
first, policies cannot transmit orders, and missing mark/FX context or policy errors
produce explicit rejection codes instead of bypassing risk.

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

For long replays, `DeterministicRunEngine.replay_to_sink` accepts an already deterministically
sorted event iterator and writes orders, order events, fills, fees, settlements, ledger
transactions and risk events into bounded Arrow IPC batches. The returned `RunResult`, frozen
logical hashes, exact ledger state and event ordering remain byte-identical to `replay` while
`engine.stored_artifacts` replaces the in-memory `RunArtifacts` graph. Existing consumers may
continue to call `replay`; migration consumers should read `StoredRunArtifacts` iterators and
must retain the immutable source-market-data snapshot separately.

```python
sink = ArrowReplayArtifactSink("run/artifacts", batch_size=8192, queue_batches=2)
result = engine.replay_to_sink(sorted_events, seed=42, sink=sink)
for payload in engine.stored_artifacts.iter_json("fills"):
    consume(payload)

verified = load_stored_artifacts("run/artifacts")
assert verified.manifest_sha256 == engine.stored_artifacts.manifest_sha256
```

## Verification

```bash
python -m ruff check src tests benchmarks tools
python -m ruff format --check src tests benchmarks tools
python -m pytest --cov=quant_execution --cov-branch --cov-report=term-missing \
  --cov-report=json:coverage.json -q
python -m coverage report --fail-under=80
python tools/check_branch_coverage.py coverage.json --threshold 90 \
  broker contracts schemas engine matching state_machine ledger rules artifacts
python benchmarks/benchmark_replay.py --workload matching --matching-events 10000000 \
  --repeat 3 --require-rate 50000 --memory-limit-gib 16 --artifact-mode arrow \
  --artifact-root /dedicated/m7-artifacts --artifact-retention keep \
  --output validation/performance/m7-execution-final-10m.json
```

The 50k-events/second objective is an explicit local performance gate and requires all three
independent10-million-event processes—not merely their median—to pass. Artifacts are retained,
strictly reloaded and hash-verified after each timed run. The exact 50%-fill stress workload and
the earlier materialized-path profile remain disclosed separately in
[`docs/performance-m3a.md`](docs/performance-m3a.md).
The bounded-memory contract, differential matrix, benchmark definition and current gate evidence
are documented in
[`docs/performance-m7-streaming.md`](docs/performance-m7-streaming.md).
