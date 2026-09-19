# Broker and data setup guide

Updated: 2026-09-18. Selected broker: **FYERS**. Dhan instructions below are optional legacy reference. This guide does not establish strategy readiness.

## Direct answer

Dhan's trading API is free, but its current public pricing lists real-time and historical Data APIs at **₹499/month**. Confirm the price in your own account before purchasing because broker pricing can change. [Dhan pricing](https://dhanhq.co/trading-apis)

For a no-recurring-data-fee alternative, choose **FYERS**. It states that trading, historical data, quotes, and market data APIs are free for its clients. [FYERS API fees](https://support.fyers.in/portal/en/kb/articles/does-fyers-charge-any-subscription-fees-for-trading-api)

| Need | Recommended source | Cost / caveat |
|---|---|---|
| Long-horizon NSE research | Official NSE bhavcopy, already cached in this repo | Free EOD; not live |
| Dhan execution only | Dhan order/portfolio API | Free; live feed not included |
| Dhan live / historical API data | Dhan Data API | Published as ₹499/month |
| Lowest-cost integrated live system | FYERS API | Broker states market and historical data are free |
| Zerodha alternative | Kite Connect | Personal execution free; real-time + historical data ₹500/month |

Sources: [Dhan authentication](https://dhanhq.co/docs/v2/authentication/), [NSE reports](https://www.nseindia.com/all-reports), and [Zerodha pricing](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis).

Do not use scraped websites, unofficial browser endpoints, or Yahoo Finance as an execution feed. They are fine only for non-live exploration.

## Free research path — start here

1. Keep using this repository's NSE bhavcopy data for EOD research.
2. Run backtests with measured fees and record every trial. Do not change the pass mark in `docs/KNOWLEDGE.md`.
3. When a strategy qualifies, collect a new live/shadow dataset for that exact strategy. EOD data cannot validate intraday execution, spread, or latency.
4. Only then configure a broker feed. Paying for data before an edge exists is an operating expense, not research evidence.

## Dhan setup — optional legacy reference

1. Activate only the equity/derivative segments you actually need.
2. In **Dhan Web → My Profile → Access DhanHQ APIs**, enable TOTP and generate an initial 24-hour access token. Start with the manual token; move to the API-key consent flow only after read-only integration works.
3. Put credentials only in the untracked `.env` file:

   ```dotenv
   DHAN_CLIENT_ID=your_client_id
   DHAN_ACCESS_TOKEN=short_lived_access_token
   ```

4. Call the profile endpoint first to verify token validity and `dataPlan`; download the official instrument master and resolve symbols to Dhan `securityId` values instead of hardcoding IDs.
5. For EOD research, keep the free NSE data. If you need Dhan real-time/historical data, confirm and subscribe to the smallest Data API plan covering the shadow universe.
6. Before any live order permission: get a fixed outbound IP from an ISP or small cloud VM, whitelist it in Dhan, and set up order updates/reconciliation. Dhan says order actions need static-IP allowlisting and its IP cannot be edited for seven days after it is set.
7. Keep `DhanClient(..., allow_order_submission=False)` until the deployment gates in `AUTOMATED_SYSTEM_REFERENCE.md` are met.

## FYERS setup — selected route

1. Open a FYERS account and activate the needed segment.
2. Open the [FYERS API dashboard](https://fyers.in/web/api-dashboard/user-apps); activate the current **Algo trading app**, add the redirect URL, choose minimum permissions, and retain the App ID/secret only in `.env`.
3. Whitelist the server's static outbound IP in the app configuration.
4. Complete the required daily 2FA and obtain a current access token.
5. Run `.venv312/bin/python -m src.execution.fyers_login` from the repository. Open the printed URL and complete FYERS login. The local callback verifies state, exchanges the code, checks your profile and saves the access token in `.env`. It waits up to ten minutes; port 8080 must be available. Repeat login when the token expires.
6. Check data: `.venv312/bin/python -m src.data.fyers_check --symbol NSE:SBIN-EQ`. This fetches one quote snapshot and recent daily candles. Next: instrument resolver/streaming recorder → order updates/reconciliation → qualified strategy shadow execution → risk-gated live deployment.

Copy these fields from `.env.example` into your untracked `.env`:

```dotenv
FYERS_APP_ID=
FYERS_ACCESS_TOKEN=
FYERS_APP_SECRET=
FYERS_REDIRECT_URI=
```

The login helper consumes App ID, secret and redirect URI; the account client
uses App ID and access token. Use `http://127.0.0.1:8080/fyers/callback` as the
exact redirect in both FYERS and `.env`. Endpoint and timeout are in `config/fyers.yaml`.
Read-only usage (do not publish returned account data):

```python
import asyncio
from dotenv import load_dotenv
from src.execution.fyers import FyersClient

async def check_account():
    load_dotenv()
    client = FyersClient.from_env()
    try:
        await client.get_profile()
        print("FYERS profile check succeeded")
    finally:
        await client.close()

asyncio.run(check_account())
```

The REST client also supports `main.py fyers`: streaming recording, status, cost
estimates, halt and qualified paper-intent commands. See [FYERS operations](FYERS_OPERATIONS.md).
`main.py run` still runs Delta and now defaults to paper. FYERS live deployment
is disabled; the transport component alone is not production-ready.

FYERS says retail-algo orders must come from a registered app and whitelisted static IP, require daily 2FA, are limited to 10 orders/second, and convert market orders to Market Price Protection orders. The risk layer must therefore have an explicit execution-price tolerance. [FYERS retail-algo rules](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026)

## Decision

- **FYERS is selected**: login, REST data and streaming recording have been verified. Collect market-hours data and qualify a strategy before paper deployment. See the operating guide for exact remaining work.
- **Dhan is legacy/optional**, not the active implementation plan.
- Neither platform solves the lack of a validated ₹10k strategy or statutory trading costs. Both remain retail systems, not HFT infrastructure.
