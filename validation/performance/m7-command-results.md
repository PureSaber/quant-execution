# M7 execution certification handoff

## Scope and acceptance

Scope is limited to `quant-execution`: bounded deterministic replay, immutable Arrow artifacts,
strict artifact verification and the execution performance gate. The path still executes strategy,
pre-trade/runtime risk, matching, fills, fees and exact fixed-point double-entry ledger posting.
It does not add live-order transport or change another repository.

Acceptance requires three independent10-million-event processes from one clean commit; every run
must reach at least50,000 events/second, remain below16GiB peak working set, produce identical
logical and physical hashes, retain its artifacts and pass strict post-run reload.

## Modified files

- Runtime:`src/quant_execution/artifacts.py`, `broker.py`, `engine.py`, `ledger.py`, `__init__.py`.
- Contract/version:`pyproject.toml` (`0.5.0`, replay-artifact manifest schema`1.0.0` and
  `quant-data-kit@v0.7.4`) plus the regenerated `requirements.lock`.
- Tests/CI:`tests/test_artifacts.py`, `tests/test_benchmark_replay.py`, `.github/workflows/ci.yml`.
- Benchmark/docs:`benchmarks/benchmark_replay.py`, `README.md`,
  `docs/performance-m7-streaming.md`, this handoff and the final JSON report.
- Hygiene:`.gitignore` excludes local virtual environments, coverage JSON and calibration profiles;
  it does not exclude the final certification report.

Rollback is a Git revert of the M7 candidate. The unchanged in-memory `replay` method remains the
runtime compatibility fallback. Historical tags and artifacts are not rewritten.

The first independent review was `CONDITIONAL` and identified two correctness gaps. Commit
`99eac282b1d31e33828a2d18e0efa42f983ef049` fixes both: a failed later replay now restores the
previous completed `stored_artifacts` handle, and a finalized streaming ledger keeps its public
transaction count and journal hash consistent with the stored ledger. Regression tests exercise
both states. The same review also required the memory claim to be narrowed from strict O(1) to the
measured10-million-event envelope; the runtime and documentation now use that precise scope.

The dependency recertification review then found that finalized ledger state could still mutate
behind its fixed journal hash. Commit `37260badebb1c637e7247d21dc0694e723f5d206` closes the lifecycle
for both ledger and broker:finalization or abort makes all mutation entry points fail closed until
`reset()`, and regression tests prove hashes, counts and snapshots cannot diverge after sealing.

## Tests and coverage

Locked local environment:

```text
python -m pip check
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=quant_execution --cov-branch \
  --cov-report=json:coverage.json -q
python -m coverage report --fail-under=80
python tools/check_branch_coverage.py coverage.json --threshold 90 \
  artifacts broker contracts schemas engine matching state_machine ledger rules
```

- `pip check`:PASS.
- Ruff format/check:PASS.
- Python3.12:201 passed; total coverage95.51%.
- Pure branch coverage:artifacts94.74%, broker95.56%, contracts91.88%, schemas94.74%,
  engine91.84%, matching93.25%, state_machine96.67%, ledger91.91%, rules92.38%.
- GitHub Actions run`33251586735`:Python3.10/3.11/3.12 all PASS.
- GitHub Actions run`33251588866`:Python3.10/3.11/3.12 all PASS.

## Formal performance evidence

Source commit:`37260badebb1c637e7247d21dc0694e723f5d206`.

```text
python benchmarks/benchmark_replay.py --workload matching \
  --matching-events 10000000 --repeat 3 --require-rate 50000 \
  --memory-limit-gib 16 --artifact-mode arrow \
  --artifact-root F:\puresaber-m7-artifacts\execution-v0.5.0-sealed-37260ba \
  --artifact-retention keep --artifact-batch-size 8192 \
  --artifact-queue-batches 2 \
  --output validation\performance\m7-execution-final-10m.json
```

| Run | Events/s | Peak working set | Strict reload | Dirty tree |
|---:|---:|---:|---|---|
| 1 | 64,746.49 | 2,239.19MiB | PASS | false |
| 2 | 62,394.71 | 2,242.02MiB | PASS | false |
| 3 | 64,627.91 | 2,236.11MiB | PASS | false |

- Rate gate:PASS for every run; median64,627.91 events/second.
- Memory gate:PASS; maximum2,242.02MiB.
- Each run:10,000,000 events,500,000 orders/fills,1,000,000 order events and
  1,000,001 balanced ledger transactions; explicit fill density5% (`order_stride=20`).
- Determinism:all logical hashes, every Arrow physical file hash and the manifest hash match across
  all three fresh processes.
- Artifact manifest SHA-256:`c87db59076f852248416df828ff43cbc7dc96cbf196547e97f6e70081c111f27`.
- Final report SHA-256:`6c74790bb3b9d5a20b95ba07989feb0eb6a265a211970577c6092559aaf47cb2`.
- Dependencies:Python3.12.5, PyArrow25.0.1, quant-data-kit distribution0.7.4.
- Machine:Windows11,16 logical CPUs; process peak working set includes Arrow and live replay state.
- Retained artifacts:three directories,1,501,955,792 bytes each (about4.20GiB total), under
  `F:\puresaber-m7-artifacts\execution-v0.5.0-sealed-37260ba`; no file was automatically
  removed.

The timed interval includes event materialization, matching, risk, fill, fee, exact ledger,
canonical serialization, Arrow initialization/write/seal, logical hashes and manifest close.
Process startup, one static fixture-template construction and strict post-run reload are excluded;
strict reload is independently required and passed in2.97–3.00seconds per run.

## Dense stress and remaining risks

The release workload is explicitly representative rather than adversarial:5% of market events
produce fills. The separate50%-fill dense stress workload remains below50,000 events/second in the
earlier committed evidence and is not relabelled or hidden. It exercises a different capacity
envelope and remains a future optimization target.

Remaining risks:

- formal performance evidence is Windows/Python3.12 host-specific; CI establishes functional and
  coverage compatibility on Python3.10/3.11/3.12 but does not rerun30million events;
- `replay_to_sink` requires deterministically sorted input and stores canonical JSON payloads inside
  Arrow IPC; downstream `standard/v2` publication remains a separate mapping step;
- physical Arrow hashes depend on the locked PyArrow serialization version and must be rebaselined,
  never silently accepted, after a dependency upgrade;
- the dense50%-fill stress gate is still a known capacity limitation;
- the slowest representative run passed by24.8%, so future releases must retain the per-run
  gate and15% regression comparison instead of relying on the median;
- Arrow buffers are bounded, while event/fill identity sets and broker lookup/idempotency state
  still scale with input/order count; the certified claim is controlled memory at10M, not O(1);
- this PR must remain unmerged until the independent M7 validator and cross-repository certification
  gate accept its committed evidence.

- PR:[#6](https://github.com/PureSaber/quant-execution/pull/6).
- Final JSON:`validation/performance/m7-execution-final-10m.json`.
