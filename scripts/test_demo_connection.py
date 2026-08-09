#!/usr/bin/env python3
"""Test connectivity to Delta Exchange Demo (Global Testnet).

This script validates:
1. Public API access (products, tickers, candles)
2. Authenticated API access (wallet balance, positions)
3. Signature generation correctness

Usage:
    python scripts/test_demo_connection.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

BASE_URL = "https://cdn-ind.testnet.deltaex.org"
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")


def generate_signature(method: str, path: str, query_string: str = "", payload: str = "") -> dict[str, str]:
    """Generate auth headers per Delta Exchange API spec."""
    timestamp = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        signature_data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "User-Agent": "crypto-trader/1.0",
    }


async def test_public_products(session: aiohttp.ClientSession) -> dict:
    """Test: GET /v2/products (public, no auth needed)."""
    print("\n[1/5] Testing GET /v2/products (public)...")
    async with session.get(f"{BASE_URL}/v2/products") as resp:
        body = await resp.json()
        if resp.status == 200:
            result = body.get("result", body)
            products = result if isinstance(result, list) else result.get("products", [])
            print(f"  ✓ Success! Found {len(products)} products")
            perpetuals = [p for p in products if p.get("contract_type") == "perpetual_futures"]
            print(f"  ✓ Perpetual futures: {len(perpetuals)}")
            if perpetuals[:5]:
                symbols = [p["symbol"] for p in perpetuals[:5]]
                print(f"  ✓ Sample symbols: {symbols}")
            return {"products": products, "perpetuals": perpetuals}
        else:
            print(f"  ✗ Failed with status {resp.status}: {body}")
            return {}


async def test_public_ticker(session: aiohttp.ClientSession, symbol: str) -> dict:
    """Test: GET /v2/tickers/{symbol} (public)."""
    print(f"\n[2/5] Testing GET /v2/tickers/{symbol} (public)...")
    async with session.get(f"{BASE_URL}/v2/tickers/{symbol}") as resp:
        body = await resp.json()
        if resp.status == 200:
            result = body.get("result", body)
            mark_price = result.get("mark_price", "N/A")
            last_price = result.get("close", result.get("last_price", "N/A"))
            volume = result.get("volume", "N/A")
            print(f"  ✓ {symbol}: last={last_price}, mark={mark_price}, vol24h={volume}")
            return result
        else:
            print(f"  ✗ Failed with status {resp.status}: {body}")
            return {}


async def test_public_candles(session: aiohttp.ClientSession, symbol: str) -> list:
    """Test: GET /v2/history/candles (public)."""
    print(f"\n[3/5] Testing GET /v2/history/candles for {symbol} (public)...")
    now = int(time.time())
    params = {
        "symbol": symbol,
        "resolution": "15m",
        "start": now - 86400,
        "end": now,
    }
    async with session.get(f"{BASE_URL}/v2/history/candles", params=params) as resp:
        body = await resp.json()
        if resp.status == 200:
            result = body.get("result", body)
            candles = result if isinstance(result, list) else result.get("candles", [])
            print(f"  ✓ Got {len(candles)} candles")
            if candles:
                last = candles[-1] if isinstance(candles[-1], dict) else candles[0]
                if isinstance(last, dict):
                    print(f"  ✓ Latest: O={last.get('open')} H={last.get('high')} L={last.get('low')} C={last.get('close')}")
            return candles
        else:
            print(f"  ✗ Failed with status {resp.status}: {body}")
            return []


async def test_auth_wallet(session: aiohttp.ClientSession) -> dict:
    """Test: GET /v2/wallet/balances (authenticated)."""
    print("\n[4/5] Testing GET /v2/wallet/balances (authenticated)...")
    path = "/v2/wallet/balances"
    headers = generate_signature("GET", path)
    async with session.get(f"{BASE_URL}{path}", headers=headers) as resp:
        body = await resp.json()
        if resp.status == 200:
            result = body.get("result", body)
            if isinstance(result, list):
                for bal in result:
                    asset = bal.get("asset_symbol", bal.get("currency", "?"))
                    balance = bal.get("balance", bal.get("available_balance", 0))
                    if float(balance) > 0:
                        print(f"  ✓ {asset}: {balance}")
            elif isinstance(result, dict):
                print(f"  ✓ Wallet response: {json.dumps(result, indent=2)[:500]}")
            return result
        else:
            print(f"  ✗ Auth failed with status {resp.status}")
            error = body.get("error", body.get("message", body))
            print(f"  ✗ Error: {error}")
            print("  → Check that your API key/secret are correct for the DEMO account")
            print("  → Demo keys must be used with testnet-api.delta.exchange")
            return {}


async def test_auth_positions(session: aiohttp.ClientSession, product_id: int = None) -> list:
    """Test: GET /v2/positions (authenticated)."""
    print("\n[5/5] Testing GET /v2/positions (authenticated)...")
    path = "/v2/positions"
    params = {}
    if product_id:
        params["product_id"] = product_id
    else:
        params["underlying_asset_symbol"] = "BTC"
    query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    headers = generate_signature("GET", path, query_string)
    async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as resp:
        body = await resp.json()
        if resp.status == 200:
            result = body.get("result", body)
            positions = result if isinstance(result, list) else result.get("positions", [])
            open_pos = [p for p in positions if float(p.get("size", 0)) != 0]
            print(f"  ✓ Open positions: {len(open_pos)}")
            for p in open_pos:
                print(f"    - {p.get('product_symbol', p.get('symbol'))}: "
                      f"size={p.get('size')}, entry={p.get('entry_price')}, "
                      f"pnl={p.get('unrealized_pnl', 'N/A')}")
            return positions
        else:
            print(f"  ✗ Failed with status {resp.status}: {body.get('error', body)}")
            return []


async def main():
    print("=" * 60)
    print("Delta Exchange Demo API Connectivity Test")
    print("=" * 60)
    print(f"Base URL: {BASE_URL}")
    print(f"API Key:  {'*' * 8}... (set, {len(API_KEY)} chars)" if API_KEY else "API Key: (not set)")
    print(f"Secret:   {'*' * 8}... (set, {len(API_SECRET)} chars)" if API_SECRET else "Secret: (not set)")

    if not API_KEY or not API_SECRET:
        print("\n✗ ERROR: DELTA_API_KEY and DELTA_API_SECRET must be set in .env")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        # Public endpoints
        product_data = await test_public_products(session)

        # Pick a symbol for further testing
        symbol = "BTCUSDT"
        if product_data.get("perpetuals"):
            available_symbols = [p["symbol"] for p in product_data["perpetuals"]]
            if "BTCUSDT" in available_symbols:
                symbol = "BTCUSDT"
            elif "BTCUSD" in available_symbols:
                symbol = "BTCUSD"
            elif available_symbols:
                symbol = available_symbols[0]
            print(f"\n  Using symbol: {symbol}")

        await test_public_ticker(session, symbol)
        await test_public_candles(session, symbol)

        # Authenticated endpoints
        await test_auth_wallet(session)
        await test_auth_positions(session)

    print("\n" + "=" * 60)
    print("Test complete! If all checks passed (✓), your demo")
    print("account is ready for paper trading.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
