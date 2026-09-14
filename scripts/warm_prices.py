#!/usr/bin/env python3
"""Pre-fetch prices for the tickers that carry the most signal.

The backtest fetches lazily, which means a run silently trades only the
tickers it happened to have prices for -- a biased subset masquerading as a
result. Warming ahead of time makes coverage a number you choose rather than
an accident of iteration order.

    python scripts/warm_prices.py --top 1500
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import PRICES_PATH, TRADES_PATH, UNIVERSE_PATH  # noqa: E402
from src.prices import BENCHMARKS, PriceStore, fetch_universe  # noqa: E402
from scripts.backtest import eligible  # noqa: E402

log = logging.getLogger("warm")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=1200)
    ap.add_argument("--days", type=int, default=800)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    rows = store.load(TRADES_PATH)
    universe = fetch_universe(UNIVERSE_PATH)
    px = PriceStore(PRICES_PATH)
    since = date.today() - timedelta(days=args.days)

    for b in BENCHMARKS:
        px.history(b, since)

    counts = Counter(r["ticker"] for r in rows
                     if r.get("ticker") and r.get("filed_date")
                     and eligible(universe.get(r["ticker"], {})))
    wanted = [t for t, _ in counts.most_common(args.top)]
    since_key = since.isoformat()

    def needs_work(t: str) -> bool:
        """Missing, or cached but not reaching far enough back.

        Only checking presence was wrong: a ticker cached with two years of
        history looks 'done' while a ten-year backtest silently gets nothing
        from it before 2024.
        """
        e = px.data.get(t)
        if not e or not e.get("closes"):
            return True
        asked = e.get("earliest_requested")
        if asked is not None and asked <= since_key:
            return False
        return min(e["closes"]) > since_key

    todo = [t for t in wanted if needs_work(t)]
    log.info("%d eligible tickers, %d already deep enough, fetching %d "
             "(history back to %s)",
             len(counts), len(wanted) - len(todo), len(todo), since_key)

    for i, tk in enumerate(todo, 1):
        px.history(tk, since)
        if i % 50 == 0:
            px.save()
            log.info("  %d/%d (cache=%d)", i, len(todo), len(px.data))
    px.save()
    log.info("done, %d tickers cached", len(px.data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
