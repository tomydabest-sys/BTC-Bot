"""Discover candidate weather-trading wallets from live Polymarket data.

The original weather-wallet analysis ("the profitable four") was never
committed to this repo, and the addresses in wallet_profiler.py turned out to
be BTC/ETH up-or-down scalpers, not weather traders. This script regenerates
candidates from live data so the operator can populate
config/polyweather/wallets.yaml with confidence:

  1. Pull active weather events from Gamma (the strict temperature filter).
  2. Collect the wallets trading those markets (data-api /trades?market=).
  3. For the most-active weather traders, sum their weather-position PnL
     (realizedPnl on closed portions + cashPnl on open) as a ranking PROXY.
  4. Print a ranked table; optionally write JSON.

Profitability here is a SNAPSHOT proxy, not closed round-trip P&L — a wallet
with a large favorable open position will rank high. Confirm a candidate over
time before trusting it as a confirmation signal.

Usage:
  python scripts/polyweather/discover_weather_wallets.py
  python scripts/polyweather/discover_weather_wallets.py --markets 30 --top 20
  python scripts/polyweather/discover_weather_wallets.py --out data/wx_wallets.json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from pathlib import Path

import httpx

from polybot.polyweather.exchanges.data_api_client import is_weather_market

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "polyweather-discover/1.0"}


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> object:
    try:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def discover(markets_n: int, top_n: int, trades_per_market: int) -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as c:
        events = await _get(
            c, f"{GAMMA}/events",
            {"active": "true", "closed": "false", "tag_slug": "weather",
             "order": "volume24hr", "ascending": "false", "limit": markets_n},
        )
        events = events if isinstance(events, list) else []
        condition_ids: list[str] = []
        for e in events:
            if not is_weather_market(e.get("title"), e.get("slug"), e.get("slug")):
                continue
            for m in (e.get("markets") or [])[:4]:
                cid = m.get("conditionId")
                if cid:
                    condition_ids.append(cid)

        print(f"[discover] {len(condition_ids)} weather markets from {len(events)} events")

        trader_trades: collections.Counter = collections.Counter()
        for cid in condition_ids:
            trades = await _get(c, f"{DATA_API}/trades", {"market": cid, "limit": trades_per_market})
            for t in (trades or []):
                w = (t.get("proxyWallet") or "").lower()
                if w:
                    trader_trades[w] += 1

        print(f"[discover] {len(trader_trades)} unique weather traders")
        top = [w for w, _ in trader_trades.most_common(top_n)]

        rows: list[dict] = []
        for w in top:
            pos = await _get(c, f"{DATA_API}/positions", {"user": w, "limit": 500})
            wx = [
                p for p in (pos or [])
                if is_weather_market(p.get("title"), p.get("slug"), p.get("eventSlug"))
            ]
            realized = sum(float(p.get("realizedPnl") or 0) for p in wx)
            cash = sum(float(p.get("cashPnl") or 0) for p in wx)
            cur_val = sum(float(p.get("currentValue") or 0) for p in wx)
            rows.append({
                "address": w,
                "weather_trades": trader_trades[w],
                "weather_positions": len(wx),
                "realized_pnl": round(realized, 2),
                "cash_pnl": round(cash, 2),
                "open_value": round(cur_val, 2),
                "pnl_proxy": round(realized + cash, 2),
            })

    rows.sort(key=lambda r: r["pnl_proxy"], reverse=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--markets", type=int, default=40, help="weather events to sample")
    ap.add_argument("--top", type=int, default=20, help="top traders to profile")
    ap.add_argument("--trades-per-market", type=int, default=100)
    ap.add_argument("--out", type=Path, default=None, help="write ranked JSON here")
    args = ap.parse_args()

    rows = asyncio.run(discover(args.markets, args.top, args.trades_per_market))

    print()
    print(f"{'wallet':<44}{'wTrades':>8}{'wxPos':>6}{'realPnL':>11}{'cashPnL':>11}{'openVal':>10}{'proxy':>11}")
    print("-" * 101)
    for r in rows:
        print(
            f"{r['address']:<44}{r['weather_trades']:>8}{r['weather_positions']:>6}"
            f"{r['realized_pnl']:>11.0f}{r['cash_pnl']:>11.0f}{r['open_value']:>10.0f}{r['pnl_proxy']:>11.0f}"
        )
    print()
    print("NOTE: pnl_proxy = realized + cash (snapshot), NOT closed round-trip P&L.")
    print("Confirm a candidate over time before adding it to config/polyweather/wallets.yaml.")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
