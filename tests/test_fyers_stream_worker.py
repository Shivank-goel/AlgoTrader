from src.fyers.stream_worker import market_tick


def test_market_tick_projects_only_configured_safe_fields():
    result = market_tick({
        "symbol": "NSE:SBIN-EQ", "bid_price": 99.0, "ask_price": 100.0,
        "access_token": "must-not-appear", "nested": {"secret": "must-not-appear"},
    }, ["NSE:SBIN-EQ"])

    assert result == {
        "symbol": "NSE:SBIN-EQ", "bid_price": 99.0, "ask_price": 100.0,
    }
    assert "must-not-appear" not in str(result)


def test_market_tick_rejects_non_mapping_without_payload():
    assert market_tick(["untrusted", "payload"], ["NSE:SBIN-EQ"]) is None


def test_market_tick_rejects_unknown_symbol_without_rewriting_it():
    result = market_tick({"symbol": "NSE:OTHER-EQ", "ltp": 42}, ["NSE:SBIN-EQ"])

    assert result is None
