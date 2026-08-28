from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import (
    BookLevel,
    BookSnapshotEvent,
    CorporateActionEvent,
    QuoteEvent,
    StatusEvent,
)
from quant_data_kit.exceptions import ValidationError
from test_ledger import PERP, SPOT, STOCK, fill, mark, perp_spec, spot_spec, stock_spec

from quant_execution import (
    Fee,
    Funding,
    LedgerEventType,
    LedgerTransaction,
    Posting,
    Side,
)
from quant_execution.ledger import ExactAccountLedger, _meta_decimal


def test_ledger_configuration_metadata_and_fx_fail_closed() -> None:
    with pytest.raises(ValidationError, match="account_id"):
        ExactAccountLedger(account_id="", base_currency="USD", instruments={})
    with pytest.raises(ValidationError, match="money_scale"):
        ExactAccountLedger(
            account_id="account", base_currency="USD", instruments={}, money_scale=19
        )
    bad = replace(perp_spec(), metadata={"initial_margin_rate": "bad"})
    with pytest.raises(ValidationError, match="decimal"):
        _meta_decimal(bad, "initial_margin_rate")
    infinite = replace(perp_spec(), metadata={"initial_margin_rate": "Infinity"})
    with pytest.raises(ValidationError, match="finite"):
        _meta_decimal(infinite, "initial_margin_rate")
    ledger = ExactAccountLedger(account_id="account", base_currency="USD", instruments={})
    with pytest.raises(ValidationError, match="positive"):
        ledger.set_fx_rate("CNY", fp("0"), event_time=T0)
    ledger.set_fx_rate("CNY", fp("0.14", 2), event_time=T0)
    with pytest.raises(ValidationError, match="backwards"):
        ledger.set_fx_rate("CNY", fp("0.15", 2), event_time=T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="not available"):
        ledger._to_base(Decimal(1), "CNY", T0 - timedelta(seconds=1))


def test_mark_identity_time_and_market_event_price_sources() -> None:
    registry = {SPOT: spot_spec()}
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100")},
    )
    unknown = mark("unknown", "1", 1)
    with pytest.raises(ValidationError, match="missing InstrumentSpec"):
        ledger.mark(unknown)
    first = mark(SPOT, "100", 2)
    assert ledger.mark(first) == ledger.mark(first)
    conflict = replace(first, price=fp("101"))
    with pytest.raises(ValidationError, match="different content"):
        ledger.mark(conflict)
    with pytest.raises(ValidationError, match="backwards"):
        ledger.mark(mark(SPOT, "99", 1))
    quote = QuoteEvent(
        **event_fields("quote", SPOT, seconds=3),
        bid_price=fp("99"),
        bid_quantity=fp("1"),
        ask_price=fp("101"),
        ask_quantity=fp("1"),
    )
    ledger.observe_market(quote)
    book = BookSnapshotEvent(
        **event_fields("book", SPOT, seconds=4, sequence=1),
        bids=(BookLevel(fp("100"), fp("1")),),
        asks=(BookLevel(fp("102"), fp("1")),),
    )
    ledger.observe_market(book)
    status = StatusEvent(**event_fields("status", SPOT, seconds=5), status="open", reason="")
    assert ledger.observe_market(status).event_time == status.available_at


def test_trading_day_idempotency_account_spec_and_cash_short_guards() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={SPOT: spot_spec()},
        initial_cash={"USDT": fp("1000")},
    )
    ledger.mark(mark(SPOT, "100", 1))
    bought = fill("buy", SPOT, Side.BUY, "1", "100", seconds=1)
    ledger.apply_with_trading_day(bought, trading_day=date(2026, 1, 2))
    with pytest.raises(ValidationError, match="trading_day changed"):
        ledger.apply_with_trading_day(bought, trading_day=date(2026, 1, 3))
    wrong_account = replace(
        fill("wrong", SPOT, Side.BUY, "1", "100", seconds=2), account_id="other"
    )
    with pytest.raises(ValidationError, match="account"):
        ledger.apply(wrong_account)
    unknown = replace(
        fill("unknown", SPOT, Side.BUY, "1", "100", seconds=2), instrument_id="unknown"
    )
    with pytest.raises(ValidationError, match="missing InstrumentSpec"):
        ledger.apply(unknown)
    with pytest.raises(ValidationError, match="short position"):
        ledger.apply(fill("short", SPOT, Side.SELL, "2", "100", seconds=2))


def test_non_fill_ledger_events_are_account_fill_instrument_and_currency_scoped() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={SPOT: spot_spec(), PERP: perp_spec()},
        initial_cash={"USDT": fp("1000")},
    )
    orphan_fee = Fee(
        fee_id="fee-orphan",
        fill_id="missing",
        account_id="account",
        amount=fp("1"),
        currency="USDT",
        event_time=T0,
        fee_type="taker",
    )
    with pytest.raises(ValidationError, match="fill not yet applied"):
        ledger.apply(orphan_fee)
    wrong_account = Funding(
        funding_id="wrong-account",
        account_id="other",
        instrument_id=PERP,
        amount=fp("1"),
        currency="USDT",
        event_time=T0,
    )
    with pytest.raises(ValidationError, match="account differs"):
        ledger.apply(wrong_account)
    wrong_currency = replace(wrong_account, account_id="account", currency="CNY")
    with pytest.raises(ValidationError, match="settlement currency"):
        ledger.apply(wrong_currency)


def test_derivative_long_short_partial_close_flatten_flip_and_today_lots() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: perp_spec()},
        initial_cash={"USDT": fp("10000")},
    )
    ledger.mark(mark(PERP, "100", 1))
    day = date(2026, 1, 3)
    short1 = fill("short-1", PERP, Side.SELL, "3", "100", seconds=1)
    short2 = fill("short-2", PERP, Side.SELL, "2", "110", seconds=2)
    ledger.apply_with_trading_day(short1, trading_day=day)
    ledger.apply_with_trading_day(short2, trading_day=day)
    assert ledger.acquired_today(PERP, day) == Decimal(5)
    partial = ledger.apply(fill("cover-1", PERP, Side.BUY, "1", "90", seconds=3))
    assert partial.positions[PERP].to_decimal() == Decimal(-4)
    assert partial.realized_pnl[PERP].to_decimal() > 0
    flat = ledger.apply(fill("cover-rest", PERP, Side.BUY, "4", "100", seconds=4))
    assert flat.positions[PERP].to_decimal() == 0
    ledger.apply(fill("long", PERP, Side.BUY, "1", "100", seconds=5))
    flipped = ledger.apply(fill("flip", PERP, Side.SELL, "2", "110", seconds=6))
    assert flipped.positions[PERP].to_decimal() == Decimal(-1)
    assert flipped.cost_basis[PERP].to_decimal() == Decimal(110)


def test_cash_only_corporate_action_residual_and_internal_guards() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: stock_spec()},
        initial_cash={"CNY": fp("10000")},
    )
    ledger.mark(mark(STOCK, "10", 1))
    ledger.apply(fill("stock", STOCK, Side.BUY, "100", "10", seconds=1))
    cash_only = CorporateActionEvent(
        **event_fields("cash-only", STOCK, seconds=2),
        action_type="cash_dividend",
        effective_date=T0.date(),
        cash_amount=fp("1"),
        currency="CNY",
    )
    snapshot = ledger.apply(cash_only)
    assert snapshot.cash_balances["CNY"].to_decimal() == Decimal(9100)
    corrupted = replace(snapshot, nav=fp("999999"))
    with pytest.raises(ValidationError, match="NAV residual"):
        ledger.assert_nav_residual(corrupted)
    duplicate = LedgerTransaction(
        transaction_id="duplicate",
        idempotency_key=ledger.transactions[0].idempotency_key,
        event_time=T0,
        event_type=LedgerEventType.FEE,
        reference_id="duplicate",
        postings=(
            Posting(ledger_account="assets:cash", currency="CNY", amount=fp("1")),
            Posting(ledger_account="expenses:test", currency="CNY", amount=fp("-1")),
        ),
    )
    with pytest.raises(ValidationError, match="duplicate ledger"):
        ledger._post(duplicate)
    with pytest.raises(ValidationError, match="unsupported ledger event"):
        ledger._translate(object())
    with pytest.raises(ValidationError, match="stable identity"):
        ledger._event_identity(object())
    with pytest.raises(ValidationError, match="missing mark"):
        ledger._mark_price("never-marked")
