#!/usr/bin/env python3
"""Today's SMG-eligible buy candidates, ranked.

This is the handoff between analysis and action: whatever you or a bot enters
at the close comes from here. It is deliberately separate from alerting --
an alert says "this happened", a pick says "this is worth buying today".

Strategy is a parameter, not a hardcoded assumption, so it can be pointed at
whichever approach the backtest actually supports.

    python scripts/daily_picks.py --strategy insider_cluster
    python scripts/daily_picks.py --since 3 --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import TRADES_PATH, UNIVERSE_PATH, load_watchlist  # noqa: E402
from src.filters import Watchlist, is_senior, range_low  # noqa: E402
from src.prices import fetch_universe  # noqa: E402
from scripts.backtest import (SENIOR, SMG_MIN_MCAP, SMG_MIN_PRICE,  # noqa: E402
                              eligible, find_clusters)

log = logging.getLogger("picks")

STRATEGIES = {
    "congress":        lambda r, w, c: r["source"] in ("house", "senate") and r["action"] == "BUY",
    "congress_big":    lambda r, w, c: (r["source"] in ("house", "senate") and r["action"] == "BUY"
                                        and range_low(r.get("value_range") or "") >= 50_000),
    "senate":          lambda r, w, c: r["source"] == "senate" and r["action"] == "BUY",
    "insider":         lambda r, w, c: r["source"] == "form4" and r["action"] == "BUY" and r.get("code") == "P",
    "insider_senior":  lambda r, w, c: (r["source"] == "form4" and r.get("code") == "P"
                                        and r["action"] == "BUY"
                                        and is_senior(r.get("role") or "", SENIOR)),
    "insider_big":     lambda r, w, c: (r["source"] == "form4" and r.get("code") == "P"
                                        and r["action"] == "BUY"
                                        and (r.get("value_usd") or 0) >= 1_000_000),
    "insider_cluster": lambda r, w, c: (r["source"] == "form4" and r.get("code") == "P"
                                        and r["action"] == "BUY"
                                        and (r.get("ticker"), (r.get("filed_date") or "")[:7]) in c),
    "watchlist":       lambda r, w, c: r["action"] == "BUY" and w.match(r.get("person") or "") is not None,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="insider_cluster", choices=sorted(STRATEGIES))
    ap.add_argument("--since", type=int, default=1,
                    help="include filings from the last N days")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    rows = store.load(TRADES_PATH)
    universe = fetch_universe(UNIVERSE_PATH)
    wl = Watchlist(load_watchlist())
    clusters = find_clusters(rows)
    fn = STRATEGIES[args.strategy]

    cutoff = (date.today() - timedelta(days=args.since)).isoformat()
    picks, seen = [], set()
    for r in rows:
        if (r.get("filed_date") or "") < cutoff or not r.get("ticker"):
            continue
        info = universe.get(r["ticker"], {})
        if not eligible(info) or not fn(r, wl, clusters):
            continue
        if r["ticker"] in seen:          # one position per ticker
            continue
        seen.add(r["ticker"])
        picks.append({
            "ticker": r["ticker"],
            "company": info.get("name") or r.get("company", ""),
            "price": info.get("price"),
            "sector": info.get("sector", "Unknown"),
            "why": f"{r.get('person', '?')} ({r.get('role') or r['source']})",
            "filed": r.get("filed_date"),
            "value": r.get("value_usd") or r.get("value_range") or "",
            "url": r.get("url", ""),
        })

    picks.sort(key=lambda p: -(p["price"] or 0))
    picks = picks[:args.limit]

    if args.json:
        print(json.dumps({"strategy": args.strategy, "generated": date.today().isoformat(),
                          "picks": picks}, indent=1))
        return 0

    print(f"\n  SMG picks — strategy: {args.strategy}, filings from the last "
          f"{args.since} day(s)")
    print(f"  Eligible: NYSE/NASDAQ, >= ${SMG_MIN_PRICE}/share, "
          f">= ${SMG_MIN_MCAP/1e6:.0f}M cap\n")
    if not picks:
        print("  Nothing qualifies today. That is a normal outcome — filings are\n"
              "  lumpy and forcing a trade on a quiet day is how you lose.\n")
        return 0
    for p in picks:
        px = f"${p['price']:.2f}" if p["price"] else "?"
        print(f"  {p['ticker']:6} {px:>9}  {p['company'][:34]:36} {p['sector'][:18]}")
        print(f"         {p['why'][:70]}  (filed {p['filed']})")
    print(f"\n  {len(picks)} candidate(s). Enter at the close; SMG fills after "
          "4pm ET at the next close.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
