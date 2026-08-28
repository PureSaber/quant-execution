from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
from conftest import T0, fp

from quant_execution import (
    ACCOUNT_SNAPSHOT_SCHEMA_ID,
    RUN_RESULT_SCHEMA_ID,
    AccountSnapshot,
    RunResult,
    execution_payload,
    get_arrow_schema,
    validate_arrow_table,
    validate_json_record,
)


def test_m3_account_snapshot_and_run_result_goldens_match_json_and_arrow() -> None:
    path = Path(__file__).parent / "golden" / "m3" / "contracts.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    values = {
        ACCOUNT_SNAPSHOT_SCHEMA_ID: AccountSnapshot(
            account_id="account",
            event_time=T0,
            base_currency="USD",
            cash_balances={"USD": fp("100")},
            positions={"asset": fp("2")},
            nav=fp("110"),
            cost_basis={"asset": fp("5")},
            realized_pnl={"asset": fp("1")},
            unrealized_pnl={"asset": fp("10")},
            initial_margin=fp("4"),
            maintenance_margin=fp("2"),
            liquidation_required=False,
        ),
        RUN_RESULT_SCHEMA_ID: RunResult(
            run_id="run",
            seed=7,
            event_count=3,
            order_count=2,
            fill_count=1,
            event_sha256="1" * 64,
            fill_sha256="2" * 64,
            ledger_sha256="3" * 64,
        ),
    }
    actual = {schema_id: execution_payload(value) for schema_id, value in values.items()}
    assert actual == expected
    for schema_id, payload in actual.items():
        validate_json_record(schema_id, payload)
        schema = get_arrow_schema(schema_id)
        arrow_payload = dict(payload)
        if "event_time" in arrow_payload:
            arrow_payload["event_time"] = T0
        table = pa.Table.from_pylist([arrow_payload], schema=schema)
        validate_arrow_table(schema_id, table)
