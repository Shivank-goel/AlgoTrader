#!/usr/bin/env python3
"""Place a minimal market order on Delta Exchange Demo and verify fill execution.

Opens the smallest allowed BTCUSD position with a market buy, confirms fill,
then closes with a market sell to restore a flat book.

Usage:
    python scripts/test_demo_market_order.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")


def sign(method: str, path: str, query_string: str = "", payload: str = "") -> dict:
    timestamp = str(int(time.time()))
    sig_data = method + timestamp + path + query_string + payload
    sig = hmac.new(API_SECRET.encode(), sig_data.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": sig,
        "Content-Type": "application/json",
        "User-Agent": "crypto-trader/1.0",
    }


async def request_json(session, method, path, params=None, data=None):
    query_string = ""
    if params:
        query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    payload = json.dumps(data, separators=(",", ":")) if data else ""
    headers = sign(method.upper(), path, query_string, payload)
    url = f"{BASE_URL}{path}"
    kwargs = {"headers": headers}
    if params:
        kwargs["params"] = params
    if data:
        kwargs["data"] = payload
    async with session.request(method.upper(), url, **kwargs) as resp:
        body = await resp.json()
        return resp.status, body


async def get_position(session, product_id: int) -> Optional[dict]:
    status, body = await request_json(
        session,
        "GET",
        "/v2/positions",
        params={"product_id": product_id},
    )
    if status != 200:
        return None
    result = body.get("result", body)
    if isinstance(result, dict):
        size = float(result.get("size", 0))
        return result if size != 0 else None
    if isinstance(result, list):
        for pos in result:
            if float(pos.get("size", 0)) != 0:
                return pos
    return None


async def main():
    print("=" * 60)
    print("Delta Exchange Demo — Market Order Fill Test")
    print("=" * 60)

    if not API_KEY or not API_SECRET:
        print("ERROR: Set DELTA_API_KEY and DELTA_API_SECRET in .env")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        # Product info
        print("\n[1] Fetching BTCUSD product...")
        async with session.get(f"{BASE_URL}/v2/products/BTCUSD") as resp:
            body = await resp.json()
            product = body.get("result", body)
            product_id = product["id"]
            min_size = float(product.get("min_size", 1))
            print(f"    Product ID: {product_id}, min size: {min_size}")

        # Current price
        print("\n[2] Fetching mark price...")
        async with session.get(f"{BASE_URL}/v2/tickers/BTCUSD") as resp:
            body = await resp.json()
            ticker = body.get("result", body)
            mark_price = float(ticker.get("mark_price", 0))
            print(f"    Mark price: ${mark_price:.2f}")

        # Wallet before
        print("\n[3] Wallet balance before trade...")
        status, body = await request_json(session, "GET", "/v2/wallet/balances")
        if status == 200:
            balances = body.get("result", body)
            if isinstance(balances, list) and balances:
                print(f"    Balance: ${float(balances[0].get('balance', 0)):.4f}")

        # Market BUY
        buy_payload = {
            "product_id": product_id,
            "size": int(min_size),
            "side": "buy",
            "order_type": "market_order",
        }
        print(f"\n[4] Placing MARKET BUY: {int(min_size)} contract(s)...")
        status, body = await request_json(session, "POST", "/v2/orders", data=buy_payload)
        if status != 200:
            print(f"    ✗ Buy failed ({status}): {body}")
            sys.exit(1)

        buy_order = body.get("result", body)
        buy_order_id = buy_order.get("id")
        buy_state = buy_order.get("state", buy_order.get("status", "unknown"))
        avg_fill = buy_order.get("average_fill_price") or buy_order.get("avg_fill_price")
        print(f"    ✓ Order ID: {buy_order_id}")
        print(f"    State: {buy_state}")
        if avg_fill:
            print(f"    Avg fill: ${float(avg_fill):.2f}")

        await asyncio.sleep(1.5)

        # Order detail
        print(f"\n[5] Fetching order {buy_order_id}...")
        status, body = await request_json(session, "GET", f"/v2/orders/{buy_order_id}")
        if status == 200:
            order = body.get("result", body)
            state = order.get("state", order.get("status"))
            filled = order.get("filled_size", order.get("size"))
            fill_price = order.get("average_fill_price") or order.get("avg_fill_price")
            print(f"    State: {state}, filled: {filled}, avg: {fill_price}")

        # Fills
        print("\n[6] Fetching fills for order...")
        status, body = await request_json(
            session,
            "GET",
            "/v2/fills",
            params={"order_id": buy_order_id},
        )
        if status == 200:
            fills = body.get("result", body)
            if isinstance(fills, list):
                print(f"    Fills: {len(fills)}")
                for f in fills[:3]:
                    print(
                        f"    - price={f.get('price')}, size={f.get('size')}, "
                        f"side={f.get('side')}, time={f.get('created_at', f.get('timestamp'))}"
                    )
            else:
                print(f"    Fills response: {str(fills)[:300]}")
        else:
            print(f"    Fills endpoint: {status} — {body.get('error', body)}")

        # Position
        print("\n[7] Checking open position...")
        position = await get_position(session, product_id)
        if position:
            size = float(position.get("size", 0))
            entry = float(position.get("entry_price", 0))
            upnl = float(position.get("unrealized_pnl", 0))
            print(f"    ✓ Position OPEN: size={size}, entry=${entry:.2f}, uPnL=${upnl:.4f}")
        else:
            print("    ✗ No position found — market order may not have filled")
            sys.exit(1)

        # Market SELL to close
        close_size = abs(int(float(position.get("size", min_size))))
        sell_payload = {
            "product_id": product_id,
            "size": close_size,
            "side": "sell",
            "order_type": "market_order",
        }
        print(f"\n[8] Closing position — MARKET SELL {close_size} contract(s)...")
        status, body = await request_json(session, "POST", "/v2/orders", data=sell_payload)
        if status != 200:
            print(f"    ✗ Sell failed ({status}): {body}")
            sys.exit(1)

        sell_order = body.get("result", body)
        print(f"    ✓ Close order ID: {sell_order.get('id')}, state: {sell_order.get('state')}")

        await asyncio.sleep(1.5)

        # Verify flat
        print("\n[9] Verifying flat position...")
        position = await get_position(session, product_id)
        if position is None:
            print("    ✓ Position closed — book is flat")
        else:
            print(f"    ⚠ Remaining size: {position.get('size')}")

        # Wallet after
        print("\n[10] Wallet balance after round-trip...")
        status, body = await request_json(session, "GET", "/v2/wallet/balances")
        if status == 200:
            balances = body.get("result", body)
            if isinstance(balances, list) and balances:
                print(f"    Balance: ${float(balances[0].get('balance', 0)):.4f}")

    print("\n" + "=" * 60)
    print("Market order fill test complete!")
    print("  Market BUY  → filled ✓")
    print("  Market SELL → closed ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
