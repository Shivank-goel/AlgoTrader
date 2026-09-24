"""FYERS observation service with durable ticks, session checks and paper handoff."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import ValidationError

from src.execution.fyers import FyersAuthenticationError, FyersClient
from src.fyers.costs import FyersCosts
from src.fyers.instruments import resolve_instruments
from src.fyers.intraday import IntradayBarAggregator, IntradayFeatureEngine
from src.fyers.journal import Journal
from src.fyers.models import ROOT, Intent, Quote, RuntimeConfig, Side, environment_path
from src.fyers.paper import PaperBroker
from src.fyers.protection_monitor import ShadowProtectionMonitor
from src.fyers.qualification import qualification


def parse_quote(data: dict, received: float) -> Quote:
    return Quote(symbol=data["symbol"], bid=data["bid_price"], ask=data["ask_price"],
                 bid_size=data["bid_size"], ask_size=data["ask_size"],
                 exchange_time=data["exch_feed_time"], received_time=received)


def nse_open(body: dict) -> bool:
    rows = body.get("marketStatus", [])
    return any(str(row.get("exchange")) in {"NSE", "10"}
               and str(row.get("segment")) in {"CM", "10"}
               and row.get("market_type") == "NORMAL" and row.get("status") == "OPEN"
               for row in rows if isinstance(row, dict))


async def observe(duration: float = 0, intents_path: Path | None = None) -> dict:
    config = RuntimeConfig.load()
    load_dotenv(environment_path(), override=False)
    client = FyersClient.from_env()
    journal = Journal(ROOT / config.database)
    lock = (ROOT / config.database).with_suffix(".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        journal.close()
        raise ValueError("A FYERS recorder already owns this database") from None
    process = None
    tasks = []
    quotes: dict[str, Quote] = {}
    benchmark_quotes: dict[str, Quote] = {}
    queue: asyncio.Queue = asyncio.Queue(maxsize=config.queue_size)
    summary = {"ticks": 0, "valid_quotes": 0, "connections": 0, "errors": 0}
    bar_aggregator = IntradayBarAggregator()
    feature_engine = IntradayFeatureEngine()
    # ``open=False`` remains the fail-closed execution gate, while the explicit
    # state distinguishes a confirmed close from an unavailable API check.
    market = {"open": False, "state": "UNKNOWN", "reason": "not_checked", "checked": 0.0}
    account_refresh = asyncio.Event()
    try:
        paper = PaperBroker(journal, config, FyersCosts.load())
        intents = []
        scheduled_ids: set[str] = set()
        if intents_path:
            intents = [Intent.model_validate(row) for row in json.loads(intents_path.read_text())]
            if any(intent.side == Side.BUY for intent in intents):
                ok, reason = qualification(ROOT / config.qualification_file, ROOT / config.trials_file)
                permitted = []
                for intent in intents:
                    if intent.side == Side.SELL or (ok and intent.strategy == reason):
                        permitted.append(intent)
                    else:
                        journal.event("paper_rejected", {
                            "intent_id": intent.intent_id,
                            "reason": "Entry strategy is not qualified",
                        })
                intents = permitted
                if not intents:
                    raise ValueError("Paper deployment blocked: no qualified entries or risk-reducing exits")
        elif journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_intents'"
        ).fetchone():
            rows = journal.db.execute(
                "SELECT payload FROM scheduled_intents s WHERE NOT EXISTS "
                "(SELECT 1 FROM paper_intent_dispatch d WHERE d.intent_id=s.intent_id) "
                "ORDER BY created"
            ).fetchall()
            intents = [Intent.model_validate_json(row[0]) for row in rows]
            scheduled_ids = {intent.intent_id for intent in intents}
        held_symbols = [row[0] for row in journal.db.execute("SELECT symbol FROM positions WHERE quantity>0")]
        symbols = sorted(set(config.symbols) | set(held_symbols))
        instruments = await resolve_instruments(config.master_url, symbols)
        exit_monitor = ShadowProtectionMonitor(paper, instruments)
        try:
            await client.get_profile()
        except FyersAuthenticationError as exc:
            with journal.db:
                previous_auth = journal.get("authentication", {})
                journal.put("authentication", {"state": "AUTH_REQUIRED", "at": time.time(),
                                                "error_type": type(exc).__name__})
                if (not isinstance(previous_auth, dict)
                        or previous_auth.get("state") != "AUTH_REQUIRED"):
                    journal.db.execute(
                        "INSERT INTO events(received,kind,payload) VALUES(?,?,?)",
                        (time.time(), "authentication_required", json.dumps({
                            "reason": "daily FYERS access token rejected",
                        })),
                    )
            raise
        except Exception as exc:
            with journal.db:
                journal.put("authentication", {"state": "UNAVAILABLE", "at": time.time(),
                                                "error_type": type(exc).__name__})
            raise
        with journal.db:
            journal.put("authentication", {"state": "READY", "at": time.time()})
        journal.event("instruments", {s: i.model_dump(mode="json") for s, i in instruments.items()})
        # Prevent inherited credentials from appearing in process arguments.
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.fyers.stream_worker", cwd=ROOT,
                 env={**os.environ, "FYERS_RECORDER_PID": str(os.getpid()),
                 "FYERS_RECORD_SYMBOLS": json.dumps(symbols + [config.regime_symbol])},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

        async def read_stream() -> None:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                    if event.get("kind") not in {"tick", "connected", "disconnected", "error"}:
                        continue
                    queue.put_nowait(event)
                except json.JSONDecodeError:
                    continue  # SDK's reconnect notices contain no market event.
                except asyncio.QueueFull:
                    journal.halt("market queue overflow; data gap")
                    raise RuntimeError("Recorder cannot keep up with stream") from None
            raise RuntimeError("Streaming worker stopped")

        async def account_checks() -> None:
            while True:
                market.update(open=False, state="UNKNOWN", reason="verification_pending")
                try:
                    # No positions are adopted into paper accounting. Broker and
                    # simulator are deliberately separate ledgers.
                    positions, orders, trades, status = await asyncio.gather(
                        client.get_positions(), client.get_orders(), client.get_trades(),
                        client.get_market_status(),
                    )
                    for body, key in [(positions, "netPositions"), (orders, "orderBook"), (trades, "tradeBook")]:
                        if not isinstance(body.get(key), list):
                            raise ValueError("Invalid account snapshot")
                    opened = nse_open(status)
                    market.update(open=opened, state="OPEN" if opened else "CLOSED",
                                  reason="fyers_market_status", checked=time.time())
                    counts = {"positions": len(positions["netPositions"]),
                              "orders": len(orders["orderBook"]), "trades": len(trades["tradeBook"]),
                              "market_open": market["open"], "market_state": market["state"]}
                    journal.event("account_check", counts)
                    with journal.db:
                        journal.put("account_check", {**counts, "at": market["checked"]})
                except Exception as exc:
                    market.update(open=False, state="UNKNOWN", reason="market_status_unavailable")
                    journal.event("account_error", {"reason": "account/session check failed",
                                                     "operation": "account_and_market_status",
                                                     "error_type": type(exc).__name__,
                                                     "market_state": market["state"]})
                    summary["errors"] += 1
                try:
                    await asyncio.wait_for(account_refresh.wait(), config.reconcile_seconds)
                except TimeoutError:
                    pass
                account_refresh.clear()

        tasks = [asyncio.create_task(read_stream()), asyncio.create_task(account_checks())]
        started = time.monotonic()
        last_health = 0.0
        while not duration or time.monotonic() - started < duration:
            if journal.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_intents'"
            ).fetchone():
                for row in journal.db.execute(
                    "SELECT payload FROM scheduled_intents s WHERE NOT EXISTS "
                    "(SELECT 1 FROM paper_intent_dispatch d WHERE d.intent_id=s.intent_id)"
                    " ORDER BY created"
                ):
                    intent = Intent.model_validate_json(row[0])
                    if intent.intent_id not in scheduled_ids and all(
                        old.intent_id != intent.intent_id for old in intents
                    ):
                        intents.append(intent)
                        scheduled_ids.add(intent.intent_id)
            for task in tasks:
                if task.done():
                    task.result()
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1)
                kind, received, data = event["kind"], event["received"], event["data"]
                journal.event(kind, data, received)
                if kind == "connected":
                    quotes.clear()
                    summary["connections"] += 1
                    market["open"] = False  # require a new account/session check
                    account_refresh.set()
                elif kind in {"error", "disconnected"}:
                    quotes.clear()
                    benchmark_quotes.clear()
                    summary["errors"] += 1
                elif kind == "tick":
                    summary["ticks"] += 1
                    for bar in bar_aggregator.update(data, received_time=received):
                        journal.event(f"bar_{bar.interval_seconds}s", asdict(bar), bar.end_time)
                        journal.event(f"features_{bar.interval_seconds}s",
                                      asdict(feature_engine.update(bar)), bar.end_time)
                    try:
                        quote = parse_quote(data, received)
                        if quote.symbol == config.regime_symbol:
                            old = benchmark_quotes.get(quote.symbol)
                            if quote.usable(received, config.stale_seconds) and (old is None or quote.exchange_time >= old.exchange_time):
                                benchmark_quotes[quote.symbol] = quote
                        elif quote.symbol in instruments:
                            old = quotes.get(quote.symbol)
                            if not quote.usable(received, config.stale_seconds):
                                quotes.pop(quote.symbol, None)
                            elif old is None or quote.exchange_time >= old.exchange_time:
                                quotes[quote.symbol] = quote
                                summary["valid_quotes"] += 1
                                exit_monitor.evaluate(quote, now=received, market_open=market["open"])
                    except (KeyError, ValueError, ValidationError):
                        quotes.pop(data.get("symbol"), None)
                now = time.time()
                for intent in intents[:]:
                    if intent.symbol in quotes:
                        try:
                            paper.fill(intent, instruments[intent.symbol], quotes, now=now,
                                       market_open=market["open"] and now - market["checked"] < config.reconcile_seconds * 2)
                            if intent.intent_id in scheduled_ids:
                                with journal.db:
                                    journal.db.execute(
                                        "INSERT INTO paper_intent_dispatch VALUES(?,?,?,?)",
                                        (intent.intent_id, "FILLED", None, now),
                                    )
                        except ValueError as exc:
                            journal.event("paper_rejected", {"intent_id": intent.intent_id, "reason": str(exc)})
                            if intent.intent_id in scheduled_ids:
                                with journal.db:
                                    journal.db.execute(
                                        "INSERT INTO paper_intent_dispatch VALUES(?,?,?,?)",
                                        (intent.intent_id, "REJECTED", str(exc)[:200], now),
                                    )
                        intents.remove(intent)
            except TimeoutError:
                pass
            paper.mark_to_market({**quotes, **benchmark_quotes}, now=time.time())
            local_now = datetime.now(ZoneInfo("Asia/Kolkata"))
            forced_hour, forced_minute = map(int, config.forced_short_exit_time.split(":"))
            if market["open"] and (local_now.hour, local_now.minute) >= (forced_hour, forced_minute):
                for short_symbol in [row[0] for row in journal.db.execute(
                        "SELECT symbol FROM short_positions WHERE quantity>0")]:
                    if short_symbol in quotes:
                        exit_monitor.evaluate(quotes[short_symbol], now=time.time(),
                                              market_open=True, force_short_close=True)
            if time.monotonic() - last_health >= 5:
                now = time.time()
                fresh = [s for s, q in quotes.items() if q.usable(now, config.stale_seconds)]
                journal.event("feed_health", {"fresh_symbols": fresh, "market_open": market["open"],
                                               "market_state": market["state"],
                                               "market_state_reason": market["reason"]})
                last_health = time.monotonic()
        journal.event("recorder_stopped", summary)
        if not summary["connections"]:
            raise RuntimeError("No authenticated stream connection was established")
        return summary
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        await client.close()
        journal.close()
        lock.close()
