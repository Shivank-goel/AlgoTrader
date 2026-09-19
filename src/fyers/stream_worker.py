"""Isolated SDK worker: credentials stay in .env, output contains market fields only."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

from dotenv import load_dotenv

from src.fyers.models import ROOT, RuntimeConfig

FIELDS = {"symbol", "type", "ltp", "bid_price", "ask_price", "bid_size", "ask_size",
          "exch_feed_time", "last_traded_time", "vol_traded_today"}


def main() -> None:
    config = RuntimeConfig.load()
    if "FYERS_RECORD_SYMBOLS" in os.environ:
        config = RuntimeConfig.model_validate({
            **config.model_dump(), "symbols": json.loads(os.environ["FYERS_RECORD_SYMBOLS"]),
        })
    load_dotenv(ROOT / ".env", override=True)
    from fyers_apiv3.FyersWebsocket import data_ws

    # SDK exceptions can include authentication data. Only our bounded status codes
    # are emitted; never forward raw SDK errors or responses to logs.
    logger = logging.getLogger("fyers_sdk_suppressed")
    logger.disabled = True
    data_ws.FyersLogger = lambda *args, **kwargs: logger
    log_path = ROOT / config.sdk_logs
    log_path.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def emit(kind: str, data: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps({"kind": kind, "received": time.time(), "data": data}, allow_nan=False) + "\n")
            sys.stdout.flush()

    def message(data) -> None:
        if isinstance(data, dict) and data.get("symbol") in config.symbols:
            try:
                emit("tick", {k: v for k, v in data.items() if k in FIELDS})
            except (ValueError, TypeError):
                emit("error", {"reason": "invalid market message"})
        elif isinstance(data, dict) and data.get("s") == "error":
            emit("error", {"reason": "subscription rejected"})

    def connected() -> None:
        emit("connected", {})
        socket.subscribe(symbols=config.symbols, data_type="SymbolUpdate")

    socket = data_ws.FyersDataSocket(
        access_token=f"{os.environ['FYERS_APP_ID']}:{os.environ['FYERS_ACCESS_TOKEN']}",
        log_path=str(log_path), write_to_file=False, litemode=False,
        reconnect=True, reconnect_retry=config.reconnect_attempts,
        on_message=message, on_connect=connected,
        on_error=lambda *_: emit("error", {"reason": "stream error"}),
        on_close=lambda *_: emit("disconnected", {}),
    )
    socket.connect()
    # SDK owns non-daemon threads. Stop even if the recorder crashes before it
    # can terminate us; orphaned streams must not outlive their data journal.
    parent_pid = int(os.environ.get("FYERS_RECORDER_PID", os.getppid()))
    while os.getppid() == parent_pid:
        time.sleep(1)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"kind":"error","received":0,"data":{"reason":"SDK startup failed"}}', flush=True)
        raise SystemExit(1) from None
