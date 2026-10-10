from dataclasses import replace
from decimal import Decimal

import pytest
from quant_data_kit.derivatives import write_bundle
from quant_data_kit.derivatives.demo import write_demo

from quant_execution.derivative_replay import replay, verify_artifacts, write_artifacts
from quant_execution.derivatives import DerivativeAccount


def test_next_bar_fills_artifact_balance_and_tamper_rejection(tmp_path):
    bundle = write_demo(tmp_path / "bundle", "future")
    config = {
        "initial_cash": 10000,
        "end": "2025-01-15",
        "fee_per_contract": 1,
        "slippage_ticks": 1,
    }
    result = replay(bundle, config, lambda quotes, at: {"DEMO:H": 1})
    assert result["fills"][0]["at"].startswith("2025-01-03")
    assert result["decisions"][0]["decision_at"] < result["fills"][0]["at"]
    assert result["final_account"]["positions"]["DEMO:H"] == "0"
    study = {"title": "<unsafe>", "input_sha256": bundle.identity, "evidence_kind": "synthetic"}
    output = tmp_path / "report"
    write_artifacts(
        output, study, {name + ".csv": result[name] for name in ("nav", "fills", "ledger")}
    )
    assert verify_artifacts(output)["status"] == "passed"
    assert "&lt;unsafe&gt;" in (output / "report.html").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_artifacts(output, study, {})
    (output / "nav.csv").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        verify_artifacts(output)


def test_missing_liquidity_and_unavailable_future_decisions(tmp_path):
    bundle = write_demo(tmp_path / "bundle", "future")
    broken = write_bundle(
        tmp_path / "illiquid",
        bundle.contracts,
        [replace(q, volume=0) if q.session == "2025-01-03" else q for q in bundle.quotes],
        provider="test",
        evidence_kind="synthetic",
        rights_note="test",
        limits="test",
    )
    with pytest.raises(ValueError, match="liquid"):
        replay(broken, {"initial_cash": 10000}, lambda quotes, at: {"DEMO:H": 1})
    with pytest.raises(ValueError, match="unavailable"):
        replay(bundle, {"initial_cash": 10000}, lambda quotes, at: {"INVENTED": 1})


def test_short_premium_reversal_balances_and_liquidation_boundary():
    from quant_data_kit.derivatives.demo import demo_records

    contracts, quotes = demo_records("option")
    c, at = contracts[0], quotes[0].at
    account = DerivativeAccount([c], 1000, at)
    account.trade_batch([(c.instrument_id, -2, 10)], at, "short")
    assert Decimal(account.snapshot()["cash"]) == 1200
    account.trade_batch([(c.instrument_id, 3, 8)], at, "reverse")
    assert account.quantity(c.instrument_id) == 1
    assert Decimal(account.snapshot()["cash"]) == 960
    assert Decimal(account.snapshot()["nav"]) == 1040
    account.validate_balance()
    short = DerivativeAccount([c], 1000, at)
    short.trade_batch([(c.instrument_id, -2, 2)], at, "open")
    short.mark(c.instrument_id, 30, at, "loss")
    assert short.ledger.liquidation_required()
    assert short.snapshot()["margin_breach"]


def test_short_physical_assignment_reports_borrow_boundary():
    from quant_data_kit.derivatives.demo import demo_records

    contracts, quotes = demo_records("option")
    c = replace(contracts[0], settlement="physical")
    account = DerivativeAccount([c], 10000, quotes[0].at)
    account.trade_batch([(c.instrument_id, -1, 2)], quotes[0].at, "short")
    account.exercise(c.instrument_id, 110, c.expiry, "assignment")
    assert account.quantity(c.underlying) == -10
    assert account.snapshot()["margin_breach"]
    assert Decimal(account.snapshot()["nav"]) == 9870
    account.validate_balance()
