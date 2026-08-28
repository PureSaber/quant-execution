from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp, spec
from quant_data_kit import (
    AssetClass,
    CorporateActionEvent,
    FundingRateEvent,
    MarginMode,
    MarkPriceEvent,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution import Fee, Fill, LiquidityRole, Settlement, Side
from quant_execution.ledger import ExactAccountLedger

SPOT = "crypto:test:BTCUSDT"
PERP = "crypto:test:BTCUSDT-PERP"
STOCK = "equity:sse:600000"


def spot_spec():
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


def perp_spec():
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


def stock_spec():
    return spec(
        STOCK,
        asset_class=AssetClass.EQUITY,
        product_type="a_share",
        settlement_currency="CNY",
        metadata={"lot_size": "100", "stamp_duty_rate": "0.001"},
    )


def fill(
    fill_id: str,
    instrument_id: str,
    side: Side,
    quantity: str,
    price: str,
    *,
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


def mark(instrument_id: str, price: str, seconds: int) -> MarkPriceEvent:
    return MarkPriceEvent(
        **event_fields(f"mark:{instrument_id}:{seconds}", instrument_id, seconds=seconds),
        price=fp(price),
    )


def test_spot_ledger_derives_cash_cost_realized_unrealized_nav_and_is_idempotent() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={SPOT: spot_spec()},
        initial_cash={"USDT": fp("1000")},
    )
    ledger.mark(mark(SPOT, "100", 1))
    buy = fill("buy", SPOT, Side.BUY, "2", "100", seconds=1)
    first = ledger.apply(buy)
    assert first.cash_balances["USDT"].to_decimal() == Decimal(800)
    assert first.positions[SPOT].to_decimal() == Decimal(2)
    assert first.nav.to_decimal() == Decimal(1000)
    assert ledger.apply(buy) == first
    ledger.mark(mark(SPOT, "110", 2))
    sold = ledger.apply(fill("sell", SPOT, Side.SELL, "1", "110", seconds=2))
    assert sold.realized_pnl[SPOT].to_decimal() == Decimal(10)
    assert sold.unrealized_pnl[SPOT].to_decimal() == Decimal(10)
    assert sold.nav.to_decimal() == Decimal(1020)
    after_fee = ledger.apply(
        Fee(
            fee_id="fee-1",
            fill_id="sell",
            account_id="account",
            amount=fp("1"),
            currency="USDT",
            event_time=T0 + timedelta(seconds=2),
            fee_type="taker",
        )
    )
    assert after_fee.nav.to_decimal() == Decimal(1019)
    assert all(
        sum(
            posting.amount.to_decimal()
            for posting in transaction.postings
            if posting.currency == currency
        )
        == 0
        for transaction in ledger.transactions
        for currency in {posting.currency for posting in transaction.postings}
    )
    conflicting = fill("buy", SPOT, Side.BUY, "2", "101", seconds=1)
    with pytest.raises(ValidationError, match="different content"):
        ledger.apply(conflicting)


def test_perpetual_funding_settlement_margin_and_liquidation_boundary() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: perp_spec()},
        initial_cash={"USDT": fp("1000")},
    )
    ledger.mark(mark(PERP, "100", 1))
    ledger.apply(fill("open", PERP, Side.BUY, "20", "100", seconds=1))
    ledger.mark(mark(PERP, "110", 2))
    snapshot = ledger.snapshot()
    assert snapshot.unrealized_pnl[PERP].to_decimal() == Decimal(200)
    assert snapshot.initial_margin.to_decimal() == Decimal(220)
    funding_event = FundingRateEvent(
        **event_fields("funding-event", PERP, seconds=3),
        rate=0.01,
        interval_start=T0,
        interval_end=T0 + timedelta(hours=8),
    )
    funding = ledger.funding_from_market(funding_event)
    assert funding is not None and funding.amount.to_decimal() == Decimal(-22)
    ledger.apply(funding)
    settled = ledger.apply(
        Settlement(
            settlement_id="settle-1",
            account_id="account",
            instrument_id=PERP,
            amount=fp("200"),
            currency="USDT",
            event_time=T0 + timedelta(seconds=4),
            settlement_type="daily_mark",
            settlement_price=fp("110"),
        )
    )
    assert settled.nav.to_decimal() == Decimal(1178)
    assert settled.unrealized_pnl[PERP].to_decimal() == 0
    ledger.mark(mark(PERP, "1", 5))
    crashed = ledger.snapshot()
    assert crashed.liquidation_required


def test_corporate_action_multi_currency_fx_and_night_trading_day() -> None:
    stock = stock_spec()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments={STOCK: stock},
        initial_cash={"CNY": fp("10000"), "USD": fp("100")},
        fx_to_base={"CNY": fp("0.14", 2)},
    )
    ledger.mark(mark(STOCK, "10", 1))
    bought = fill("stock-buy", STOCK, Side.BUY, "100", "10", seconds=1)
    trading_day = date(2026, 1, 3)
    ledger.apply_with_trading_day(bought, trading_day=trading_day)
    assert ledger.acquired_today(STOCK, trading_day) == Decimal(100)
    action = CorporateActionEvent(
        **event_fields("action", STOCK, seconds=2, trading_day=trading_day),
        action_type="split_and_dividend",
        effective_date=trading_day,
        ratio=fp("2"),
        cash_amount=fp("1"),
        currency="CNY",
    )
    ledger.apply(action)
    ledger.mark(mark(STOCK, "5", 3))
    snapshot = ledger.snapshot()
    assert snapshot.positions[STOCK].to_decimal() == Decimal(200)
    assert snapshot.cash_balances["CNY"].to_decimal() == Decimal(9100)
    assert snapshot.nav.to_decimal() == Decimal(1514)


def test_missing_fx_is_fail_closed() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments={},
        initial_cash={"CNY": fp("100")},
    )
    with pytest.raises(ValidationError, match="missing FX"):
        ledger.snapshot()
