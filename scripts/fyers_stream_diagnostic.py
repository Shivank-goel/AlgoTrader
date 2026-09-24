"""Safe FYERS stream timing probe; never performs account or order operations."""
from __future__ import annotations

import argparse
import json
import os
import time

from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--symbols", nargs="*", default=["NSE:RELIANCE-EQ", "NSE:NIFTY50-INDEX"])
    args = parser.parse_args()
    load_dotenv(os.getenv("FYERS_ENV_FILE", ".env"), override=False)
    from fyers_apiv3.FyersWebsocket import data_ws

    started = time.time()
    def emit(kind: str, payload: dict) -> None:
        print(json.dumps({"kind": kind, "local_time": time.time(), **payload}, sort_keys=True), flush=True)
    def on_message(data: dict) -> None:
        now = time.time()
        if not isinstance(data, dict) or data.get("symbol") not in args.symbols:
            return
        exchange = data.get("exch_feed_time")
        emit("tick", {"symbol": data.get("symbol"), "exchange_time": exchange,
                       "exchange_latency_seconds": now - exchange if isinstance(exchange, int | float) else None,
                       "safe_fields": sorted(k for k in data if k in {"ltp", "bid_price", "ask_price", "exch_feed_time", "last_traded_time"})})
    def on_connect() -> None:
        emit("connected", {"symbols": args.symbols, "data_type": "SymbolUpdate"})
        socket.subscribe(symbols=args.symbols, data_type="SymbolUpdate")
    def on_error(error: object) -> None:
        emit("error", {"error_type": type(error).__name__})
    def on_close(*_: object) -> None:
        emit("closed", {})
    socket = data_ws.FyersDataSocket(
        access_token=f"{os.environ['FYERS_APP_ID']}:{os.environ['FYERS_ACCESS_TOKEN']}",
        litemode=False, reconnect=True, reconnect_retry=3,
        on_message=on_message, on_connect=on_connect, on_error=on_error, on_close=on_close,
    )
    socket.connect()
    try:
        while time.time() - started < args.seconds:
            time.sleep(1)
    finally:
        socket.close_connection()


if __name__ == "__main__":
    main()
