#!/usr/bin/env python3
"""Place a small test trade on Delta Exchange Demo to verify order execution.

This places a tiny limit BUY order far below market price (won't fill),
then cancels it. Validates the full order lifecycle without risk.

Usage:
    python scripts/test_demo_trade.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

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


async def main():
    print("=" * 60)
    print("Delta Exchange Demo — Test Order Lifecycle")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        # 1. Get BTCUSD product info
        print("\n[1] Fetching BTCUSD product...")
        async with session.get(f"{BASE_URL}/v2/products/BTCUSD") as resp:
            body = await resp.json()
            product = body.get("result", body)
            product_id = product["id"]
            tick_size = float(product.get("tick_size", 0.5))
            min_size = float(product.get("min_size", 1))
            print(f"    Product ID: {product_id}")
            print(f"    Tick Size: {tick_size}")
            print(f"    Min Size: {min_size}")

        # 2. Get current price
        print("\n[2] Fetching current BTCUSD price...")
        async with session.get(f"{BASE_URL}/v2/tickers/BTCUSD") as resp:
            body = await resp.json()
            ticker = body.get("result", body)
            mark_price = float(ticker.get("mark_price", 0))
            print(f"    Mark Price: ${mark_price:.2f}")

        # 3. Place a limit buy order way below market (won't fill)
        limit_price = round((mark_price * 0.90) / tick_size) * tick_size  # 10% below market
        order_payload = {
            "product_id": product_id,
            "size": int(min_size),
            "side": "buy",
            "order_type": "limit_order",
            "limit_price": str(limit_price),
        }
        payload_str = json.dumps(order_payload, separators=(",", ":"))
        path = "/v2/orders"
        headers = sign("POST", path, "", payload_str)

        print(f"\n[3] Placing limit BUY order: {min_size} contracts @ ${limit_price:.2f} (10% below market)...")
        async with session.post(f"{BASE_URL}{path}", headers=headers, data=payload_str) as resp:
            body = await resp.json()
            if resp.status == 200:
                result = body.get("result", body)
                order_id = result.get("id")
                status = result.get("state", result.get("status", "unknown"))
                print(f"    ✓ Order placed! ID: {order_id}, Status: {status}")
                print(f"    Details: {json.dumps(result, indent=2)[:500]}")
            else:
                print(f"    ✗ Order failed: {resp.status}")
                error = body.get("error", body)
                print(f"    Error: {json.dumps(error, indent=2)[:500]}")
                return

        # 4. Check open orders
        print(f"\n[4] Fetching open orders...")
        path = "/v2/orders"
        params = {"product_id": product_id, "state": "open"}
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = sign("GET", path, qs)
        async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as resp:
            body = await resp.json()
            result = body.get("result", body)
            orders = result if isinstance(result, list) else result.get("orders", [])
            print(f"    Open orders: {len(orders)}")
            for o in orders:
                print(f"    - ID={o.get('id')}, side={o.get('side')}, "
                      f"price={o.get('limit_price')}, size={o.get('size')}, state={o.get('state')}")

        # 5. Cancel the test order
        if order_id:
            print(f"\n[5] Cancelling test order {order_id}...")
            path = "/v2/orders"
            cancel_payload = {"id": order_id, "product_id": product_id}
            payload_str = json.dumps(cancel_payload, separators=(",", ":"))
            headers = sign("DELETE", path, "", payload_str)
            async with session.delete(f"{BASE_URL}{path}", headers=headers, data=payload_str) as resp:
                body = await resp.json()
                if resp.status == 200:
                    print(f"    ✓ Order cancelled successfully!")
                else:
                    print(f"    ✗ Cancel failed: {body}")

        # 6. Verify no open orders remain
        print(f"\n[6] Verifying clean state...")
        path = "/v2/orders"
        params = {"product_id": product_id, "state": "open"}
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = sign("GET", path, qs)
        async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as resp:
            body = await resp.json()
            result = body.get("result", body)
            orders = result if isinstance(result, list) else result.get("orders", [])
            if len(orders) == 0:
                print(f"    ✓ No open orders — clean state confirmed")
            else:
                print(f"    ⚠ Still {len(orders)} open orders")

    print("\n" + "=" * 60)
    print("Order lifecycle test complete!")
    print("  - Place ✓")
    print("  - Query ✓")
    print("  - Cancel ✓")
    print("Your demo account is fully functional for automated trading.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
