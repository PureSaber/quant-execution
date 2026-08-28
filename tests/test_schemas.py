from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pytest
from conftest import T0, fp
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import Fee, Settlement
from quant_execution.schemas import (
    FEE_SCHEMA_ID,
    FILL_SCHEMA_ID,
    FUNDING_SCHEMA_ID,
    LEDGER_TRANSACTION_SCHEMA_ID,
    LEGACY_SCHEMA_VERSION,
    ORDER_EVENT_SCHEMA_ID,
    ORDER_INTENT_SCHEMA_ID,
    ORDER_SCHEMA_ID,
    SCHEMA_VERSION,
    SETTLEMENT_SCHEMA_ID,
    execution_payload,
    get_arrow_schema,
    get_json_schema,
    validate_arrow_table,
    validate_json_record,
)

SCHEMA_IDS = {
    ORDER_INTENT_SCHEMA_ID,
    ORDER_SCHEMA_ID,
    ORDER_EVENT_SCHEMA_ID,
    FILL_SCHEMA_ID,
    FEE_SCHEMA_ID,
    FUNDING_SCHEMA_ID,
    SETTLEMENT_SCHEMA_ID,
    LEDGER_TRANSACTION_SCHEMA_ID,
}


def test_all_execution_goldens_match_json_and_arrow_field_contracts() -> None:
    path = Path(__file__).parent / "golden" / "v1" / "records.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    assert set(records) == SCHEMA_IDS
    for schema_id, payload in records.items():
        validate_json_record(schema_id, payload, LEGACY_SCHEMA_VERSION)
        assert list(payload) == get_arrow_schema(schema_id, LEGACY_SCHEMA_VERSION).names


def test_arrow_contract_rejects_nullable_or_wrong_physical_fields() -> None:
    schema = get_arrow_schema(FILL_SCHEMA_ID)
    valid = pa.Table.from_arrays([pa.array([], type=field.type) for field in schema], schema=schema)
    validate_arrow_table(FILL_SCHEMA_ID, valid)

    wrong_schema = pa.schema([pa.field(field.name, field.type, nullable=True) for field in schema])
    wrong = pa.Table.from_arrays(
        [pa.array([], type=field.type) for field in wrong_schema], schema=wrong_schema
    )
    with pytest.raises(ValidationError, match="Arrow schema mismatch"):
        validate_arrow_table(FILL_SCHEMA_ID, wrong)

    postings = get_arrow_schema(LEDGER_TRANSACTION_SCHEMA_ID).field("postings")
    assert postings.type.value_field.nullable is False


def test_unknown_schema_version_fails_closed() -> None:
    with pytest.raises(ValidationError, match="Unsupported"):
        get_arrow_schema(FILL_SCHEMA_ID, "2.0.0")
    with pytest.raises(ValidationError, match="Unknown execution schema ID"):
        get_arrow_schema("puresaber.execution.unknown")
    with pytest.raises(ValidationError, match="Unknown execution schema ID"):
        get_json_schema("puresaber.execution.unknown")
    assert (
        execution_payload(
            Fee(
                fee_id="fee",
                fill_id="fill",
                account_id="account",
                amount=fp("1"),
                currency="USD",
                event_time=T0,
                fee_type="commission",
            )
        )["fee_id"]
        == "fee"
    )


def test_settlement_schema_versions_preserve_v1_and_dual_read_arrow() -> None:
    root = Path(__file__).parent / "golden"
    legacy_records = json.loads((root / "v1" / "records.json").read_text(encoding="utf-8"))
    legacy = legacy_records[SETTLEMENT_SCHEMA_ID]
    latest_golden = json.loads((root / "v1_1" / "settlement.json").read_text(encoding="utf-8"))
    latest = latest_golden["record"]
    assert latest_golden["schema_version"] == SCHEMA_VERSION

    validate_json_record(SETTLEMENT_SCHEMA_ID, legacy, LEGACY_SCHEMA_VERSION)
    validate_json_record(SETTLEMENT_SCHEMA_ID, legacy)
    validate_json_record(SETTLEMENT_SCHEMA_ID, latest)
    with pytest.raises(ValidationError, match="JSON schema validation failed"):
        validate_json_record(SETTLEMENT_SCHEMA_ID, latest, LEGACY_SCHEMA_VERSION)

    legacy_arrow = dict(legacy)
    legacy_arrow["event_time"] = datetime.fromisoformat(legacy["event_time"].replace("Z", "+00:00"))
    legacy_table = pa.Table.from_pylist(
        [legacy_arrow], schema=get_arrow_schema(SETTLEMENT_SCHEMA_ID, LEGACY_SCHEMA_VERSION)
    )
    validate_arrow_table(SETTLEMENT_SCHEMA_ID, legacy_table)
    with pytest.raises(ValidationError, match="Arrow schema mismatch"):
        validate_arrow_table(SETTLEMENT_SCHEMA_ID, legacy_table, SCHEMA_VERSION)

    latest_arrow = dict(latest)
    latest_arrow["event_time"] = datetime.fromisoformat(latest["event_time"].replace("Z", "+00:00"))
    latest_table = pa.Table.from_pylist(
        [latest_arrow], schema=get_arrow_schema(SETTLEMENT_SCHEMA_ID, SCHEMA_VERSION)
    )
    validate_arrow_table(SETTLEMENT_SCHEMA_ID, latest_table)
    assert (
        "settlement_price"
        not in get_json_schema(SETTLEMENT_SCHEMA_ID, LEGACY_SCHEMA_VERSION)["properties"]
    )

    without_price = Settlement(
        settlement_id="legacy",
        account_id="account",
        instrument_id="asset",
        amount=fp("0"),
        currency="USD",
        event_time=T0,
        settlement_type="daily_mark",
    )
    assert "settlement_price" not in execution_payload(without_price, version=LEGACY_SCHEMA_VERSION)
    assert execution_payload(without_price)["settlement_price"] is None
    with pytest.raises(ValidationError, match="cannot be serialized"):
        execution_payload(
            Settlement(
                settlement_id="latest",
                account_id="account",
                instrument_id="asset",
                amount=fp("0"),
                currency="USD",
                event_time=T0,
                settlement_type="daily_mark",
                settlement_price=fp("100"),
            ),
            version=LEGACY_SCHEMA_VERSION,
        )
    with pytest.raises(ValidationError, match="Unsupported execution schema version"):
        execution_payload(without_price, version="2.0.0")


def test_json_contract_rejects_illegal_transition_and_unbalanced_ledger() -> None:
    path = Path(__file__).parent / "golden" / "v1" / "records.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    illegal_event = dict(records[ORDER_EVENT_SCHEMA_ID])
    illegal_event["from_status"] = "created"
    illegal_event["to_status"] = "filled"
    with pytest.raises(ValidationError, match="JSON schema validation failed"):
        validate_json_record(ORDER_EVENT_SCHEMA_ID, illegal_event)

    fill_event = dict(records[ORDER_EVENT_SCHEMA_ID])
    fill_event["from_status"] = "accepted"
    fill_event["to_status"] = "partially_filled"
    with pytest.raises(ValidationError, match="JSON schema validation failed"):
        validate_json_record(ORDER_EVENT_SCHEMA_ID, fill_event)
    fill_event["fill_quantity"] = {"units": 25, "scale": 2}
    validate_json_record(ORDER_EVENT_SCHEMA_ID, fill_event)

    unbalanced = json.loads(json.dumps(records[LEDGER_TRANSACTION_SCHEMA_ID]))
    unbalanced["postings"][1]["amount"]["units"] = 9999
    with pytest.raises(ValidationError, match="unbalanced"):
        validate_json_record(LEDGER_TRANSACTION_SCHEMA_ID, unbalanced)
