#!/usr/bin/env python3
"""Pre-close SMG digest: what to enter today, with share counts.

SMG fills at the closing price if you enter before 4pm ET, so instant alerts
buy you nothing over a single well-timed daily list -- you cannot act on a
10am filing any faster than a 3pm one. This runs shortly before the close and
sends one actionable message.

Position sizing assumes an equal-weight slice of the portfolio, so the output
is "buy N shares of X", not "here is an interesting filing".

    python scripts/smg_digest.py --strategy insider_big --positions 10
    python scripts/smg_digest.py --send          # push to phone/Discord
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import (OUTBOX_PATH, TRADES_PATH, UNIVERSE_PATH,  # noqa: E402
                        load_settings, load_watchlist)
from src.filters import Watchlist  # noqa: E402
from src.models import Trade  # noqa: E402
from src.notify import discord, ntfy, outbox  # noqa: E402
from src.config import PRICES_PATH  # noqa: E402
from src.prices import PriceStore, daily_volatility, fetch_universe  # noqa: E402
from scripts.backtest import (COMMISSION, SMG_MIN_MCAP, SMG_MIN_PRICE,  # noqa: E402
                              eligible, find_clusters)
from scripts.daily_picks import STRATEGIES  # noqa: E402

log = logging.getLogger("smg")
ET = ZoneInfo("America/New_York")
PORTFOLIO = 100_000.0

# Stop and target are set from recent daily volatility, so a quiet stock gets a
# tight stop and a jumpy one gets room. A fixed percentage would stop you out
# of volatile names on noise and leave no protection on calm ones.
STOP_MULT = 2.5          # days of typical movement against you
TARGET_MULT = 5.0        # 2:1 reward-to-risk

# DECA SMG 2026-27: Sept 8 - Dec 4 2026. Ranking is percent return vs
# S&P 500 Growth, and the top 25 per region advance to ICDC.
SESSION_END = date(2026, 12, 4)


def market_status() -> str:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return "market closed (weekend) — this list is for the next session"
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= close:
        return "after 4pm ET — entries now fill at TOMORROW's close"
    mins = int((close - now).total_seconds() // 60)
    return f"{mins // 60}h {mins % 60}m until the 4pm ET close"


def build(strategy: str, positions: int, since_days: int, limit: int):
    rows = store.load(TRADES_PATH)
    universe = fetch_universe(UNIVERSE_PATH)
    px = PriceStore(PRICES_PATH)
    today = date.today()
    wl = Watchlist(load_watchlist())
    clusters = find_clusters(rows)
    fn = STRATEGIES[strategy]

    cutoff = (today - timedelta(days=since_days)).isoformat()
    slice_size = PORTFOLIO / max(positions, 1)

    picks, seen = [], set()
    for r in rows:
        tk = r.get("ticker")
        if not tk or (r.get("filed_date") or "") < cutoff or tk in seen:
            continue
        info = universe.get(tk, {})
        if not eligible(info) or not fn(r, wl, clusters):
            continue
        price = info.get("price")
        if not price:
            continue
        seen.add(tk)
        shares = int((slice_size - COMMISSION) // price)
        if shares < 1:
            continue

        closes = px.history(tk, today - timedelta(days=90))
        vol = daily_volatility(closes, today)
        last = px.close_on_or_before(tk, today, 6) or price
        stop = target = band = None
        if vol:
            stop = round(last * (1 - STOP_MULT * vol), 2)
            target = round(last * (1 + TARGET_MULT * vol), 2)
            band = round(last * vol, 2)

        picks.append({
            "ticker": tk, "price": last, "shares": shares,
            "cost": round(shares * last, 2),
            "company": (info.get("name") or r.get("company", ""))[:44],
            "who": r.get("person", "?"), "role": r.get("role") or r["source"],
            "filed": r.get("filed_date"), "url": r.get("url", ""),
            "value": r.get("value_usd") or r.get("value_range") or "",
            "vol": vol, "band": band, "stop": stop, "target": target,
            "risk": round((last - stop) * shares, 0) if stop else None,
            "reward": round((target - last) * shares, 0) if target else None,
        })

    # Biggest disclosed conviction first, so a short list is the strongest one.
    picks.sort(key=lambda p: -(p["cost"]))
    px.save()
    return picks[:limit]


def render(picks, strategy: str, positions: int) -> str:
    status = market_status()
    days_left = (SESSION_END - date.today()).days
    if not picks:
        return (f"SMG — no qualifying signals today ({strategy}).\n"
                f"{status}. {days_left}d left in the DECA session.\n"
                "A quiet day is normal; forcing a trade is how you lose.")

    out = [f"SMG PICKS — {strategy}",
           f"{status} · {days_left}d left in session",
           f"Equal weight, {positions} pos (${PORTFOLIO/positions:,.0f} each)",
           ""]
    for p in picks:
        out.append(f"BUY {p['ticker']}  {p['shares']} sh")
        out.append(f"  now      ${p['price']:.2f}")
        if p.get("band"):
            lo, hi = p["price"] - p["band"], p["price"] + p["band"]
            # Not a forecast. This is the stock's own typical daily range, so
            # you know what a normal close looks like versus a real move.
            out.append(f"  fills at today's close; typical range "
                       f"${lo:.2f}-${hi:.2f}")
        else:
            out.append("  fills at today's close")
        out.append(f"  cost     ${p['cost']:,.0f}")
        if p.get("stop"):
            dn = 100 * (p["stop"] / p["price"] - 1)
            out.append(f"  STOP     ${p['stop']:.2f} ({dn:+.1f}%)  "
                       f"risk ${abs(p['risk']):,.0f}")
        if p.get("target"):
            up = 100 * (p["target"] / p["price"] - 1)
            out.append(f"  TARGET   ${p['target']:.2f} ({up:+.1f}%)  "
                       f"profit ${p['reward']:,.0f}")
        if p.get("stop") and p.get("target"):
            rr = (p["target"] - p["price"]) / max(p["price"] - p["stop"], 0.01)
            out.append(f"  R:R      {rr:.1f}:1")
        out.append(f"  why      {p['who'][:30]} ({p['role'][:20]}) {p['filed']}")
        out.append("")

    out.append("Enter before 4pm ET to fill at today's close.")
    out.append("Stops/targets are from each stock's own recent volatility,")
    out.append("not a price prediction. Nobody can forecast a close.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="insider_big", choices=sorted(STRATEGIES))
    ap.add_argument("--positions", type=int, default=10)
    ap.add_argument("--since", type=int, default=1)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--send", action="store_true", help="push to the alert channels")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    picks = build(args.strategy, args.positions, args.since, args.limit)
    text = render(picks, args.strategy, args.positions)
    print("\n" + text + "\n")

    if not args.send:
        return 0

    settings = load_settings().get("notify", {})
    if settings.get("discord", {}).get("enabled"):
        discord.send_text(os.environ.get("DISCORD_WEBHOOK_URL", ""), text)
    if settings.get("ntfy", {}).get("enabled"):
        topic = os.environ.get("NTFY_TOPIC", "")
        if topic:
            ntfy.send_text(topic, text, "SMG picks — enter before 4pm ET",
                           priority="high")
    if settings.get("mac_agent", {}).get("enabled") and picks:
        # One line per pick, so the Mac agent texts something readable.
        summary = "SMG: " + ", ".join(f"{p['ticker']} x{p['shares']}" for p in picks)
        outbox.append(OUTBOX_PATH, [Trade(
            source="smg", uid=f"smg:{date.today()}:{args.strategy}",
            person="SMG digest", action="BUY", company=summary[:90],
            filed_date=date.today(),
        )])
    log.info("sent to enabled channels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
