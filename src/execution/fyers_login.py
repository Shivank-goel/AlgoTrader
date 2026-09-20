"""Local FYERS login: python -m src.execution.fyers_login."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp
import yaml
from aiohttp import web
from dotenv import dotenv_values, set_key

from src.execution.fyers import FyersClient, FyersGatewayError
from src.fyers.models import environment_path

ROOT = Path(__file__).resolve().parents[2]
CALLBACK_HANDLER = web.AppKey("callback_handler", object)


def callback_address(uri: str) -> tuple[str, int, str]:
    """Only expose the callback on this computer's loopback interface."""
    parsed = urlsplit(uri)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or not parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("Redirect must be http://127.0.0.1:PORT/PATH")
    return parsed.hostname, parsed.port, parsed.path


async def exchange_code(base_url: str, app_id: str, secret: str, code: str) -> str:
    payload = {
        "grant_type": "authorization_code",
        "appIdHash": hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest(),
        "code": code,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(f"{base_url}/validate-authcode", json=payload,
                                    allow_redirects=False) as response:
                if response.status != 200:
                    raise FyersGatewayError(f"Token exchange failed: HTTP {response.status}")
                body = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError):
        raise FyersGatewayError("Token exchange failed; restart login") from None
    if (not isinstance(body, dict) or body.get("s") != "ok"
            or not isinstance(body.get("access_token"), str) or not body["access_token"].strip()):
        raise FyersGatewayError("FYERS did not return a valid access token; restart login")
    return body["access_token"]


def make_app(state: str, completed: asyncio.Future[str]) -> web.Application:
    async def callback(request: web.Request) -> web.Response:
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if not secrets.compare_digest(request.query.get("state", ""), state):
            return web.Response(status=400, text="Invalid login state. Restart login.", headers=headers)
        code = request.query.get("auth_code", "")
        if not code or request.query.get("s") == "error":
            return web.Response(status=400, text="Login was not completed. Try again.", headers=headers)
        if completed.done():
            return web.Response(status=409, text="Login callback already received.", headers=headers)
        completed.set_result(code)
        return web.Response(text="Login received. Check your terminal for verification.", headers=headers)

    app = web.Application()
    app[CALLBACK_HANDLER] = callback
    return app


async def login() -> None:
    env_path = environment_path()
    values = dotenv_values(env_path)
    required = ("FYERS_APP_ID", "FYERS_APP_SECRET", "FYERS_REDIRECT_URI")
    if any(not (values.get(key) or "").strip() for key in required):
        raise ValueError("Set FYERS_APP_ID, FYERS_APP_SECRET and FYERS_REDIRECT_URI in .env")
    app_id, secret, redirect = (values[key].strip() for key in required)
    host, port, path = callback_address(redirect)
    with (ROOT / "config/fyers.yaml").open() as stream:
        config = yaml.safe_load(stream)
    # Reuse the gateway's official-host validation before sending credentials.
    client = FyersClient(app_id, "pending", **config)
    state = secrets.token_urlsafe(32)
    completed = asyncio.get_running_loop().create_future()
    app = make_app(state, completed)
    app.router.add_get(path, app[CALLBACK_HANDLER])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        query = urlencode({"client_id": app_id, "redirect_uri": redirect,
                           "response_type": "code", "state": state})
        print("Open this URL in your browser and complete FYERS login:", flush=True)
        print(f"{client.base_url}/generate-authcode?{query}", flush=True)
        code = await asyncio.wait_for(completed, timeout=600)
        token = await exchange_code(client.base_url, app_id, secret, code)
        client = FyersClient(app_id, token, **config)
        try:
            await client.get_profile()
        finally:
            await client.close()
        # Preserve all other dotenv entries; dotenv replaces the file atomically.
        env_path.chmod(0o600)
        set_key(str(env_path), "FYERS_ACCESS_TOKEN", token)
        env_path.chmod(0o600)
        print("FYERS profile verified. Access token saved to .env.", flush=True)
    finally:
        await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(login())
    except KeyboardInterrupt:
        print("Login cancelled.")
    except (TimeoutError, ValueError, OSError, FyersGatewayError):
        # Exceptions may contain credentials or callback query parameters.
        print("Login failed. Check .env, port availability and network, then retry.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
