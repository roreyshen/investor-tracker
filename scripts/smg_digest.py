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
from src.prices import fetch_universe  # noqa: E402
from scripts.backtest import (COMMISSION, SMG_MIN_MCAP, SMG_MIN_PRICE,  # noqa: E402
                              eligible, find_clusters)
from scripts.daily_picks import STRATEGIES  # noqa: E402

log = logging.getLogger("smg")
ET = ZoneInfo("America/New_York")
PORTFOLIO = 100_000.0


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
    wl = Watchlist(load_watchlist())
    clusters = find_clusters(rows)
    fn = STRATEGIES[strategy]

    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
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
        picks.append({
            "ticker": tk, "price": price, "shares": shares,
            "cost": round(shares * price, 2),
            "company": (info.get("name") or r.get("company", ""))[:44],
            "who": r.get("person", "?"), "role": r.get("role") or r["source"],
            "filed": r.get("filed_date"), "url": r.get("url", ""),
            "value": r.get("value_usd") or r.get("value_range") or "",
        })

    # Biggest disclosed conviction first, so a short list is the strongest one.
    picks.sort(key=lambda p: -(p["cost"]))
    return picks[:limit]


def render(picks, strategy: str, positions: int) -> str:
    status = market_status()
    if not picks:
        return (f"SMG digest — no qualifying signals today ({strategy}).\n"
                f"{status}.\nA quiet day is normal; forcing a trade is how you lose.")
    lines = [f"SMG picks — {strategy} · {status}",
             f"Equal weight, {positions} positions (${PORTFOLIO/positions:,.0f} each)", ""]
    for p in picks:
        lines.append(f"{p['ticker']}  {p['shares']} sh @ ${p['price']:.2f} "
                     f"= ${p['cost']:,.0f}")
        lines.append(f"   {p['who'][:34]} ({p['role'][:22]}) · filed {p['filed']}")
    lines.append("")
    lines.append("Enter before 4pm ET to fill at today's close.")
    return "\n".join(lines)


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
