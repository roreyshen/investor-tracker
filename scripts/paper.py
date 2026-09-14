#!/usr/bin/env python3
"""Forward paper-trading log — out-of-sample evidence, recorded daily.

The backtest has three weaknesses no amount of care removes: it is in-sample,
it silently excludes ~12.6% of signals on tickers that no longer exist
(survivorship bias), and its holding period and position count were chosen by
me after seeing the data.

A forward log has none of those. It commits to a pick BEFORE the outcome
exists, prices it at the next real close, and cannot quietly drop a company
that later fails. Run daily, it produces the only evidence here that is
honestly out-of-sample -- and by the time an SMG session starts there is a
track record instead of a hypothesis.

    python scripts/paper.py --strategy insider_big        # record + mark
    python scripts/paper.py --report
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, PRICES_PATH  # noqa: E402
from src.prices import PriceStore  # noqa: E402
from scripts.smg_digest import PORTFOLIO, build  # noqa: E402

log = logging.getLogger("paper")
PAPER_DIR = DATA_DIR / "paper"


def path_for(strategy: str) -> Path:
    return PAPER_DIR / f"{strategy}.json"


def load(strategy: str) -> dict:
    p = path_for(strategy)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("paper log unreadable, starting fresh")
    return {"strategy": strategy, "started": date.today().isoformat(),
            "cash": PORTFOLIO, "open": [], "closed": [], "marks": []}


def save(strategy: str, data: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    p = path_for(strategy)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(p)


def run(strategy: str, positions: int, hold_days: int, limit: int,
        since_days: int = 4) -> dict:
    data = load(strategy)
    px = PriceStore(PRICES_PATH)
    today = date.today()

    # 1. Close anything past its holding period, at a real close.
    still = []
    for pos in data["open"]:
        opened = date.fromisoformat(pos["opened"])
        if (today - opened).days >= hold_days:
            out = px.close_on_or_before(pos["ticker"], today, 6)
            if out:
                proceeds = pos["shares"] * out
                data["cash"] += proceeds
                data["closed"].append({**pos, "exit": round(out, 4),
                                       "closed": today.isoformat(),
                                       "pnl": round(proceeds - pos["cost"], 2)})
                continue
        still.append(pos)
    data["open"] = still

    # 2. Fill yesterday's picks at a real close. Entries are never priced on
    #    the day they were generated -- that would be the lookahead this whole
    #    exercise exists to avoid.
    for pend in data.get("pending", []):
        entry = px.close_on_or_before(pend["ticker"], today, 4)
        if not entry or entry <= 0:
            continue
        slice_size = PORTFOLIO / max(positions, 1)
        shares = int(min(slice_size, data["cash"]) // entry)
        if shares < 1:
            continue
        cost = shares * entry
        data["cash"] -= cost
        data["open"].append({"ticker": pend["ticker"], "shares": shares,
                             "entry": round(entry, 4), "cost": round(cost, 2),
                             "opened": today.isoformat(),
                             "why": pend.get("why", "")})
    data["pending"] = []

    # 3. Record today's picks as pending for the next session.
    held = {p["ticker"] for p in data["open"]}
    room = max(0, positions - len(data["open"]))
    if room:
        for p in build(strategy, positions, since_days, limit):
            if p["ticker"] in held or room <= 0:
                continue
            data["pending"].append({"ticker": p["ticker"],
                                    "picked": today.isoformat(),
                                    "why": f"{p['who']} ({p['role']})"[:80]})
            room -= 1

    # 4. Mark to market.
    held_value = 0.0
    for pos in data["open"]:
        mark = px.close_on_or_before(pos["ticker"], today, 6) or pos["entry"]
        held_value += pos["shares"] * mark
    equity = data["cash"] + held_value

    spy = px.close_on_or_before("SPY", today, 6)
    data["marks"] = [m for m in data["marks"] if m["date"] != today.isoformat()]
    data["marks"].append({"date": today.isoformat(),
                          "equity": round(equity, 2),
                          "spy": round(spy, 4) if spy else None})
    px.save()
    save(strategy, data)
    return data


def report(strategy: str) -> None:
    data = load(strategy)
    marks = data.get("marks", [])
    if not marks:
        print(f"\n  No marks yet for {strategy}. Run without --report first.\n")
        return
    first, last = marks[0], marks[-1]
    ret = 100 * (last["equity"] / PORTFOLIO - 1)
    line = (f"  {strategy}: ${last['equity']:,.0f} ({ret:+.2f}%) "
            f"over {len(marks)} session(s) since {data['started']}")
    if first.get("spy") and last.get("spy"):
        bench = 100 * (last["spy"] / first["spy"] - 1)
        line += f"  |  SPY {bench:+.2f}%  |  edge {ret - bench:+.2f}pp"
    print("\n" + line)
    print(f"  open {len(data['open'])}  closed {len(data['closed'])}  "
          f"pending {len(data.get('pending', []))}  cash ${data['cash']:,.0f}")
    wins = [c for c in data["closed"] if c["pnl"] > 0]
    if data["closed"]:
        print(f"  win rate {100*len(wins)/len(data['closed']):.0f}% "
              f"({len(wins)}/{len(data['closed'])})")
    print("  NOTE: too few sessions to mean anything until this runs for weeks.\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="insider_big")
    ap.add_argument("--positions", type=int, default=10)
    ap.add_argument("--hold", type=int, default=63)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--since", type=int, default=4,
                    help="lookback for new picks; 4 covers a weekend gap so "
                         "Monday still sees Friday's filings")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.report:
        report(args.strategy)
        return 0

    data = run(args.strategy, args.positions, args.hold, args.limit,
               args.since)
    log.info("%s: open=%d pending=%d closed=%d cash=$%s",
             args.strategy, len(data["open"]), len(data.get("pending", [])),
             len(data["closed"]), f"{data['cash']:,.0f}")
    report(args.strategy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
