"""PIT book cost must not depend on a representable per-unit average."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import FixedPoint, StatusEvent
from quant_data_kit.exceptions import ValidationError
from test_hong_kong import instrument, schedule
from test_ledger import PERP, STOCK, fill, mark, perp_spec, stock_spec

from quant_execution import Settlement, Side
from quant_execution._fixed import fixed_fraction_half_even
from quant_execution.artifacts import export_dividend_run, replay_dividend_run
from quant_execution.dividends import DividendExecutionMode, FxValuationMode
from quant_execution.hong_kong import HKDailyExecution
from quant_execution.ledger import ExactAccountLedger


def account(*, derivative=False, money_scale=8):
    instrument_spec = (
        replace(perp_spec(), settlement_currency="USD", quote_currency="USD")
        if derivative
        else stock_spec()
    )
    return ExactAccountLedger(
        account_id="account",
        base_currency=instrument_spec.settlement_currency,
        instruments={instrument_spec.instrument_id: instrument_spec},
        initial_cash={instrument_spec.settlement_currency: fp(1000000)},
        money_scale=money_scale,
        opened_at=T0,
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )


def book_cost(ledger, symbol):
    return sum(
        (
            p.amount.to_decimal()
            for tx in ledger.transactions
            for p in tx.postings
            if p.instrument_id == symbol
            and p.ledger_account in {"assets:position_cost", "memo:position_cost"}
        ),
        Decimal(0),
    )


@pytest.mark.parametrize("second_price,reported", [("101", "100.67"), ("100.01", "100.01")])
def test_mixed_lots_report_average_but_value_exact_total_cost(second_price, reported):
    ledger = account()
    ledger.apply(fill("a", STOCK, Side.BUY, "1", "100", seconds=1))
    ledger.apply(fill("b", STOCK, Side.BUY, "2", second_price, seconds=2))
    unmarked = ledger.snapshot()
    assert unmarked.cost_basis[STOCK] == fp(reported)
    assert unmarked.nav.to_decimal() == 1000000
    assert unmarked.unrealized_pnl[STOCK].units == 0
    assert ledger.risk_balances(T0 + timedelta(seconds=2))[2] == 1000000
    ledger.mark(mark(STOCK, "102", 3))
    snapshot = ledger.snapshot()
    expected = Decimal(306) - book_cost(ledger, STOCK)
    assert snapshot.unrealized_pnl[STOCK].to_decimal() == expected
    assert snapshot.nav.to_decimal() == 1000000 + expected
    assert ledger.portfolio_risk_snapshot(snapshot.event_time).nav == snapshot.nav
    assert not ledger.liquidation_required()


@pytest.mark.parametrize("second_price,reported", [("100.01", "100.00"), ("100.03", "100.02")])
def test_finite_subtick_average_rounds_half_even_for_display(second_price, reported):
    ledger = account()
    ledger.apply(fill("a", STOCK, Side.BUY, "1", "100", seconds=1))
    ledger.apply(fill("b", STOCK, Side.BUY, "1", second_price, seconds=2))
    assert ledger.snapshot().cost_basis[STOCK] == fp(reported)
    assert ledger.snapshot().nav.to_decimal() == 1000000


def test_hk_4300_shares_partial_and_final_sale_keep_book_cost_and_t2(tmp_path):
    broker = HKDailyExecution(
        {"00700": instrument()},
        initial_cash=fp(1000000),
        opened_at=T0,
        fees=schedule(),
        settlement_days=[
            T0.date(),
            (T0 + timedelta(days=3)).date(),
            (T0 + timedelta(days=4)).date(),
        ],
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )
    fees = Decimal(0)
    for n, (side, quantity, price) in enumerate(
        [
            (Side.BUY, 100, "100"),
            (Side.BUY, 4200, "101"),
            (Side.SELL, 100, "102"),
            (Side.SELL, 4200, "102"),
        ]
    ):
        at = T0 + timedelta(seconds=n)
        broker.mark("00700", fp(price), at)
        _, charges = broker.execute(
            order_id=str(n), symbol="00700", quantity=quantity, side=side, price=fp(price), at=at
        )
        fees += sum((v.to_decimal() for v in charges.values()), Decimal(0))
        snapshot = broker.ledger.snapshot()
        if n == 1:
            assert snapshot.positions["00700"].to_decimal() == 4300
            assert snapshot.cost_basis["00700"] == fp("100.98")
        if n == 2:
            assert book_cost(broker.ledger, "00700") == Decimal("424102.32558140")
            assert snapshot.realized_pnl["00700"].to_decimal() == Decimal("102.32558140")
            assert snapshot.unrealized_pnl["00700"].to_decimal() == Decimal("4297.67441860")
    assert book_cost(broker.ledger, "00700") == 0
    assert snapshot.realized_pnl["00700"].to_decimal() == 4400
    assert snapshot.nav.to_decimal() == 1004400 - fees
    assert broker.available_cash() < broker.ledger.cash_balance("HKD")
    broker.settle_end_of_day((T0 + timedelta(days=4)).date())
    assert broker.available_cash() == broker.ledger.cash_balance("HKD")
    for tx in broker.ledger.transactions:
        assert sum((p.amount.to_decimal() for p in tx.postings), Decimal(0)) == 0
    broker.ledger.record_dividend_valuation(as_of=snapshot.event_time)
    stored = export_dividend_run(broker.ledger, tmp_path / "hk-cost")
    replayed = replay_dividend_run(stored).ledger
    assert replayed.journal_sha256 == broker.ledger.journal_sha256
    assert replayed.snapshot() == snapshot


@pytest.mark.parametrize("side,sign", [(Side.BUY, 1), (Side.SELL, -1)])
@pytest.mark.parametrize("close_quantity", ["1", "3", "4"])
def test_derivative_partial_full_and_reversing_close_preserve_total_cost(
    side, sign, close_quantity, tmp_path
):
    ledger = account(derivative=True)
    ledger.apply(fill("a", PERP, side, "1", "100", seconds=1))
    ledger.apply(fill("b", PERP, side, "2", "101", seconds=2))
    assert ledger.snapshot().nav.to_decimal() == 1000000
    ledger.mark(mark(PERP, "102", 3))
    assert ledger.snapshot().unrealized_pnl[PERP].to_decimal() == sign * 4
    closing_side = Side.SELL if side is Side.BUY else Side.BUY
    snapshot = ledger.apply(fill("c", PERP, closing_side, close_quantity, "102", seconds=3))
    expected_cost = {"1": Decimal("201.33333333"), "3": Decimal(0), "4": Decimal(-102)}
    assert book_cost(ledger, PERP) == sign * expected_cost[close_quantity]
    assert snapshot.nav.to_decimal() == 1000000 + sign * 4
    expected_realized = Decimal("1.33333333") if close_quantity == "1" else Decimal(4)
    assert snapshot.realized_pnl[PERP].to_decimal() == sign * expected_realized
    assert ledger.risk_balances(snapshot.event_time)[2] == snapshot.nav.to_decimal()
    assert ledger.portfolio_risk_snapshot(snapshot.event_time).nav == snapshot.nav
    assert not ledger.liquidation_required()
    ledger.record_dividend_valuation(as_of=snapshot.event_time)
    replayed = replay_dividend_run(export_dividend_run(ledger, tmp_path / "derivative-cost")).ledger
    assert replayed.snapshot() == snapshot
    assert replayed.journal_sha256 == ledger.journal_sha256


@pytest.mark.parametrize("side,sign", [(Side.BUY, 1), (Side.SELL, -1)])
def test_derivative_daily_mark_uses_total_cost(side, sign):
    ledger = account(derivative=True)
    ledger.apply(fill("a", PERP, side, "1", "100", seconds=1))
    ledger.apply(fill("b", PERP, side, "2", "101", seconds=2))
    settled = ledger.apply(
        Settlement(
            settlement_id="settle",
            account_id="account",
            instrument_id=PERP,
            amount=fp(sign * 4),
            currency="USD",
            event_time=T0 + timedelta(seconds=3),
            settlement_type="daily_mark",
            settlement_price=fp("102"),
        )
    )
    assert settled.nav.to_decimal() == 1000000 + sign * 4
    assert settled.unrealized_pnl[PERP].units == 0
    assert book_cost(ledger, PERP) == sign * 306


def test_reporting_nonterminating_cost_is_independent_of_decimal_context():
    ledger = account()
    ledger.apply(fill("a", STOCK, Side.BUY, "1", "100", seconds=1))
    ledger.apply(fill("b", STOCK, Side.BUY, "2", "101", seconds=2))
    ledger.mark(mark(STOCK, "102", 3))
    expected = ledger.snapshot()
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        assert ledger.snapshot() == expected
        assert ledger.risk_balances(expected.event_time)[2] == Decimal(1000004)
        assert ledger.portfolio_risk_snapshot(expected.event_time).nav == expected.nav
        assert not ledger.liquidation_required()


@pytest.mark.parametrize("second_price,removed", [("100.01", "100.00"), ("100.03", "100.02")])
def test_partial_allocation_half_even_keeps_residue_until_final_sale(second_price, removed):
    ledger = account(money_scale=2)
    ledger.apply(fill("a", STOCK, Side.BUY, "1", "100", seconds=1))
    ledger.apply(fill("b", STOCK, Side.BUY, "1", second_price, seconds=2))
    original_cost = book_cost(ledger, STOCK)
    first = ledger.apply(fill("c", STOCK, Side.SELL, "1", "102", seconds=3))
    assert book_cost(ledger, STOCK) == original_cost - Decimal(removed)
    assert first.realized_pnl[STOCK].to_decimal() == 102 - Decimal(removed)
    last = ledger.apply(fill("d", STOCK, Side.SELL, "1", "102", seconds=4))
    assert book_cost(ledger, STOCK) == 0
    assert last.realized_pnl[STOCK].to_decimal() == 204 - original_cost
    assert last.nav.to_decimal() == 1000000 + 204 - original_cost


def test_fractional_quantity_and_multiplier_are_allocated_from_total_cost():
    instrument_spec = replace(
        stock_spec(), quantity_step=fp("0.001", 3), contract_multiplier=fp(10)
    )
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: instrument_spec},
        initial_cash={"CNY": fp(1000000)},
        opened_at=T0,
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )
    ledger.apply(fill("a", STOCK, Side.BUY, "0.100", "100", seconds=1))
    ledger.apply(fill("b", STOCK, Side.BUY, "0.200", "101", seconds=2))
    ledger.mark(mark(STOCK, "102", 3))
    first = ledger.apply(fill("c", STOCK, Side.SELL, "0.100", "102", seconds=3))
    assert first.realized_pnl[STOCK].to_decimal() == Decimal("1.33333333")
    last = ledger.apply(fill("d", STOCK, Side.SELL, "0.200", "102", seconds=4))
    assert book_cost(ledger, STOCK) == 0
    assert last.realized_pnl[STOCK].to_decimal() == 4


def test_generated_daily_settlement_after_partial_close_uses_remaining_cost():
    ledger = account(derivative=True)
    ledger.apply(fill("a", PERP, Side.BUY, "1", "100", seconds=1))
    ledger.apply(fill("b", PERP, Side.BUY, "2", "101", seconds=2))
    ledger.mark(mark(PERP, "102", 3))
    ledger.apply(fill("c", PERP, Side.SELL, "1", "102", seconds=3))
    ledger.mark(mark(PERP, "102", 4))
    event = StatusEvent(**event_fields("settle-status", PERP, seconds=4), status="daily_settlement")
    settlement = ledger.settlement_from_market(event)
    assert settlement.amount.to_decimal() == Decimal("2.66666667")
    snapshot = ledger.apply(settlement)
    assert snapshot.unrealized_pnl[PERP].units == 0
    assert snapshot.nav.to_decimal() == 1000004


def test_display_rounding_does_not_relax_exact_nav_precision_and_rollback():
    ledger = account(money_scale=2)
    ledger.apply(fill("a", STOCK, Side.BUY, "1", "100", seconds=1))
    before = ledger.capture_state()
    with pytest.raises(ValidationError, match="not exact at scale"):
        ledger.mark(replace(mark(STOCK, "100", 2), price=fp("100.001", 3)))
    assert ledger.capture_state() == before


def test_fraction_rounding_uses_integer_half_even_and_rejects_invalid_inputs():
    for numerator, expected in [(20001, 10000), (20003, 10002), (-20001, -10000), (-20003, -10002)]:
        assert fixed_fraction_half_even(Fraction(numerator, 200), 2) == FixedPoint(expected, 2)
    assert fixed_fraction_half_even(Fraction(1, 3), 8) == FixedPoint(33333333, 8)
    assert fixed_fraction_half_even(Fraction(2, 3), 8) == FixedPoint(66666667, 8)
    with pytest.raises(ValidationError, match="Fraction"):
        fixed_fraction_half_even(Decimal(1), 2)
    for scale in (-1, 19, True):
        with pytest.raises(ValidationError):
            fixed_fraction_half_even(Fraction(1), scale)
    with pytest.raises(ValidationError):
        fixed_fraction_half_even(Fraction(2**63), 0)
