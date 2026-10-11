"""Offline JSON entry point using QDK v2 event payloads and shared accounting."""

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from quant_data_kit import (
    AggressorSide,
    AssetClass,
    BookAction,
    BookDeltaEvent,
    BookLevel,
    BookSide,
    BookSnapshotEvent,
    FixedPoint,
    InstrumentSpec,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)

from .market_making import QuotePolicy, replay_market_making
from .schemas import execution_payload


def stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return result


def event_from_payload(raw):
    value = dict(raw)
    kind = value.pop("event_type")
    classes = {
        "quote": QuoteEvent,
        "trade": TradeEvent,
        "book_snapshot": BookSnapshotEvent,
        "book_delta": BookDeltaEvent,
        "status": StatusEvent,
    }
    if kind not in classes:
        raise ValueError("market-making CLI supports quote/trade/L2/status events")
    for key in ("event_time", "received_at", "available_at"):
        value[key] = stamp(value[key])
    value["trading_day"] = date.fromisoformat(value["trading_day"])
    for key in ("bid_price", "ask_price", "bid_quantity", "ask_quantity", "price", "quantity"):
        if key in value:
            value[key] = FixedPoint(**value[key])
    for side in ("bids", "asks"):
        if side in value:
            value[side] = tuple(
                BookLevel(
                    price=FixedPoint(**level["price"]),
                    quantity=FixedPoint(**level["quantity"]),
                    order_count=level.get("order_count"),
                )
                for level in value[side]
            )
    if kind == "trade":
        value["aggressor_side"] = AggressorSide(value["aggressor_side"])
    if kind == "book_delta":
        value["side"], value["action"] = BookSide(value["side"]), BookAction(value["action"])
    return classes[kind](**value)


def evaluate(raw):
    """Run deterministic research in memory, without changing inputs or files."""
    contents = json.dumps(raw, sort_keys=True, allow_nan=False).encode()
    if raw.get("schema") != "quant.market-making-input/1":
        raise ValueError("unsupported market-making input schema")
    if raw.get("evidence_kind") not in {"synthetic", "retrospective"}:
        raise ValueError("declare synthetic or retrospective inputs")
    if not isinstance(raw.get("source_ref"), str) or not raw["source_ref"].strip():
        raise ValueError("source_ref is required")
    spec = dict(raw["instrument"])
    spec["asset_class"] = AssetClass(spec["asset_class"])
    for key in ("price_tick", "quantity_step", "contract_multiplier"):
        spec[key] = FixedPoint(**spec[key])
    for key in ("effective_from", "available_at"):
        spec[key] = stamp(spec[key])
    # Deliberately keep this CLI on explicit US cash rules; other market-specific
    # configurations use the Python API and their own supported rule contracts.
    if spec["product_type"] not in {"us_equity", "us_etf"} or spec["settlement_currency"] != "USD":
        raise ValueError("CLI v1 supports explicitly configured US cash equity/ETF")
    instrument = InstrumentSpec(**spec)
    policy = dict(raw["policy"])
    for key in ("order_quantity", "max_inventory", "target_inventory"):
        policy[key] = FixedPoint(**policy[key])
    engine, decisions = replay_market_making(
        [event_from_payload(row) for row in raw["events"]],
        instrument,
        QuotePolicy(**policy),
        initial_cash=FixedPoint(**raw["initial_cash"]),
        matching=raw.get("matching", "l2"),
        latency_ms=raw.get("latency_ms", 1),
    )
    artifacts = engine.artifacts
    report = {
        "title": "库存报价与成交模拟",
        "currency": "USD",
        "schema": "quant.market-making-study/1",
        "evidence_kind": raw["evidence_kind"],
        "input_sha256": hashlib.sha256(contents).hexdigest(),
        "source_ref": raw["source_ref"],
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("market_making.py", "market_making_cli.py", "rules.py", "matching.py")
        },
        "matching": raw.get("matching", "l2"),
        "config": {k: v for k, v in raw.items() if k != "events"},
        "result": execution_payload(artifacts.result),
        "decisions": decisions,
        "final_account": execution_payload(engine.ledger.snapshot()),
        "risk_events": list(artifacts.risk_events),
        "orders": [execution_payload(v) for v in artifacts.orders],
        "fills": [execution_payload(v) for v in artifacts.fills],
        "fees": [execution_payload(v) for v in artifacts.fees],
        "ledger": [execution_payload(v) for v in artifacts.ledger_transactions],
        "limits": [
            "Long-only cash inventory; no naked shorts or margin-financed market-making",
            "L2 queue-ahead is a model; BBO mode lacks queue priority",
            "Current event matches before cancellation/quote decisions; cancellation is event-driven",
            "Fees, ticks and lot sizes are input assumptions; no exchange-priority certification",
            "Ending inventory is marked, not forcibly liquidated; no live order transmission",
        ],
    }
    return report


def run(input_path, output):
    report = evaluate(json.loads(Path(input_path).read_bytes()))
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    payload = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    (root / "market-making.json").write_bytes(payload)
    (root / "artifacts.json").write_text(
        json.dumps({"market-making.json": hashlib.sha256(payload).hexdigest()}, indent=2),
        encoding="utf-8",
    )
    return {
        "output": str(root),
        "evidence_kind": report["evidence_kind"],
        "fills": len(report["fills"]),
        "ledger_sha256": report["result"]["ledger_sha256"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线库存报价与成交模拟")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = run(args.input, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
