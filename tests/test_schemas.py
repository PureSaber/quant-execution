from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest
from quant_data_kit.exceptions import ValidationError

from quant_execution.schemas import (
    FEE_SCHEMA_ID,
    FILL_SCHEMA_ID,
    FUNDING_SCHEMA_ID,
    LEDGER_TRANSACTION_SCHEMA_ID,
    ORDER_EVENT_SCHEMA_ID,
    ORDER_INTENT_SCHEMA_ID,
    ORDER_SCHEMA_ID,
    SETTLEMENT_SCHEMA_ID,
    get_arrow_schema,
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
        validate_json_record(schema_id, payload)
        assert list(payload) == get_arrow_schema(schema_id).names


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
