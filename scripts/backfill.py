#!/usr/bin/env python3
"""Populate the trade store with history, without sending any alerts.

The live bot only records what it sees from the moment it starts. This walks
the current year's congressional filings (and optionally recent Form 4s) so the
website has something to analyse on day one.

Deliberately separate from the alerting path: backfilling should never text or
post anything, and it writes to a throwaway state file so it can't mark live
filings as already-seen.

    python scripts/backfill.py                    # House + Senate, this year
    python scripts/backfill.py --form4-days 3
    python scripts/backfill.py --limit 50         # quick sample
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import TRADES_PATH, load_settings, load_watchlist  # noqa: E402
from src.filters import Watchlist, evaluate  # noqa: E402
from src.http import Fetcher  # noqa: E402
from src.sources import edgar_form4, house, senate  # noqa: E402
from src.state import State  # noqa: E402

log = logging.getLogger("backfill")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="cap filings per source (0 = all)")
    ap.add_argument("--form4-days", type=int, default=0,
                    help="also sweep N days of SEC Form 4 filings")
    ap.add_argument("--no-house", action="store_true")
    ap.add_argument("--no-senate", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    settings = load_settings()
    wl_cfg = load_watchlist()
    fetcher = Fetcher(settings.get("http", {}).get("user_agent",
                                                   "investor-tracker"),
                      per_second=settings.get("http", {}).get("requests_per_second", 5))

    # Throwaway state: backfill must not mark live filings as seen, or the
    # running bot would skip them and never alert.
    with tempfile.TemporaryDirectory() as tmp:
        st = State(Path(tmp) / "backfill.json")
        for src in ("house", "senate", "form4"):
            st.data["seen"][src] = {"__backfill__": 1}   # bypass cold-start priming

        trades = []
        if not args.no_house:
            trades += house.collect(fetcher, st, limit=args.limit)
        if not args.no_senate:
            trades += senate.collect(fetcher, st, limit=args.limit)
        if args.form4_days:
            trades += edgar_form4.collect(fetcher, st, backfill_days=args.form4_days,
                                          limit=args.limit)

    log.info("collected %d transactions", len(trades))
    if not trades:
        return 0

    alerts = evaluate(trades, settings, Watchlist(wl_cfg), [])
    added = store.append(TRADES_PATH, trades, {a.uid for a in alerts})
    log.info("stored %d new (%d would have alerted, none sent)", added, len(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
