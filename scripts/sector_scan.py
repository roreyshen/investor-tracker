#!/usr/bin/env python3
"""Recent insider buying, filtered by sector or industry.

Deliberately not a stock picker. It shows where people with a legal duty to
disclose have been putting their own money, and leaves the judgement to you.
Theme-driven buying ("war stocks", "approval plays") is the kind of trade the
walk-forward in this repo has nothing good to say about -- but insider
purchases inside a theme are at least evidence rather than a headline.

    python scripts/sector_scan.py --sector "Health Care" --days 30
    python scripts/sector_scan.py --industry oil --days 45
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import FUNDAMENTALS_PATH, TRADES_PATH, UNIVERSE_PATH  # noqa: E402
from src.fundamentals import Fundamentals  # noqa: E402
from src.prices import fetch_universe  # noqa: E402
from scripts.backtest import eligible  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sector", default="")
    ap.add_argument("--industry", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-value", type=float, default=100_000)
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    rows = store.load(TRADES_PATH)
    uni = fetch_universe(UNIVERSE_PATH)
    fund = Fundamentals(FUNDAMENTALS_PATH)
    cutoff = (date.today() - timedelta(days=args.days)).isoformat()

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tk = r.get("ticker")
        if (not tk or r.get("action") != "BUY" or r.get("code") != "P"
                or (r.get("filed_date") or "") < cutoff):
            continue
        info = uni.get(tk)
        if not info or not eligible(info):
            continue
        if args.sector and args.sector.lower() not in info.get("sector", "").lower():
            continue
        if args.industry and args.industry.lower() not in info.get("industry", "").lower():
            continue
        if (r.get("value_usd") or 0) < args.min_value:
            continue
        by_ticker[tk].append(r)

    scored = []
    for tk, trades in by_ticker.items():
        info = uni[tk]
        scored.append({
            "ticker": tk, "name": info.get("name", "")[:38],
            "industry": info.get("industry", "")[:30],
            "price": info.get("price"),
            "buys": len(trades),
            "insiders": len({t.get("person") for t in trades}),
            "total": sum(t.get("value_usd") or 0 for t in trades),
            "latest": max(t.get("filed_date") or "" for t in trades),
            "who": max(trades, key=lambda t: t.get("value_usd") or 0).get("person", "")[:26],
        })
    # Distinct insiders first: several people buying independently is the
    # strongest pattern in the literature, not one large cheque.
    scored.sort(key=lambda s: (-s["insiders"], -s["total"]))

    label = args.sector or args.industry or "all sectors"
    print(f"\n  Insider open-market BUYS — {label}, last {args.days} days, "
          f"min ${args.min_value:,.0f}\n")
    if not scored:
        print("  Nothing matches. Try a longer window or a broader filter.\n")
        return 0
    print(f"  {'tkr':6} {'price':>8} {'ppl':>4} {'buys':>5} {'total':>11}  "
          f"{'P/E':>7}  {'latest':10} company / industry")
    for s in scored[:args.limit]:
        pe, note = fund.pe(s["ticker"], s["price"] or 0)
        pe_txt = f"{pe}" if pe else (note[:7] if note else "-")
        print(f"  {s['ticker']:6} ${s['price'] or 0:7.2f} {s['insiders']:>4} "
              f"{s['buys']:>5} ${s['total']:>10,.0f}  {pe_txt:>7}  {s['latest']:10} "
              f"{s['name']}")
        print(f"         {s['industry']}  |  largest: {s['who']}")
    fund.save()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
