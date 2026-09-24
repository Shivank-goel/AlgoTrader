from src.fyers.costs import FyersCosts
from src.fyers.journal import Journal
from src.fyers.models import Instrument, Quote, RuntimeConfig
from src.fyers.paper import PaperBroker
from src.fyers.sizing import DynamicShadowSizer, SizingConfig


def _sizer(tmp_path):
    config = RuntimeConfig.load().model_copy(update={"database": str(tmp_path / "runtime.db")})
    journal = Journal(tmp_path / "runtime.db")
    paper = PaperBroker(journal, config, FyersCosts.load())
    instrument = Instrument(symbol="NSE:SBIN-EQ", isin="INE062A01020", lot_size=1, tick_size="0.1")
    quote = Quote(symbol=instrument.symbol, bid=100, ask=100, bid_size=100, ask_size=100,
                  received_time=1000, exchange_time=1000)
    policy = SizingConfig(risk_per_trade_fraction=.01, max_position_fraction=.5,
                          max_gross_exposure_fraction=.8, max_symbol_exposure_fraction=.5,
                          max_open_positions=5, minimum_cash_buffer_inr=0,
                          minimum_trade_value_inr=1, maximum_trade_value_inr=2000)
    return journal, paper, instrument, quote, DynamicShadowSizer(paper, sizing=policy)


def test_sizing_uses_current_equity_and_integer_constraints(tmp_path):
    journal, paper, instrument, quote, sizer = _sizer(tmp_path)
    journal.put("paper_valuation", {"equity": 10_000})
    first = sizer.size_long(symbol=instrument.symbol, requested_quantity=50, reference_price=100,
                            stop_loss=95, instrument=instrument, quote=quote)
    assert first.risk_budget_rupees == 100
    assert first.final_quantity == 19  # fees must fit within the ₹2,000 cap
    journal.put("paper_valuation", {"equity": 9_000})
    second = sizer.size_long(symbol=instrument.symbol, requested_quantity=50, reference_price=100,
                             stop_loss=95, instrument=instrument, quote=quote)
    assert second.risk_budget_rupees == 90
    assert second.final_quantity == 18


def test_cash_exposure_and_liquidity_limits_bind(tmp_path):
    journal, paper, instrument, quote, sizer = _sizer(tmp_path)
    journal.put("paper_valuation", {"equity": 10_000})
    journal.put("cash", 450)
    journal.put("paper_valuation", {"equity": 450})
    quote.ask_size = 1
    decision = sizer.size_long(symbol=instrument.symbol, requested_quantity=50, reference_price=100,
                               stop_loss=99, instrument=instrument, quote=quote)
    assert decision.final_quantity == 1
    assert decision.quantity_by_cash == 1  # displayed ask size also caps the estimate
    assert decision.quantity_by_liquidity == 1
