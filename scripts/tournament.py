#!/usr/bin/env python3
"""Tournament analysis: the distribution of 10-week outcomes, not the average.

SMG is ranked against other teams, so "average return" is the wrong objective.
Finishing average loses. What matters is the probability of a top-decile
result, which depends on the SPREAD of outcomes at least as much as the mean.

This rolls a session-length window through history, runs a strategy in each,
and reports the distribution -- median, best and worst decile, and how often
the result clears thresholds that would plausibly win.

The uncomfortable implication is stated rather than hidden: when a signal's
edge is weak, concentration raises your chance of winning AND your chance of
finishing last, because you are buying variance rather than edge.

    python scripts/tournament.py --session 70 --strategy insider_big
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import (PRICES_PATH, TRADES_PATH, UNIVERSE_PATH,  # noqa: E402
                        load_watchlist)
from src.filters import Watchlist  # noqa: E402
from src.prices import PriceStore, fetch_universe  # noqa: E402
from scripts.backtest import (build_signals, buy_and_hold, find_clusters,  # noqa: E402
                              make_strategies, prepare, run)

log = logging.getLogger("tournament")


def pctile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return round(s[k], 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=int, default=70,
                    help="session length in days (10 weeks = 70)")
    ap.add_argument("--step", type=int, default=14,
                    help="how far to slide the window between samples")
    ap.add_argument("--holds", type=int, nargs="+", default=[14, 21, 35, 70])
    ap.add_argument("--positions", type=int, nargs="+", default=[3, 5, 10, 20])
    ap.add_argument("--history", type=int, default=700)
    ap.add_argument("--out", default="docs/tournament.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    rows = store.load(TRADES_PATH)
    universe = fetch_universe(UNIVERSE_PATH)
    px = PriceStore(PRICES_PATH)
    wl = Watchlist(load_watchlist())
    clusters = find_clusters(rows)
    strategies = make_strategies(wl, clusters)

    hist_end = date.today() - timedelta(days=1)
    hist_start = hist_end - timedelta(days=args.history)

    windows = []
    cur = hist_start
    while cur + timedelta(days=args.session) <= hist_end:
        windows.append((cur, cur + timedelta(days=args.session)))
        cur += timedelta(days=args.step)
    log.info("%d overlapping %d-day sessions from %s to %s",
             len(windows), args.session, hist_start, hist_end)

    bench = []
    for w0, w1 in windows:
        b = buy_and_hold(px, w0, w1, "SPY")
        if b:
            bench.append(b["return_pct"])
    log.info("SPY across those sessions: median %+.2f%%  p90 %+.2f%%",
             pctile(bench, 50) or 0, pctile(bench, 90) or 0)

    out = {}
    for name, fn in strategies.items():
        for hold in args.holds:
            for npos in args.positions:
                rets = []
                for w0, w1 in windows:
                    prep = prepare(rows, universe, w0, w1)
                    sig = build_signals(prep, fn)
                    if not sig:
                        continue
                    r = run(rows, px, universe, fn, hold, npos, w0, w1, signals=sig)
                    if r["trades"]:
                        rets.append(r["return_pct"])
                if len(rets) < 5:
                    continue
                key = f"{name} | hold {hold}d | {npos} pos"
                out[key] = {
                    "strategy": name, "hold": hold, "positions": npos,
                    "sessions": len(rets),
                    "median": pctile(rets, 50), "p10": pctile(rets, 10),
                    "p90": pctile(rets, 90), "best": round(max(rets), 2),
                    "worst": round(min(rets), 2),
                    "stdev": round(statistics.pstdev(rets), 2) if len(rets) > 1 else 0,
                    # A rough proxy for "would this have won": how often the
                    # session cleared a big number.
                    "pct_over_20": round(100 * sum(1 for v in rets if v > 20) / len(rets), 1),
                    "pct_over_40": round(100 * sum(1 for v in rets if v > 40) / len(rets), 1),
                    "pct_negative": round(100 * sum(1 for v in rets if v < 0) / len(rets), 1),
                }
        px.save()
        log.info("done %s", name)

    ranked = sorted(out.items(), key=lambda kv: -(kv[1]["pct_over_20"]))
    log.info("")
    log.info("%-46s %7s %7s %7s %7s %7s", "setting", "median", "p90", ">20%", ">40%", "<0%")
    for k, v in ranked[:14]:
        log.info("%-46s %+6.1f%% %+6.1f%% %6.1f%% %6.1f%% %6.1f%%",
                 k, v["median"], v["p90"], v["pct_over_20"],
                 v["pct_over_40"], v["pct_negative"])

    payload = {"generated": date.today().isoformat(), "session_days": args.session,
               "windows": len(windows), "spy_median": pctile(bench, 50),
               "spy_p90": pctile(bench, 90), "settings": out}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
