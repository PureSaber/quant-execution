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
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
from pyarrow import ipc
from quant_data_kit.exceptions import ValidationError

from quant_execution._json import fixed_token, string_token, utc_token
from quant_execution.contracts import Fee, Fill, LedgerTransaction, Order, OrderEvent, Settlement

_STREAMS: Final = (
    "orders",
    "order_events",
    "fills",
    "fees",
    "settlements",
    "ledger_transactions",
    "risk_events",
)
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

    def iter_payload_bytes(self, stream: str) -> Iterator[bytes]:
        if stream not in _STREAMS:
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


def load_stored_artifacts(root: str | Path) -> StoredRunArtifacts:
    """Strictly verify a completed artifact directory before exposing its facts."""

    resolved = Path(root).resolve()
    manifest_path = resolved / "manifest.json"
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
    if payload.get("schema_version") != "1.0.0":
        raise ValidationError("artifact manifest schema version is unsupported")
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
    if not isinstance(counts, dict) or set(counts) != set(_STREAMS):
        raise ValidationError("artifact counts changed shape")
    if not isinstance(logical, dict) or set(logical) != set(_STREAMS):
        raise ValidationError("artifact logical hashes changed shape")
    if not isinstance(files, dict):
        raise ValidationError("artifact files must be an object")
    verified_counts: dict[str, int] = {}
    verified_logical: dict[str, str] = {}
    verified_files: dict[str, dict[str, object]] = {}
    for stream in _STREAMS:
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
    ) -> None:
        if isinstance(batch_size, bool) or batch_size <= 0:
            raise ValidationError("batch_size must be a positive integer")
        if isinstance(queue_batches, bool) or queue_batches <= 0:
            raise ValidationError("queue_batches must be a positive integer")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self._batch_size = batch_size
        self._buffers: dict[str, list[bytes]] = {name: [] for name in _STREAMS}
        self._counts = {name: 0 for name in _STREAMS}
        self._digests = {name: _SequenceDigest() for name in _STREAMS}
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_batches)
        self._failure: BaseException | None = None
        self._closed = False
        self._sealed = False
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
        if stream not in self._buffers:
            raise ValidationError(f"unknown artifact stream: {stream}")
        if not isinstance(payload, bytes):
            raise ValidationError("artifact payload must be canonical bytes")
        if self._staged is not None:
            self._staged.append((stream, payload))
            return
        self._append_committed(stream, payload)

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
        for stream, payload in staged:
            self._append_committed(stream, payload)

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

    def close(self, manifest: Mapping[str, object]) -> StoredRunArtifacts:
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
                for name in _STREAMS
                for path in (self.root / f"{name}.arrow",)
                if path.is_file()
            }
            completed = {
                "schema_version": "1.0.0",
                "artifact_format": "puresaber.arrow-canonical-json.v1",
                "counts": dict(self._counts),
                "logical_sha256": logical,
                "files": files,
                "complete": True,
                "run_metadata": dict(manifest),
            }
            completed["manifest_sha256"] = _manifest_hash(completed)
            manifest_path = self.root / "manifest.json"
            _write_no_clobber(manifest_path, _canonical_manifest_bytes(completed))
            self._closed = True
            return StoredRunArtifacts(
                root=self.root,
                manifest_path=manifest_path,
                counts=dict(self._counts),
                logical_sha256=logical,
                files=files,
                manifest_sha256=str(completed["manifest_sha256"]),
            )
        except Exception:
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


__all__ = [
    "ArrowReplayArtifactSink",
    "StoredRunArtifacts",
    "fee_bytes",
    "fill_bytes",
    "ledger_transaction_bytes",
    "load_stored_artifacts",
    "order_bytes",
    "order_event_bytes",
    "settlement_bytes",
]
