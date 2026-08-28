from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp, spec
from quant_data_kit import AssetClass, MarginMode, MarkPriceEvent, StatusEvent, TradeEvent
from quant_data_kit.exceptions import ValidationError

from quant_execution import (
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderType,
    PortfolioRiskSnapshot,
    PositionRiskSnapshot,
    RiskCheckContext,
    RiskDecision,
    Side,
    TimeInForce,
)
from quant_execution.ledger import ExactAccountLedger
from quant_execution.rules import RuleBookRiskGate

SPOT = "crypto:test:BTCUSDT"
PERP = "crypto:test:BTCUSDT-PERP"
STOCK = "equity:sse:600000"


def _spot_spec():
    return spec(
        SPOT,
        asset_class=AssetClass.CRYPTO,
        product_type="spot",
        settlement_currency="USDT",
        base_currency="BTC",
        quote_currency="USDT",
        quantity_step="0.001",
        metadata={"min_quantity": "0.001"},
    )


def _perp_spec():
    return spec(
        PERP,
        asset_class=AssetClass.CRYPTO,
        product_type="linear_perpetual",
        settlement_currency="USDT",
        base_currency="BTC",
        quote_currency="USDT",
        quantity_step="0.001",
        margin_mode=MarginMode.CROSS,
        metadata={"initial_margin_rate": "0.10", "maintenance_margin_rate": "0.05"},
    )


def _stock_spec():
    return spec(
        STOCK,
        asset_class=AssetClass.EQUITY,
        product_type="a_share",
        settlement_currency="CNY",
        metadata={"lot_size": "100", "commission_rate": "0.0003"},
    )


def _mark(instrument_id: str, price: str, seconds: int) -> MarkPriceEvent:
    return MarkPriceEvent(
        **event_fields(f"mark:{instrument_id}:{seconds}", instrument_id, seconds=seconds),
        price=fp(price),
    )


def _fill(
    fill_id: str,
    instrument_id: str,
    side: Side,
    quantity: str,
    price: str,
    seconds: int,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=f"order:{fill_id}",
        account_id="account",
        strategy_id="strategy",
        instrument_id=instrument_id,
        side=side,
        quantity=fp(quantity, 3 if "." in quantity else 0),
        price=fp(price),
        event_time=T0 + timedelta(seconds=seconds),
        liquidity_role=LiquidityRole.TAKER,
    )


def _intent(quantity: str = "100") -> OrderIntent:
    return OrderIntent(
        idempotency_key=f"intent:{quantity}",
        account_id="account",
        strategy_id="strategy",
        instrument_id=STOCK,
        side=Side.BUY,
        quantity=fp(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("10"),
    )


def _stock_gate(*policies):
    stock = _stock_spec()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: stock},
        initial_cash={"CNY": fp("100000")},
    )
    gate = RuleBookRiskGate(
        instruments={STOCK: stock},
        ledger=ledger,
        policies=policies,
    )
    event = TradeEvent(
        **event_fields("trade:stock", STOCK),
        price=fp("10"),
        quantity=fp("1000"),
    )
    gate.observe(event)
    ledger.observe_market(event)
    return gate, ledger


def test_multi_currency_portfolio_risk_snapshot_is_exact_and_sorted() -> None:
    instruments = {SPOT: _spot_spec(), PERP: _perp_spec()}
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments=instruments,
        initial_cash={"USDT": fp("1000")},
        fx_to_base={"USDT": fp("0.5")},
    )
    ledger.mark(_mark(SPOT, "100", 1))
    ledger.apply(_fill("spot-buy", SPOT, Side.BUY, "2", "100", 1))
    ledger.mark(_mark(PERP, "200", 2))
    ledger.apply(_fill("perp-buy", PERP, Side.BUY, "1", "200", 2))

    view = ledger.portfolio_risk_snapshot(T0 + timedelta(seconds=2))

    assert view.nav.to_decimal() == Decimal(500)
    assert view.cash_value.to_decimal() == Decimal(400)
    assert view.gross_exposure.to_decimal() == Decimal(200)
    assert view.net_exposure.to_decimal() == Decimal(200)
    assert view.initial_margin.to_decimal() == Decimal(10)
    assert view.maintenance_margin.to_decimal() == Decimal(5)
    assert [position.instrument_id for position in view.positions] == sorted(instruments)
    by_id = {position.instrument_id: position for position in view.positions}
    assert by_id[SPOT].base_notional.to_decimal() == Decimal(100)
    assert by_id[PERP].base_notional.to_decimal() == Decimal(100)


def test_risk_snapshot_fails_when_derivative_margin_metadata_is_missing() -> None:
    broken = replace(
        _perp_spec(),
        metadata={"initial_margin_rate": "0.10"},
    )
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: broken},
        initial_cash={"USDT": fp("1000")},
    )
    ledger.mark(_mark(PERP, "100", 1))
    ledger.apply(_fill("perp-buy", PERP, Side.BUY, "1", "100", 1))

    with pytest.raises(ValidationError, match="maintenance_margin_rate"):
        ledger.portfolio_risk_snapshot(T0 + timedelta(seconds=1))


class _RejectLargeOrder:
    sends_live_orders = False

    def __init__(self, limit: Decimal = Decimal(999)) -> None:
        self.limit = limit
        self.order_calls = 0

    def check_order(self, order_intent, context):
        self.order_calls += 1
        assert context.instrument_spec.instrument_id == order_intent.instrument_id
        assert context.reference_price.to_decimal() == Decimal(10)
        if abs(context.projected_notional_base.to_decimal()) > self.limit:
            return RiskDecision(False, "GROSS_LIMIT", "projected order exceeds limit")
        return RiskDecision(True, "ACCEPTED")

    def runtime_check(self, context):
        assert context.instrument_spec is None
        assert context.reference_price is None
        assert context.projected_notional_base is None
        return RiskDecision(True, "ACCEPTED")


def test_rulebook_runs_builtin_checks_before_composed_policy() -> None:
    policy = _RejectLargeOrder()
    gate, ledger = _stock_gate(policy)

    assert gate.check(_intent("50"), ledger.snapshot()).code == "A_SHARE_LOT"
    assert policy.order_calls == 0
    decision = gate.check(_intent("100"), ledger.snapshot())
    assert decision.code == "GROSS_LIMIT"
    assert policy.order_calls == 1


class _RuntimeReject:
    sends_live_orders = False

    def check_order(self, order_intent, context):
        return RiskDecision(True, "ACCEPTED")

    def runtime_check(self, context):
        return RiskDecision(False, "STRESS_LIMIT", "stress loss exceeds limit")


class _AcceptAll:
    sends_live_orders = False

    def check_order(self, order_intent, context):
        return RiskDecision(True, "ACCEPTED")

    def runtime_check(self, context):
        return RiskDecision(True, "ACCEPTED")


def test_runtime_policy_is_applied_to_current_ledger_snapshot() -> None:
    gate, _ = _stock_gate(_RuntimeReject())
    assert gate.runtime_check_current(T0).code == "STRESS_LIMIT"


def test_accepting_policy_preserves_order_and_runtime_acceptance() -> None:
    gate, ledger = _stock_gate(_AcceptAll())
    assert gate.check(_intent(), ledger.snapshot()).accepted
    assert gate.runtime_check_current(T0).accepted


def test_policy_configuration_and_context_fail_closed() -> None:
    gate, ledger = _stock_gate()

    class MissingRuntime:
        sends_live_orders = False

        def check_order(self, order_intent, context):
            return RiskDecision(True, "ACCEPTED")

    with pytest.raises(ValidationError, match="must implement"):
        RuleBookRiskGate(
            instruments=gate.instruments,
            ledger=ledger,
            policies=[MissingRuntime()],
        )

    policy_gate = RuleBookRiskGate(
        instruments=gate.instruments,
        ledger=ledger,
        policies=[_AcceptAll()],
    )
    status = StatusEvent(
        **event_fields("status:stock", STOCK),
        status="open",
        reason="fixture",
    )
    policy_gate.observe(status)
    ledger.observe_market(status)
    assert policy_gate.check(_intent(), ledger.snapshot()).code == "RISK_CONTEXT_INVALID"


def test_runtime_context_failure_is_reported_without_skipping_policy() -> None:
    broken = replace(_perp_spec(), metadata={"initial_margin_rate": "0.10"})
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: broken},
        initial_cash={"USDT": fp("1000")},
    )
    mark = _mark(PERP, "100", 1)
    ledger.mark(mark)
    ledger.apply(_fill("perp-buy", PERP, Side.BUY, "1", "100", 1))
    gate = RuleBookRiskGate(
        instruments={PERP: broken},
        ledger=ledger,
        policies=[_AcceptAll()],
    )
    gate.observe(mark)
    assert gate.runtime_check_current(T0 + timedelta(seconds=1)).code == "RISK_CONTEXT_INVALID"


class _BrokenPolicy:
    sends_live_orders = False

    def __init__(self, result=None, *, raises: bool = False) -> None:
        self.result = result
        self.raises = raises

    def check_order(self, order_intent, context):
        if self.raises:
            raise RuntimeError("broken policy")
        return self.result

    def runtime_check(self, context):
        if self.raises:
            raise RuntimeError("broken policy")
        return self.result


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (_BrokenPolicy(raises=True), "RISK_POLICY_ERROR"),
        (_BrokenPolicy(result="accepted"), "RISK_POLICY_INVALID"),
    ],
)
def test_policy_exceptions_and_invalid_results_fail_closed(policy, expected) -> None:
    gate, ledger = _stock_gate(policy)
    assert gate.check(_intent(), ledger.snapshot()).code == expected
    assert gate.runtime_check_current(T0).code == expected


def test_missing_fx_and_live_policy_capability_fail_closed() -> None:
    stock = _stock_spec()
    usd_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments={STOCK: stock},
        initial_cash={"USD": fp("1000")},
    )
    policy = _RejectLargeOrder()
    gate = RuleBookRiskGate(instruments={STOCK: stock}, ledger=usd_ledger, policies=[policy])
    event = TradeEvent(
        **event_fields("trade:stock", STOCK),
        price=fp("10"),
        quantity=fp("1000"),
    )
    gate.observe(event)
    usd_ledger.observe_market(event)
    supplied = replace(
        usd_ledger.snapshot(),
        cash_balances={"CNY": fp("100000")},
    )
    assert gate.check(_intent(), supplied).code == "RISK_CONTEXT_INVALID"

    policy.sends_live_orders = True
    with pytest.raises(ValidationError, match="cannot send live orders"):
        RuleBookRiskGate(instruments={STOCK: stock}, ledger=usd_ledger, policies=[policy])


def test_risk_snapshot_and_context_contracts_reject_invalid_shapes() -> None:
    position = PositionRiskSnapshot(
        instrument_id=STOCK,
        asset_class=AssetClass.EQUITY,
        venue="TEST",
        settlement_currency="CNY",
        quantity=fp("100"),
        mark_price=fp("10"),
        base_notional=fp("1000"),
        initial_margin=fp("0"),
        maintenance_margin=fp("0"),
    )
    account = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: _stock_spec()},
        initial_cash={"CNY": fp("1000")},
    ).snapshot(T0)
    portfolio = PortfolioRiskSnapshot(
        account_id="account",
        event_time=T0,
        base_currency="CNY",
        nav=fp("1000"),
        cash_value=fp("1000"),
        gross_exposure=fp("1000"),
        net_exposure=fp("1000"),
        initial_margin=fp("0"),
        maintenance_margin=fp("0"),
        positions=(position,),
    )
    with pytest.raises(ValidationError, match="all present or all absent"):
        RiskCheckContext(
            account_snapshot=account,
            portfolio_snapshot=portfolio,
            instrument_spec=_stock_spec(),
        )
    with pytest.raises(ValidationError, match="same account and time"):
        RiskCheckContext(
            account_snapshot=account,
            portfolio_snapshot=replace(portfolio, account_id="other"),
        )
    with pytest.raises(ValidationError, match="sorted"):
        replace(portfolio, positions=(replace(position, instrument_id="z"), position))
