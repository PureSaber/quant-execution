"""Chronological selection of predeclared fill models from observed executions.

Candidate predictions must be produced with the same orders and observation
window. This scores queue/latency assumptions; it never certifies exchange fills.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from quant_data_kit.derivatives.models import decimal, utc


def calibrate(payload, as_of):
    if payload.get("evidence_kind") not in {"synthetic", "retrospective"}:
        raise ValueError("explicit execution evidence kind required")
    candidates, records = payload["candidates"], payload["records"]
    if not candidates or len(records) < 10:
        raise ValueError("candidate definitions and >=10 execution observations required")
    rows, seen = [], set()
    for row in records:
        if row["order_id"] in seen or not row.get("source"):
            raise ValueError("unique order IDs and source evidence required")
        seen.add(row["order_id"])
        if not utc(row["submitted_at"]) < utc(row["outcome_available_at"]) <= utc(as_of):
            raise ValueError(
                "execution outcomes must follow submission and be known as of calibration"
            )
        quantity = decimal(row["quantity"])
        if quantity <= 0 or set(row["predicted_fills"]) != set(candidates):
            raise ValueError("positive order size and predictions for every candidate required")
        if any(
            not 0 <= decimal(value) <= quantity
            for value in [row["filled_quantity"], *row["predicted_fills"].values()]
        ):
            raise ValueError("actual/predicted fills must be within order quantity")
        rows.append(row)
    rows.sort(key=lambda row: (utc(row["submitted_at"]), row["order_id"]))
    boundary = utc(rows[int(len(rows) * 0.7)]["submitted_at"])
    train = [r for r in rows if utc(r["outcome_available_at"]) < boundary]
    evaluation = [r for r in rows if utc(r["submitted_at"]) >= boundary]
    if len(train) < 5 or len(evaluation) < 3:
        raise ValueError("purged chronological fit/evaluation are too small")

    def score(group, name):
        errors = [
            (decimal(r["predicted_fills"][name]) - decimal(r["filled_quantity"]))
            / decimal(r["quantity"])
            for r in group
        ]
        return {
            "n": len(errors),
            "mean_squared_fill_fraction_error": str(
                sum((e * e for e in errors), Decimal(0)) / len(errors)
            ),
            "mean_bias": str(sum(errors) / len(errors)),
        }

    scores = {name: score(train, name) for name in candidates}
    selected = min(
        candidates,
        key=lambda name: (decimal(scores[name]["mean_squared_fill_fraction_error"]), name),
    )
    return {
        "schema": "quant.fill-calibration/1",
        "evidence_kind": payload["evidence_kind"],
        "available_at": utc(as_of).isoformat(),
        "selected_candidate": selected,
        "selected_parameters": candidates[selected],
        "training_scores": scores,
        "evaluation": score(evaluation, selected),
        "evaluation_order_ids": [r["order_id"] for r in evaluation],
        "input_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        "limits": "Selection uses training executions only. Held-out scores are not refitted. "
        "Observed fills, aligned counterfactual predictions and vendor rights must be supplied; no venue certification.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="基于执行证据选择成交模拟参数")
    for key in ("input", "as-of", "output"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    report = calibrate(json.loads(Path(args.input).read_text("utf-8-sig")), args.as_of)
    with Path(args.output).open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
