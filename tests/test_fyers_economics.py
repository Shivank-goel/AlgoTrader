from datetime import date
import pytest

from src.fyers.costs import FyersCosts
from src.fyers.economics import estimate_fill, maximum_buy_quantity, settlement_day
from src.fyers.models import Instrument, Quote, Side


@pytest.fixture
def market():
    instrument = Instrument(symbol="NSE:TEST-EQ", isin="TEST", lot_size=1, tick_size="0.05")
    quote = Quote(symbol=instrument.symbol, bid=99, ask=100, bid_size=3, ask_size=2,
                  exchange_time=1000, received_time=1000)
    return instrument, quote, FyersCosts.load()


def test_conservative_fill_is_shared_and_never_invents_liquidity(market):
    instrument, quote, costs = market
    unfilled = estimate_fill(side=Side.BUY, quantity=3, instrument=instrument,
                             quote=quote, slippage_bps=2, costs=costs)
    partial = estimate_fill(side=Side.BUY, quantity=3, instrument=instrument,
                            quote=quote, slippage_bps=2, costs=costs, allow_partial=True)
    assert unfilled.status == "UNFILLED" and unfilled.filled_quantity == 0
    assert partial.status == "PARTIALLY_FILLED" and partial.filled_quantity == 2
    assert partial.price == 100.05
    assert partial.cash_required == pytest.approx(partial.notional + partial.fee)


def test_whole_share_sizing_reserves_fees(market):
    instrument, quote, costs = market
    one = estimate_fill(side=Side.BUY, quantity=1, instrument=instrument,
                        quote=quote, slippage_bps=2, costs=costs)
    assert maximum_buy_quantity(cash=one.cash_required, max_notional=1000,
                                instrument=instrument, quote=quote,
                                slippage_bps=2, costs=costs) == 1
    assert maximum_buy_quantity(cash=one.cash_required - .01, max_notional=1000,
                                instrument=instrument, quote=quote,
                                slippage_bps=2, costs=costs) == 0


def test_tariff_has_effective_date_provenance():
    assert FyersCosts.load().provenance == {
        "tariff_version": "fyers-nse-cash-2024-10-01", "effective_from": "2024-10-01"}


def test_t_plus_one_skips_weekends_and_explicit_holidays():
    assert settlement_day(date(2026, 9, 18)) == date(2026, 9, 21)
    assert settlement_day(date(2026, 9, 18), holidays=frozenset({date(2026, 9, 21)})) == date(2026, 9, 22)
