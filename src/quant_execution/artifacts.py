"""Bounded-memory Arrow artifact sink for deterministic replays."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
from pyarrow import ipc
from quant_data_kit import (
    AssetClass,
    CorporateActionEvent,
    FixedPoint,
    InstrumentSpec,
    MarginMode,
    MarkPriceEvent,
)
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial import DividendLifecycle, PitFxRate

from quant_execution._json import fixed_token, parse_utc_timestamp, string_token, utc_token
from quant_execution.contracts import (
    Fee,
    Fill,
    Funding,
    LedgerEventType,
    LedgerTransaction,
    LiquidityRole,
    Order,
    OrderEvent,
    Posting,
    Settlement,
    Side,
)
from quant_execution.dividends import (
    DividendExecutionMode,
    DividendExecutionPhase,
    DividendExecutionRecord,
    DividendExecutionRequest,
    DividendValuationRecord,
    EntitlementEvidenceVerifier,
    FxValuationMode,
    PitFxObservationRecord,
    canonical_bytes,
    dividend_record_bytes,
    dividend_record_from_dict,
)

_LEGACY_STREAMS: Final = (
    "orders",
    "order_events",
    "fills",
    "fees",
    "settlements",
    "ledger_transactions",
    "risk_events",
)
_STREAMS_BY_MANIFEST_VERSION: Final = {
    "1.0.0": _LEGACY_STREAMS,
    "1.1.0": (*_LEGACY_STREAMS, "dividend_records"),
}
_SCHEMA = pa.schema(
    [
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("payload", pa.large_binary(), nullable=False),
    ]
)
_STOP = object()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def order_event_bytes(event: OrderEvent) -> bytes:
    return (
        "{"
        f'"event_id":{string_token(event.event_id)},'
        f'"event_time":{utc_token(event.event_time)},'
        f'"fill_quantity":{fixed_token(event.fill_quantity)},'
        f'"from_status":{string_token(event.from_status.value)},'
        f'"order_id":{string_token(event.order_id)},'
        f'"reason":{string_token(event.reason)},'
        f'"sequence":{event.sequence},'
        f'"to_status":{string_token(event.to_status.value)}'
        "}"
    ).encode()


def fill_bytes(fill: Fill) -> bytes:
    venue_trade_id = "null" if fill.venue_trade_id is None else string_token(fill.venue_trade_id)
    return (
        "{"
        f'"account_id":{string_token(fill.account_id)},'
        f'"event_time":{utc_token(fill.event_time)},'
        f'"fill_id":{string_token(fill.fill_id)},'
        f'"instrument_id":{string_token(fill.instrument_id)},'
        f'"liquidity_role":{string_token(fill.liquidity_role.value)},'
        f'"order_id":{string_token(fill.order_id)},'
        f'"price":{fixed_token(fill.price)},'
        f'"quantity":{fixed_token(fill.quantity)},'
        f'"side":{string_token(fill.side.value)},'
        f'"strategy_id":{string_token(fill.strategy_id)},'
        f'"venue_trade_id":{venue_trade_id}'
        "}"
    ).encode()


def order_bytes(order: Order) -> bytes:
    from quant_execution.broker import _intent_bytes

    return (
        "{"
        f'"filled_quantity":{fixed_token(order.filled_quantity)},'
        f'"intent":{_intent_bytes(order.intent).decode()},'
        f'"order_id":{string_token(order.order_id)},'
        f'"status":{string_token(order.status.value)},'
        f'"version":{order.version}'
        "}"
    ).encode()


def fee_bytes(fee: Fee) -> bytes:
    return (
        "{"
        f'"account_id":{string_token(fee.account_id)},'
        f'"amount":{fixed_token(fee.amount)},'
        f'"currency":{string_token(fee.currency)},'
        f'"event_time":{utc_token(fee.event_time)},'
        f'"fee_id":{string_token(fee.fee_id)},'
        f'"fee_type":{string_token(fee.fee_type)},'
        f'"fill_id":{string_token(fee.fill_id)}'
        "}"
    ).encode()


def settlement_bytes(settlement: Settlement) -> bytes:
    return (
        "{"
        f'"account_id":{string_token(settlement.account_id)},'
        f'"amount":{fixed_token(settlement.amount)},'
        f'"currency":{string_token(settlement.currency)},'
        f'"event_time":{utc_token(settlement.event_time)},'
        f'"instrument_id":{string_token(settlement.instrument_id)},'
        f'"settlement_id":{string_token(settlement.settlement_id)},'
        f'"settlement_price":{fixed_token(settlement.settlement_price)},'
        f'"settlement_type":{string_token(settlement.settlement_type)}'
        "}"
    ).encode()


def ledger_transaction_bytes(transaction: LedgerTransaction) -> bytes:
    postings: list[str] = []
    for posting in transaction.postings:
        instrument_id = (
            "null" if posting.instrument_id is None else string_token(posting.instrument_id)
        )
        postings.append(
            "{"
            f'"amount":{fixed_token(posting.amount)},'
            f'"currency":{string_token(posting.currency)},'
            f'"instrument_id":{instrument_id},'
            f'"ledger_account":{string_token(posting.ledger_account)},'
            f'"quantity_delta":{fixed_token(posting.quantity_delta)}'
            "}"
        )
    return (
        "{"
        f'"event_time":{utc_token(transaction.event_time)},'
        f'"event_type":{string_token(transaction.event_type.value)},'
        f'"idempotency_key":{string_token(transaction.idempotency_key)},'
        f'"postings":[{",".join(postings)}],'
        f'"reference_id":{string_token(transaction.reference_id)},'
        f'"transaction_id":{string_token(transaction.transaction_id)}'
        "}"
    ).encode()


class _SequenceDigest:
    """Incrementally hash a canonical JSON array without retaining its facts."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._count = 0
        self._closed = False

    def append(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("artifact digest is already closed")
        if self._count:
            self._digest.update(b",")
        self._digest.update(payload)
        self._count += 1

    def close(self) -> str:
        if not self._closed:
            self._digest.update(b"]")
            self._closed = True
        return self._digest.hexdigest()


@dataclass(frozen=True, slots=True)
class StoredRunArtifacts:
    """Immutable handle to a completed on-disk replay artifact set."""

    root: Path
    manifest_path: Path
    counts: Mapping[str, int]
    logical_sha256: Mapping[str, str]
    files: Mapping[str, Mapping[str, object]]
    manifest_sha256: str
    schema_version: str
    run_metadata: Mapping[str, object]

    def iter_payload_bytes(self, stream: str) -> Iterator[bytes]:
        if stream not in self.counts:
            raise ValidationError(f"unknown artifact stream: {stream}")
        path = self.root / f"{stream}.arrow"
        if not path.exists():
            return
        with pa.memory_map(str(path), "r") as source:
            reader = ipc.open_stream(source)
            for batch in reader:
                for payload in batch.column("payload").to_pylist():
                    yield bytes(payload)

    def iter_json(self, stream: str) -> Iterator[object]:
        for payload in self.iter_payload_bytes(stream):
            yield json.loads(payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_manifest_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_no_clobber(path: Path, body: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_hash(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_manifest_bytes(unsigned)).hexdigest()


def _load_stored_artifacts(
    root: str | Path,
    *,
    manifest_name: str,
    reject_failed: bool,
) -> StoredRunArtifacts:
    resolved = Path(root).resolve()
    if reject_failed and (resolved / "FAILED.json").exists():
        raise ValidationError("artifact directory is marked FAILED")
    manifest_path = resolved / manifest_name
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"artifact manifest is unreadable: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("artifact manifest root must be an object")
    expected_fields = {
        "artifact_format",
        "complete",
        "counts",
        "files",
        "logical_sha256",
        "manifest_sha256",
        "run_metadata",
        "schema_version",
    }
    if set(payload) != expected_fields:
        raise ValidationError("artifact manifest fields changed")
    if not isinstance(payload.get("run_metadata"), dict) or not payload["run_metadata"]:
        raise ValidationError("artifact manifest contains no run metadata")
    schema_version = payload.get("schema_version")
    if schema_version not in _STREAMS_BY_MANIFEST_VERSION:
        raise ValidationError("artifact manifest schema version is unsupported")
    streams = _STREAMS_BY_MANIFEST_VERSION[schema_version]
    if payload.get("artifact_format") != "puresaber.arrow-canonical-json.v1":
        raise ValidationError("artifact format is unsupported")
    if payload.get("complete") is not True:
        raise ValidationError("artifact run is not complete")
    if raw != _canonical_manifest_bytes(payload):
        raise ValidationError("artifact manifest bytes are not canonical")
    manifest_sha256 = payload.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or manifest_sha256 != _manifest_hash(payload):
        raise ValidationError("artifact manifest hash mismatch")
    counts = payload.get("counts")
    logical = payload.get("logical_sha256")
    files = payload.get("files")
    if not isinstance(counts, dict) or set(counts) != set(streams):
        raise ValidationError("artifact counts changed shape")
    if not isinstance(logical, dict) or set(logical) != set(streams):
        raise ValidationError("artifact logical hashes changed shape")
    if not isinstance(files, dict):
        raise ValidationError("artifact files must be an object")
    verified_counts: dict[str, int] = {}
    verified_logical: dict[str, str] = {}
    verified_files: dict[str, dict[str, object]] = {}
    for stream in streams:
        count = counts[stream]
        logical_sha256 = logical[stream]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError(f"artifact count is invalid: {stream}")
        if not isinstance(logical_sha256, str) or not _SHA256.fullmatch(logical_sha256):
            raise ValidationError(f"artifact logical hash is invalid: {stream}")
        metadata = files.get(stream)
        if count == 0:
            if metadata is not None:
                raise ValidationError(f"empty artifact stream unexpectedly has a file: {stream}")
            digest = _SequenceDigest()
            if digest.close() != logical_sha256:
                raise ValidationError(f"empty artifact logical hash mismatch: {stream}")
            verified_counts[stream] = 0
            verified_logical[stream] = logical_sha256
            continue
        if not isinstance(metadata, dict) or set(metadata) != {"bytes", "path", "sha256"}:
            raise ValidationError(f"artifact file metadata changed shape: {stream}")
        relative = metadata["path"]
        expected_relative = f"{stream}.arrow"
        if relative != expected_relative:
            raise ValidationError(f"artifact file path is invalid: {stream}")
        path = (resolved / expected_relative).resolve()
        try:
            path.relative_to(resolved)
        except ValueError as exc:
            raise ValidationError(f"artifact file escapes its run root: {stream}") from exc
        expected_bytes = metadata["bytes"]
        expected_sha256 = metadata["sha256"]
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes <= 0
            or not isinstance(expected_sha256, str)
            or not _SHA256.fullmatch(expected_sha256)
        ):
            raise ValidationError(f"artifact file metadata is invalid: {stream}")
        if not path.is_file() or path.stat().st_size != expected_bytes:
            raise ValidationError(f"artifact file is missing or changed size: {stream}")
        if _sha256_file(path) != expected_sha256:
            raise ValidationError(f"artifact file content hash mismatch: {stream}")
        digest = _SequenceDigest()
        observed = 0
        try:
            with pa.memory_map(str(path), "r") as source:
                reader = ipc.open_stream(source)
                if reader.schema != _SCHEMA:
                    raise ValidationError(f"artifact Arrow schema changed: {stream}")
                for batch in reader:
                    sequences = batch.column("sequence").to_pylist()
                    expected_sequences = list(range(observed, observed + batch.num_rows))
                    if sequences != expected_sequences:
                        raise ValidationError(f"artifact sequence is not contiguous: {stream}")
                    for item in batch.column("payload").to_pylist():
                        digest.append(bytes(item))
                    observed += batch.num_rows
        except (OSError, pa.ArrowException) as exc:
            raise ValidationError(f"artifact Arrow stream is unreadable: {stream}") from exc
        if observed != count or digest.close() != logical_sha256:
            raise ValidationError(f"artifact logical content mismatch: {stream}")
        verified_counts[stream] = count
        verified_logical[stream] = logical_sha256
        verified_files[stream] = dict(metadata)
    if set(files) != set(verified_files):
        raise ValidationError("artifact files contain unknown streams")
    return StoredRunArtifacts(
        root=resolved,
        manifest_path=manifest_path,
        counts=verified_counts,
        logical_sha256=verified_logical,
        files=verified_files,
        manifest_sha256=manifest_sha256,
        schema_version=schema_version,
        run_metadata=dict(payload["run_metadata"]),
    )


def load_stored_artifacts(root: str | Path) -> StoredRunArtifacts:
    """Strictly verify a completed artifact directory before exposing its facts."""

    return _load_stored_artifacts(
        root,
        manifest_name="manifest.json",
        reject_failed=True,
    )


class ArrowReplayArtifactSink:
    """Write canonical replay facts to bounded Arrow record batches.

    The producer only retains at most ``batch_size`` payload references per stream.
    A single background writer owns every Arrow stream, so replay and native Arrow
    I/O can overlap without exposing partially written artifacts as complete runs.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        batch_size: int = 65_536,
        queue_batches: int = 8,
        manifest_schema_version: str = "1.0.0",
    ) -> None:
        if isinstance(batch_size, bool) or batch_size <= 0:
            raise ValidationError("batch_size must be a positive integer")
        if isinstance(queue_batches, bool) or queue_batches <= 0:
            raise ValidationError("queue_batches must be a positive integer")
        if manifest_schema_version not in _STREAMS_BY_MANIFEST_VERSION:
            raise ValidationError("artifact manifest schema version is unsupported")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self._manifest_schema_version = manifest_schema_version
        self._streams = _STREAMS_BY_MANIFEST_VERSION[manifest_schema_version]
        self._batch_size = batch_size
        self._buffers: dict[str, list[bytes]] = {name: [] for name in self._streams}
        self._counts = {name: 0 for name in self._streams}
        self._digests = {name: _SequenceDigest() for name in self._streams}
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_batches)
        self._failure: BaseException | None = None
        self._closed = False
        self._sealed = False
        self._poisoned = False
        self._staged: list[tuple[str, bytes]] | None = None
        self._writers: dict[str, ipc.RecordBatchStreamWriter] = {}
        self._files: dict[str, pa.NativeFile] = {}
        self._thread = threading.Thread(
            target=self._write_loop,
            name="quant-execution-artifact-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def counts(self) -> Mapping[str, int]:
        return dict(self._counts)

    def append(self, stream: str, payload: bytes) -> None:
        if self._closed or self._sealed:
            raise RuntimeError("artifact sink no longer accepts records")
        try:
            if stream not in self._buffers:
                raise ValidationError(f"unknown artifact stream: {stream}")
            if not isinstance(payload, bytes):
                raise ValidationError("artifact payload must be canonical bytes")
            if self._staged is not None:
                self._staged.append((stream, payload))
                return
            self._append_committed(stream, payload)
        except Exception:
            if self._manifest_schema_version == "1.1.0":
                self._poisoned = True
                self.abort()
            raise

    def begin(self) -> None:
        if self._staged is not None:
            raise RuntimeError("nested artifact transactions are not supported")
        if self._closed or self._sealed:
            raise RuntimeError("artifact sink no longer accepts records")
        self._staged = []

    def commit(self) -> None:
        staged = self._staged
        if staged is None:
            raise RuntimeError("no artifact transaction is active")
        self._staged = None
        try:
            for stream, payload in staged:
                self._append_committed(stream, payload)
        except Exception:
            if self._manifest_schema_version == "1.1.0":
                self._poisoned = True
                self.abort()
            raise

    def rollback(self) -> None:
        if self._staged is None:
            raise RuntimeError("no artifact transaction is active")
        self._staged = None

    def _append_committed(self, stream: str, payload: bytes) -> None:
        self._raise_writer_failure()
        self._digests[stream].append(payload)
        self._counts[stream] += 1
        buffer = self._buffers[stream]
        buffer.append(payload)
        if len(buffer) >= self._batch_size:
            self._enqueue((stream, self._counts[stream] - len(buffer), buffer))
            self._buffers[stream] = []

    def logical_sha256(self, stream: str) -> str:
        if stream not in self._digests:
            raise ValidationError(f"unknown artifact stream: {stream}")
        return self._digests[stream].close()

    def close(
        self,
        manifest: Mapping[str, object],
        *,
        candidate_validator: Callable[[StoredRunArtifacts], None] | None = None,
    ) -> StoredRunArtifacts:
        if self._closed:
            raise RuntimeError("artifact sink is already closed")
        try:
            self.seal()
            logical = {name: digest.close() for name, digest in self._digests.items()}
            files = {
                name: {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
                for name in self._streams
                for path in (self.root / f"{name}.arrow",)
                if path.is_file()
            }
            completed = {
                "schema_version": self._manifest_schema_version,
                "artifact_format": "puresaber.arrow-canonical-json.v1",
                "counts": dict(self._counts),
                "logical_sha256": logical,
                "files": files,
                "complete": True,
                "run_metadata": dict(manifest),
            }
            completed["manifest_sha256"] = _manifest_hash(completed)
            if self._manifest_schema_version == "1.1.0":
                if candidate_validator is None:
                    raise ValidationError("manifest 1.1 requires candidate replay validation")
                candidate_path = self.root / "manifest.candidate.json"
                _write_no_clobber(candidate_path, _canonical_manifest_bytes(completed))
                candidate = _load_stored_artifacts(
                    self.root,
                    manifest_name=candidate_path.name,
                    reject_failed=False,
                )
                candidate_validator(candidate)
            manifest_path = self.root / "manifest.json"
            _write_no_clobber(manifest_path, _canonical_manifest_bytes(completed))
            stored = load_stored_artifacts(self.root)
            (self.root / "manifest.candidate.json").unlink(missing_ok=True)
            self._closed = True
            return stored
        except Exception:
            self._poisoned = True
            self.abort()
            raise

    def seal(self) -> None:
        """Flush and close Arrow writers while leaving manifest finalization pending."""

        if self._sealed:
            return
        if self._staged is not None:
            raise RuntimeError("cannot seal an active artifact transaction")
        for stream, buffer in self._buffers.items():
            if buffer:
                self._enqueue((stream, self._counts[stream] - len(buffer), buffer))
                self._buffers[stream] = []
        self._enqueue(_STOP)
        self._thread.join()
        self._raise_writer_failure()
        self._sealed = True

    def ledger_sha256(
        self,
        *,
        fx_history: list[tuple[str, Decimal, datetime]],
        marks: Mapping[str, tuple[Decimal, datetime, str]],
    ) -> str:
        """Reproduce the frozen ledger hash after bounded stream finalization."""

        self.seal()
        digest = hashlib.sha256()
        digest.update(b'{"fx_snapshots":[')
        for index, (currency, rate, event_time) in enumerate(fx_history):
            if index:
                digest.update(b",")
            digest.update(
                (
                    "{"
                    f'"currency":{string_token(currency)},'
                    f'"event_time":{utc_token(event_time, zulu=False)},'
                    f'"rate":{string_token(str(rate))},'
                    f'"version":{index + 1}'
                    "}"
                ).encode()
            )
        digest.update(b'],"marks":[')
        for index, (instrument_id, (price, event_time, event_id)) in enumerate(
            sorted(marks.items())
        ):
            if index:
                digest.update(b",")
            digest.update(
                (
                    "{"
                    f'"event_id":{string_token(event_id)},'
                    f'"event_time":{utc_token(event_time, zulu=False)},'
                    f'"instrument_id":{string_token(instrument_id)},'
                    f'"price":{string_token(str(price))}'
                    "}"
                ).encode()
            )
        digest.update(b'],"transactions":[')
        for index, payload in enumerate(self._iter_payload_bytes("ledger_transactions")):
            if index:
                digest.update(b",")
            digest.update(payload)
        digest.update(b"]}")
        return digest.hexdigest()

    def abort(self) -> None:
        """Fail closed while preserving the incomplete directory for diagnosis."""

        if self._closed:
            return
        for buffer in self._buffers.values():
            buffer.clear()
        self._staged = None
        deadline = time.monotonic() + 10
        while self._thread.is_alive() and time.monotonic() < deadline:
            try:
                self._queue.put(_STOP, timeout=0.1)
                break
            except queue.Full:
                continue
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("artifact writer did not stop after abort")
        failure = {
            "artifact_format": "puresaber.arrow-canonical-json.v1",
            "counts": dict(self._counts),
            "complete": False,
            "poisoned": self._poisoned,
        }
        try:
            (self.root / "FAILED.json").write_text(
                json.dumps(failure, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        finally:
            self._closed = True

    def _iter_payload_bytes(self, stream: str) -> Iterator[bytes]:
        path = self.root / f"{stream}.arrow"
        if not path.exists():
            return
        with pa.memory_map(str(path), "r") as source:
            reader = ipc.open_stream(source)
            for batch in reader:
                for payload in batch.column("payload").to_pylist():
                    yield bytes(payload)

    def _write_loop(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                stream, first_sequence, payloads = item
                writer = self._writer(stream)
                sequences = pa.array(
                    range(first_sequence, first_sequence + len(payloads)), type=pa.int64()
                )
                values = pa.array(payloads, type=pa.large_binary())
                writer.write_batch(pa.record_batch([sequences, values], schema=_SCHEMA))
            for writer in self._writers.values():
                writer.close()
            for output in self._files.values():
                output.close()
        except Exception as exc:  # noqa: BLE001 - thread boundary must relay every writer failure
            self._failure = exc
            for writer in self._writers.values():
                with suppress(Exception):
                    writer.close()
            for output in self._files.values():
                with suppress(Exception):
                    output.close()

    def _writer(self, stream: str) -> ipc.RecordBatchStreamWriter:
        prior = self._writers.get(stream)
        if prior is not None:
            return prior
        output = pa.OSFile(str(self.root / f"{stream}.arrow"), "wb")
        writer = ipc.new_stream(output, _SCHEMA)
        self._files[stream] = output
        self._writers[stream] = writer
        return writer

    def _raise_writer_failure(self) -> None:
        if self._failure is not None:
            raise RuntimeError("artifact writer failed") from self._failure

    def _enqueue(self, item: object) -> None:
        while True:
            self._raise_writer_failure()
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue


@dataclass(frozen=True, slots=True)
class DividendReplayResult:
    artifacts: StoredRunArtifacts
    ledger: object


def _fixed_payload(value: FixedPoint) -> dict[str, int]:
    return {"units": value.units, "scale": value.scale}


def _fixed_from_payload(value: object, field: str) -> FixedPoint:
    if not isinstance(value, Mapping) or set(value) != {"units", "scale"}:
        raise ValidationError(f"{field} must be a fixed-point object")
    units = value["units"]
    scale = value["scale"]
    if isinstance(units, bool) or not isinstance(units, int):
        raise ValidationError(f"{field}.units must be an integer")
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValidationError(f"{field}.scale must be an integer")
    return FixedPoint(units, scale)


def _time_payload(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _time_from_payload(value: object, field: str) -> datetime:
    return parse_utc_timestamp(value, field=field)


def _spec_payload(spec: InstrumentSpec) -> dict[str, object]:
    return {
        "instrument_id": spec.instrument_id,
        "asset_class": spec.asset_class.value,
        "product_type": spec.product_type,
        "venue": spec.venue,
        "native_symbol": spec.native_symbol,
        "base_currency": spec.base_currency,
        "quote_currency": spec.quote_currency,
        "settlement_currency": spec.settlement_currency,
        "price_tick": _fixed_payload(spec.price_tick),
        "quantity_step": _fixed_payload(spec.quantity_step),
        "contract_multiplier": _fixed_payload(spec.contract_multiplier),
        "calendar_id": spec.calendar_id,
        "margin_mode": spec.margin_mode.value,
        "inverse": spec.inverse,
        "effective_from": _time_payload(spec.effective_from),
        "effective_to": (
            _time_payload(spec.effective_to) if spec.effective_to is not None else None
        ),
        "available_at": _time_payload(spec.available_at),
        "superseded_at": (
            _time_payload(spec.superseded_at) if spec.superseded_at is not None else None
        ),
        "underlying_id": spec.underlying_id,
        "expiry_date": spec.expiry_date.isoformat() if spec.expiry_date is not None else None,
        "metadata": dict(sorted(spec.metadata.items())),
    }


def _spec_from_payload(value: object) -> InstrumentSpec:
    if not isinstance(value, Mapping):
        raise ValidationError("instrument spec replay fact must be an object")
    return InstrumentSpec(
        instrument_id=value["instrument_id"],
        asset_class=AssetClass(value["asset_class"]),
        product_type=value["product_type"],
        venue=value["venue"],
        native_symbol=value["native_symbol"],
        base_currency=value["base_currency"],
        quote_currency=value["quote_currency"],
        settlement_currency=value["settlement_currency"],
        price_tick=_fixed_from_payload(value["price_tick"], "price_tick"),
        quantity_step=_fixed_from_payload(value["quantity_step"], "quantity_step"),
        contract_multiplier=_fixed_from_payload(
            value["contract_multiplier"], "contract_multiplier"
        ),
        calendar_id=value["calendar_id"],
        margin_mode=MarginMode(value["margin_mode"]),
        inverse=value["inverse"],
        effective_from=_time_from_payload(value["effective_from"], "effective_from"),
        effective_to=(
            _time_from_payload(value["effective_to"], "effective_to")
            if value["effective_to"] is not None
            else None
        ),
        available_at=_time_from_payload(value["available_at"], "available_at"),
        superseded_at=(
            _time_from_payload(value["superseded_at"], "superseded_at")
            if value["superseded_at"] is not None
            else None
        ),
        underlying_id=value["underlying_id"],
        expiry_date=(date.fromisoformat(value["expiry_date"]) if value["expiry_date"] else None),
        metadata=dict(value["metadata"]),
    )


def _transaction_from_payload(value: object) -> LedgerTransaction:
    if not isinstance(value, Mapping):
        raise ValidationError("ledger transaction replay fact must be an object")
    postings = []
    for posting in value["postings"]:
        postings.append(
            Posting(
                ledger_account=posting["ledger_account"],
                currency=posting["currency"],
                amount=_fixed_from_payload(posting["amount"], "posting.amount"),
                instrument_id=posting["instrument_id"],
                quantity_delta=(
                    _fixed_from_payload(posting["quantity_delta"], "posting.quantity_delta")
                    if posting["quantity_delta"] is not None
                    else None
                ),
            )
        )
    return LedgerTransaction(
        transaction_id=value["transaction_id"],
        idempotency_key=value["idempotency_key"],
        event_time=_time_from_payload(value["event_time"], "event_time"),
        event_type=LedgerEventType(value["event_type"]),
        reference_id=value["reference_id"],
        postings=tuple(postings),
    )


def _ledger_event_from_business_fact(value: object):
    if not isinstance(value, Mapping):
        raise ValidationError("ledger event business fact must be an object")
    kind = value.get("event_kind")
    payload = value.get("event")
    if not isinstance(payload, Mapping):
        raise ValidationError("ledger event payload must be an object")
    if kind == "fill":
        return Fill(
            fill_id=payload["fill_id"],
            order_id=payload["order_id"],
            account_id=payload["account_id"],
            strategy_id=payload["strategy_id"],
            instrument_id=payload["instrument_id"],
            side=Side(payload["side"]),
            quantity=_fixed_from_payload(payload["quantity"], "fill.quantity"),
            price=_fixed_from_payload(payload["price"], "fill.price"),
            event_time=_time_from_payload(payload["event_time"], "fill.event_time"),
            liquidity_role=LiquidityRole(payload["liquidity_role"]),
            venue_trade_id=payload["venue_trade_id"],
        )
    if kind == "fee":
        return Fee(
            fee_id=payload["fee_id"],
            fill_id=payload["fill_id"],
            account_id=payload["account_id"],
            amount=_fixed_from_payload(payload["amount"], "fee.amount"),
            currency=payload["currency"],
            event_time=_time_from_payload(payload["event_time"], "fee.event_time"),
            fee_type=payload["fee_type"],
        )
    if kind == "funding":
        return Funding(
            funding_id=payload["funding_id"],
            account_id=payload["account_id"],
            instrument_id=payload["instrument_id"],
            amount=_fixed_from_payload(payload["amount"], "funding.amount"),
            currency=payload["currency"],
            event_time=_time_from_payload(payload["event_time"], "funding.event_time"),
        )
    if kind == "settlement":
        return Settlement(
            settlement_id=payload["settlement_id"],
            account_id=payload["account_id"],
            instrument_id=payload["instrument_id"],
            amount=_fixed_from_payload(payload["amount"], "settlement.amount"),
            currency=payload["currency"],
            event_time=_time_from_payload(payload["event_time"], "settlement.event_time"),
            settlement_type=payload["settlement_type"],
            settlement_price=(
                _fixed_from_payload(payload["settlement_price"], "settlement.settlement_price")
                if payload.get("settlement_price") is not None
                else None
            ),
        )
    if kind == "corporate_action":
        return CorporateActionEvent(
            event_id=payload["event_id"],
            instrument_id=payload["instrument_id"],
            event_time=_time_from_payload(payload["event_time"], "action.event_time"),
            received_at=_time_from_payload(payload["received_at"], "action.received_at"),
            available_at=_time_from_payload(payload["available_at"], "action.available_at"),
            source=payload["source"],
            trading_day=date.fromisoformat(payload["trading_day"]),
            session_id=payload["session_id"],
            sequence=payload["sequence"],
            action_type=payload["action_type"],
            effective_date=date.fromisoformat(payload["effective_date"]),
            ratio=(
                _fixed_from_payload(payload["ratio"], "action.ratio")
                if payload["ratio"] is not None
                else None
            ),
            cash_amount=(
                _fixed_from_payload(payload["cash_amount"], "action.cash_amount")
                if payload["cash_amount"] is not None
                else None
            ),
            currency=payload["currency"],
        )
    raise ValidationError("unsupported ledger event business fact")


def _mark_from_business_fact(value: object) -> MarkPriceEvent:
    if not isinstance(value, Mapping):
        raise ValidationError("mark business fact must be an object")
    if value.get("event_type") != "mark_price":
        raise ValidationError("mark business fact has an invalid event type")
    return MarkPriceEvent(
        event_id=value["event_id"],
        instrument_id=value["instrument_id"],
        event_time=_time_from_payload(value["event_time"], "mark.event_time"),
        received_at=_time_from_payload(value["received_at"], "mark.received_at"),
        available_at=_time_from_payload(value["available_at"], "mark.available_at"),
        source=value["source"],
        trading_day=date.fromisoformat(value["trading_day"]),
        session_id=value["session_id"],
        sequence=value["sequence"],
        price=_fixed_from_payload(value["price"], "mark.price"),
    )


def _account_snapshot_payload(value) -> dict[str, object]:
    return {
        "account_id": value.account_id,
        "event_time": _time_payload(value.event_time),
        "base_currency": value.base_currency,
        "cash_balances": {
            key: _fixed_payload(item) for key, item in sorted(value.cash_balances.items())
        },
        "positions": {key: _fixed_payload(item) for key, item in sorted(value.positions.items())},
        "nav": _fixed_payload(value.nav),
        "cost_basis": {key: _fixed_payload(item) for key, item in sorted(value.cost_basis.items())},
        "realized_pnl": {
            key: _fixed_payload(item) for key, item in sorted(value.realized_pnl.items())
        },
        "unrealized_pnl": {
            key: _fixed_payload(item) for key, item in sorted(value.unrealized_pnl.items())
        },
        "initial_margin": _fixed_payload(value.initial_margin),
        "maintenance_margin": _fixed_payload(value.maintenance_margin),
        "liquidation_required": value.liquidation_required,
    }


def _dividend_state_payload(ledger) -> list[dict[str, object]]:
    return [state.to_dict() for _, state in sorted(ledger._dividend_lifecycle_states.items())]


def _sealed_dividend_ledger(captured: Mapping[str, object]):
    from quant_execution.ledger import ExactAccountLedger

    sealed = ExactAccountLedger(
        account_id=captured["account_id"],
        base_currency=captured["base_currency"],
        instruments=captured["instruments"],
        initial_cash=captured["initial_cash"],
        money_scale=captured["money_scale"],
        opened_at=captured["opened_at"],
        dividend_execution_mode=captured["dividend_execution_mode"],
        fx_valuation_mode=captured["fx_valuation_mode"],
        entitlement_evidence_verifier=captured["entitlement_evidence_verifier"],
    )
    sealed._restore_captured_state(captured["state"])
    return sealed


def _trusted_verification_scope(ledger) -> list[str]:
    if ledger.dividend_execution_mode is not DividendExecutionMode.PRODUCTION_CERTIFIED:
        return []
    phases = [
        record
        for record in ledger._dividend_execution_records
        if isinstance(record, DividendExecutionRecord)
    ]
    scope = []
    if any(record.phase is DividendExecutionPhase.ENTITLEMENT for record in phases):
        scope.append("entitlement_basis")
    if any(record.lifecycle_snapshot.get("payment_policy") is not None for record in phases):
        scope.append("payment_policy")
    if any(record.phase is DividendExecutionPhase.PAYMENT for record in phases):
        scope.append("actual_dividend_payment")
    return scope


def _dividend_run_metadata(ledger) -> dict[str, object]:
    if ledger.dividend_execution_mode is None:
        raise ValidationError("dividend export requires an explicit execution mode")
    if ledger.fx_valuation_mode is not FxValuationMode.EVIDENCED_PIT:
        raise ValidationError("dividend export requires EVIDENCED_PIT")
    if not ledger._dividend_operation_log:
        raise ValidationError("dividend export requires lifecycle records")
    snapshot = ledger.snapshot(ledger._event_time)
    transaction_bytes = [ledger_transaction_bytes(item) for item in ledger._transactions]
    initial_count = len(ledger._initial_cash)
    metadata = {
        "kind": "puresaber.execution.dividend-run/1",
        "market_admission_certified": False,
        "trusted_verification_scope": _trusted_verification_scope(ledger),
        "business_facts": list(ledger._dividend_replay_facts),
        "initial_conditions": {
            "account_id": ledger.account_id,
            "base_currency": ledger.base_currency,
            "money_scale": ledger.money_scale,
            "opened_at": _time_payload(ledger._default_opened_at),
            "initial_cash": {
                key: _fixed_payload(value) for key, value in sorted(ledger._initial_cash.items())
            },
            "instruments": [_spec_payload(spec) for _, spec in sorted(ledger.instruments.items())],
            "execution_mode": ledger.dividend_execution_mode.value,
            "fx_valuation_mode": ledger.fx_valuation_mode.value,
            "initial_transaction_count": initial_count,
        },
        "final_facts": {
            "event_time": _time_payload(ledger._event_time),
            "marks": [
                {
                    "instrument_id": instrument_id,
                    "price": str(price),
                    "event_time": _time_payload(event_time),
                    "event_id": event_id,
                }
                for instrument_id, (price, event_time, event_id) in sorted(ledger._marks.items())
            ],
            "position_lots": {
                instrument_id: [
                    {"date": lot_date.isoformat(), "quantity": str(quantity)}
                    for lot_date, quantity in lots
                ]
                for instrument_id, lots in sorted(ledger._position_lots.items())
            },
            "transaction_sha256": hashlib.sha256(
                canonical_bytes([item.hex() for item in transaction_bytes])
            ).hexdigest(),
            "dividend_states": _dividend_state_payload(ledger),
            "account_snapshot": _account_snapshot_payload(snapshot),
            "journal_sha256": ledger.journal_sha256,
        },
    }
    metadata["metadata_sha256"] = hashlib.sha256(canonical_bytes(metadata)).hexdigest()
    return metadata


def export_dividend_run(ledger, root: str | Path) -> StoredRunArtifacts:
    """Export an immutable manifest 1.1 run and publish only after full replay validation."""

    captured = ledger.capture_dividend_export_state()
    sealed = _sealed_dividend_ledger(captured)
    metadata = _dividend_run_metadata(sealed)
    transactions = tuple(sealed._transactions)
    dividend_records = tuple(sealed._dividend_operation_log)
    verifier = captured["entitlement_evidence_verifier"]
    sink = ArrowReplayArtifactSink(root, manifest_schema_version="1.1.0")
    try:
        sink.begin()
        for transaction in transactions:
            sink.append("ledger_transactions", ledger_transaction_bytes(transaction))
        for record in dividend_records:
            sink.append("dividend_records", dividend_record_bytes(record))
        sink.commit()

        def validate_candidate(candidate: StoredRunArtifacts) -> None:
            replay_dividend_run(
                candidate,
                entitlement_evidence_verifier=verifier,
            )

        return sink.close(metadata, candidate_validator=validate_candidate)
    except Exception:
        sink.abort()
        raise


def _validate_metadata(
    metadata: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if metadata.get("kind") != "puresaber.execution.dividend-run/1":
        raise ValidationError("artifact is not a dividend replay run")
    expected_hash = metadata.get("metadata_sha256")
    unsigned = dict(metadata)
    unsigned.pop("metadata_sha256", None)
    if (
        not isinstance(expected_hash, str)
        or hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != expected_hash
    ):
        raise ValidationError("dividend run metadata hash mismatch")
    initial = metadata.get("initial_conditions")
    final = metadata.get("final_facts")
    if not isinstance(initial, Mapping) or not isinstance(final, Mapping):
        raise ValidationError("dividend run metadata is incomplete")
    return initial, final


def replay_dividend_run(
    source: str | Path | StoredRunArtifacts,
    *,
    entitlement_evidence_verifier: EntitlementEvidenceVerifier | None = None,
) -> DividendReplayResult:
    """Rebuild a dividend ledger from initial conditions and ordered persisted facts."""

    from quant_execution.ledger import ExactAccountLedger

    artifacts = source if isinstance(source, StoredRunArtifacts) else load_stored_artifacts(source)
    if artifacts.schema_version != "1.1.0":
        raise ValidationError("dividend replay requires manifest schema version 1.1.0")
    initial, final = _validate_metadata(artifacts.run_metadata)
    execution_mode = DividendExecutionMode(initial["execution_mode"])
    if (
        execution_mode is DividendExecutionMode.PRODUCTION_CERTIFIED
        and entitlement_evidence_verifier is None
    ):
        raise ValidationError("production dividend replay requires a trusted verifier")
    specs = [_spec_from_payload(item) for item in initial["instruments"]]
    instruments = {item.instrument_id: item for item in specs}
    initial_cash = {
        key: _fixed_from_payload(value, f"initial_cash.{key}")
        for key, value in initial["initial_cash"].items()
    }
    ledger = ExactAccountLedger(
        account_id=initial["account_id"],
        base_currency=initial["base_currency"],
        instruments=instruments,
        initial_cash=initial_cash,
        money_scale=initial["money_scale"],
        opened_at=_time_from_payload(initial["opened_at"], "opened_at"),
        dividend_execution_mode=execution_mode,
        fx_valuation_mode=FxValuationMode(initial["fx_valuation_mode"]),
        entitlement_evidence_verifier=entitlement_evidence_verifier,
    )
    transactions = [
        _transaction_from_payload(value) for value in artifacts.iter_json("ledger_transactions")
    ]
    initial_count = initial["initial_transaction_count"]
    if initial_count != len(ledger.transactions):
        raise ValidationError("initial transaction count changed during replay")
    for index in range(initial_count):
        if ledger_transaction_bytes(ledger.transactions[index]) != ledger_transaction_bytes(
            transactions[index]
        ):
            raise ValidationError("initial transaction bytes changed during replay")
    records = [
        dividend_record_from_dict(value) for value in artifacts.iter_json("dividend_records")
    ]
    business_facts = artifacts.run_metadata.get("business_facts")
    if not isinstance(business_facts, list) or not all(
        isinstance(item, Mapping) for item in business_facts
    ):
        raise ValidationError("ordered business facts are missing")
    for item in business_facts:
        sequence = item.get("operation_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValidationError("business fact operation sequence is invalid")
    operations = [(item["operation_sequence"], "business_fact", item) for item in business_facts]
    operations.extend((item.operation_sequence, "dividend_record", item) for item in records)
    operations.sort(key=lambda item: item[0])
    if [item[0] for item in operations] != list(range(len(operations))):
        raise ValidationError("replay operation sequence is not contiguous")
    observation_sequences = [
        item.observation_sequence for item in records if isinstance(item, PitFxObservationRecord)
    ]
    if observation_sequences != list(range(len(observation_sequences))):
        raise ValidationError("PIT FX observation sequence is not contiguous")

    cursor = initial_count

    def compare_transaction_slice(before: int, after: int, *, context: str) -> None:
        nonlocal cursor
        if before != cursor or after < before or after > len(transactions):
            raise ValidationError(f"{context} transaction boundary is invalid")
        actual_created = ledger.transactions[before:after]
        expected_created = transactions[before:after]
        if [ledger_transaction_bytes(item) for item in actual_created] != [
            ledger_transaction_bytes(item) for item in expected_created
        ]:
            raise ValidationError(f"{context} transaction bytes changed during replay")
        cursor = after

    for _, operation_kind, expected in operations:
        if operation_kind == "business_fact":
            before = expected.get("transaction_count_before")
            after = expected.get("transaction_count_after")
            payload = expected.get("payload")
            if (
                isinstance(before, bool)
                or not isinstance(before, int)
                or isinstance(after, bool)
                or not isinstance(after, int)
                or not isinstance(payload, Mapping)
            ):
                raise ValidationError("business fact is malformed")
            kind = expected.get("kind")
            if kind == "opening_position":
                ledger.book_opening_position(
                    instrument_id=payload["instrument_id"],
                    quantity=_fixed_from_payload(payload["quantity"], "opening.quantity"),
                    average_cost=_fixed_from_payload(
                        payload["average_cost"], "opening.average_cost"
                    ),
                    acquired_on=date.fromisoformat(payload["acquired_on"]),
                )
            elif kind == "external_cash":
                ledger.book_external_cash(
                    transfer_id=payload["transfer_id"],
                    amount=_fixed_from_payload(payload["amount"], "external_cash.amount"),
                    currency=payload["currency"],
                    event_time=_time_from_payload(
                        payload["event_time"], "external_cash.event_time"
                    ),
                )
            elif kind == "mark":
                ledger.mark(_mark_from_business_fact(payload), create_snapshot=False)
            elif kind == "ledger_event":
                event = _ledger_event_from_business_fact(payload)
                trading_day_value = payload.get("trading_day")
                if isinstance(event, Fill):
                    if not isinstance(trading_day_value, str):
                        raise ValidationError("fill business fact requires trading_day")
                    ledger.apply_with_trading_day(
                        event,
                        trading_day=date.fromisoformat(trading_day_value),
                        create_snapshot=False,
                    )
                else:
                    if trading_day_value is not None:
                        raise ValidationError("non-fill business fact cannot set trading_day")
                    ledger.apply(event, create_snapshot=False)
            else:
                raise ValidationError("unsupported ordered business fact")
            compare_transaction_slice(before, after, context="business fact")
            if ledger._dividend_replay_facts[-1] != dict(expected):
                raise ValidationError("business fact changed during replay")
            continue

        if isinstance(expected, DividendExecutionRecord):
            before = expected.transaction_count_before
            after = expected.transaction_count_after
            if before != cursor:
                raise ValidationError("dividend phase transaction boundary is invalid")
            lifecycle = DividendLifecycle.from_dict(expected.to_dict()["lifecycle_snapshot"])
            actual = ledger.apply_dividend_lifecycle(
                DividendExecutionRequest(
                    lifecycle=lifecycle,
                    phase=DividendExecutionPhase(expected.phase),
                    cutoff=expected.cutoff,
                    entitlement_basis=expected.entitlement_basis,
                )
            )
            compare_transaction_slice(before, after, context="dividend phase")
        elif isinstance(expected, PitFxObservationRecord):
            if expected.transaction_count != cursor:
                raise ValidationError("PIT FX transaction boundary is invalid")
            actual = ledger.observe_pit_fx(PitFxRate.from_dict(expected.to_dict()["rate_payload"]))
        elif isinstance(expected, DividendValuationRecord):
            if expected.transaction_count != cursor:
                raise ValidationError("valuation transaction boundary is invalid")
            actual = ledger.record_dividend_valuation(as_of=expected.as_of)
        else:  # pragma: no cover - closed union guarded by parser
            raise ValidationError("unsupported dividend replay record")
        if actual.to_dict() != expected.to_dict():
            raise ValidationError("dividend record changed during replay")
    if cursor != len(transactions):
        raise ValidationError("ledger transactions lack ordered business facts")

    expected_event_time = _time_from_payload(final["event_time"], "final.event_time")
    if ledger._event_time != expected_event_time:
        raise ValidationError("replayed ledger event time mismatch")
    marks = final.get("marks")
    actual_marks = [
        {
            "instrument_id": instrument_id,
            "price": str(price),
            "event_time": _time_payload(event_time),
            "event_id": event_id,
        }
        for instrument_id, (price, event_time, event_id) in sorted(ledger._marks.items())
    ]
    if actual_marks != marks:
        raise ValidationError("replayed mark state mismatch")
    position_lots = final.get("position_lots")
    actual_position_lots = {
        instrument_id: [
            {"date": lot_date.isoformat(), "quantity": str(quantity)} for lot_date, quantity in lots
        ]
        for instrument_id, lots in sorted(ledger._position_lots.items())
    }
    if actual_position_lots != position_lots:
        raise ValidationError("replayed position lots mismatch")
    transaction_hash = hashlib.sha256(
        canonical_bytes([ledger_transaction_bytes(item).hex() for item in ledger.transactions])
    ).hexdigest()
    if transaction_hash != final.get("transaction_sha256"):
        raise ValidationError("replayed transaction sequence hash mismatch")
    if _dividend_state_payload(ledger) != final.get("dividend_states"):
        raise ValidationError("replayed dividend lifecycle state mismatch")
    if _account_snapshot_payload(ledger.snapshot(ledger._event_time)) != final.get(
        "account_snapshot"
    ):
        raise ValidationError("replayed account snapshot mismatch")
    if ledger.journal_sha256 != final.get("journal_sha256"):
        raise ValidationError("replayed ledger journal hash mismatch")
    return DividendReplayResult(artifacts=artifacts, ledger=ledger)


__all__ = [
    "ArrowReplayArtifactSink",
    "DividendReplayResult",
    "StoredRunArtifacts",
    "export_dividend_run",
    "fee_bytes",
    "fill_bytes",
    "ledger_transaction_bytes",
    "load_stored_artifacts",
    "order_bytes",
    "order_event_bytes",
    "replay_dividend_run",
    "settlement_bytes",
]
