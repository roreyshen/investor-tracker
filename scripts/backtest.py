#!/usr/bin/env python3
"""Portfolio backtest under Stock Market Game rules.

Averages of per-trade excess returns hide the things that decide a
competition: how often a signal fires, whether you had cash when it did, and
how much a few bad positions hurt. This simulates an actual portfolio instead.

Rules modelled (SMG):
  * $100,000 starting cash
  * fills at the NEXT close after a filing is public -- never the same close,
    which would be lookahead, and never the trade date, which is unbuyable
  * NYSE/NASDAQ only, >= $3/share, >= $25M market cap
  * long only, equal weight, a cap on simultaneous positions
  * fixed holding period, then sell at the close

Deliberately NOT modelled: slippage and commissions (SMG is a simulator),
survivorship in the ticker universe, and dividends. All three flatter the
results slightly.

    python scripts/backtest.py
    python scripts/backtest.py --hold 63 --positions 10
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import PRICES_PATH, TRADES_PATH, UNIVERSE_PATH  # noqa: E402
from src.filters import Watchlist, is_senior, range_low  # noqa: E402
from src.config import load_watchlist  # noqa: E402
from src.prices import PriceStore, fetch_universe  # noqa: E402

log = logging.getLogger("backtest")

START_CASH = 100_000.0
SMG_MIN_PRICE, SMG_MIN_MCAP = 3.0, 25_000_000

# SMG charges a FLAT $5 per trade, not a percentage. That is the whole
# argument against many small positions: $10 round-trip is 0.05% of a $20k
# position and 0.5% of a $2k one. Cash earns 0.75% annualised, well below
# market, so idle cash is a real drag rather than a neutral choice.
COMMISSION = 5.0
CASH_APY = 0.0075
SENIOR = ["CEO", "Chief Executive", "CFO", "Chief Financial", "President",
          "Chairman", "Chief Operating", "COO"]


def eligible(info: dict) -> bool:
    p, m = info.get("price"), info.get("market_cap")
    return p is not None and p >= SMG_MIN_PRICE and m is not None and m >= SMG_MIN_MCAP


# ── Strategies: each returns True if this filing is a BUY signal ───────────
def make_strategies(wl: Watchlist, clusters: set[tuple[str, str]]):
    def congress(r):
        return r["source"] in ("house", "senate") and r["action"] == "BUY"

    def senate(r):
        return r["source"] == "senate" and r["action"] == "BUY"

    def congress_big(r):
        return (r["source"] in ("house", "senate") and r["action"] == "BUY"
                and range_low(r.get("value_range") or "") >= 50_000)

    def _discretionary(r):
        # Exclude pre-scheduled 10b5-1 trades: they execute on a calendar, not
        # on a view, so they are noise in a conviction signal.
        return "10b5-1" not in (r.get("note") or "")

    def insider(r):
        return (r["source"] == "form4" and r["action"] == "BUY"
                and r.get("code") == "P" and _discretionary(r))

    def insider_senior(r):
        return insider(r) and is_senior(r.get("role") or "", SENIOR)

    def insider_big(r):
        return insider(r) and (r.get("value_usd") or 0) >= 1_000_000

    def insider_cluster(r):
        return insider(r) and (r.get("ticker"), r.get("filed_date", "")[:7]) in clusters

    def watchlist(r):
        return r["action"] == "BUY" and wl.match(r.get("person") or "") is not None

    return {
        "Congress (all buys)": congress,
        "Congress ($50k+)": congress_big,
        "Senate only": senate,
        "Insider buys (code P)": insider,
        "Insider: CEO/CFO only": insider_senior,
        "Insider: $1M+": insider_big,
        "Insider: cluster buys": insider_cluster,
        "Your watchlist": watchlist,
    }


def find_clusters(rows: list[dict], window_days: int = 7, min_insiders: int = 3):
    """(ticker, YYYY-MM) pairs where 3+ distinct insiders bought in a window."""
    by_ticker = defaultdict(list)
    for r in rows:
        if r["source"] == "form4" and r.get("code") == "P" and r.get("ticker") and r.get("filed_date"):
            by_ticker[r["ticker"]].append((r["filed_date"], r.get("person")))
    out = set()
    for tk, events in by_ticker.items():
        events.sort()
        for i, (d0, _) in enumerate(events):
            try:
                start = date.fromisoformat(d0)
            except ValueError:
                continue
            people = set()
            for d1, p in events[i:]:
                try:
                    if (date.fromisoformat(d1) - start).days > window_days:
                        break
                except ValueError:
                    continue
                people.add(p)
            if len(people) >= min_insiders:
                out.add((tk, d0[:7]))
    return out


def conviction(r: dict) -> float:
    """Rank signals when there are more than there are slots.

    Ten positions and a 63-day hold means most signals are never acted on, so
    taking whichever arrived first makes arrival order the strategy. Ranking
    by dollar size, seniority and cluster membership at least tests the signal
    rather than the calendar.
    """
    v = float(r.get("value_usd") or range_low(r.get("value_range") or "") or 0)
    score = v
    if is_senior(r.get("role") or "", SENIOR):
        score *= 2.0
    if r.get("source") == "senate":
        score *= 1.2
    return score


def next_close(px: PriceStore, tk: str, when: date, window: int = 6):
    """First close strictly AFTER `when` -- a same-day fill would be lookahead."""
    return px.close_on_or_after(tk, when + timedelta(days=1), window)


def survivorship(rows, universe, start, end) -> dict:
    """How much of the signal set is invisible to this backtest, and why.

    The ticker universe is a snapshot of what is listed TODAY, so anything
    delisted, acquired or moved to OTC since is absent and gets skipped. That
    is survivorship bias: failures are quietly excluded, which flatters
    results. Acquisitions (often at a premium) are excluded too, pushing the
    other way, so the net direction is not certain -- but the size of the hole
    should never be hidden.
    """
    lo, hi = start.isoformat(), end.isoformat()
    inwin = [r for r in rows if r.get("ticker") and r.get("filed_date")
             and lo <= r["filed_date"] <= hi]
    missing = sum(1 for r in inwin if r["ticker"] not in universe)
    inelig = sum(1 for r in inwin if r["ticker"] in universe
                 and not eligible(universe[r["ticker"]]))
    return {
        "signals_in_window": len(inwin),
        "ticker_not_listed_today": missing,
        "listed_but_ineligible": inelig,
        "tradeable": len(inwin) - missing - inelig,
        "delisted_pct": round(100 * missing / len(inwin), 1) if inwin else None,
    }


def prepare(rows, universe, start, end):
    """Parse and filter once, not once per parameter combination.

    The sweep runs 96 simulations. Re-parsing 77,000 rows and re-checking
    eligibility inside each one cost more than the simulation itself.
    """
    out = []
    for r in rows:
        fd = r.get("filed_date")
        tk = r.get("ticker")
        if not fd or not tk:
            continue
        try:
            d = date.fromisoformat(fd)
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        if not eligible(universe.get(tk, {})):
            continue
        out.append((d, r))
    out.sort(key=lambda x: x[0])
    return out


def build_signals(prepared, signal):
    """Group one strategy's signals by day, strongest first within a day."""
    by_day = defaultdict(list)
    for d, r in prepared:
        if signal(r):
            by_day[d].append(r)
    for d in by_day:
        by_day[d].sort(key=conviction, reverse=True)
    return by_day


def run(rows, px, universe, signal, hold_days, max_positions, start, end,
        signals=None):
    cash, positions, closed = START_CASH, [], []
    equity_curve = []
    skipped_no_price = 0
    considered = 0

    if signals is None:
        signals = build_signals(prepare(rows, universe, start, end), signal)

    day = start
    while day <= end:
        # Exit anything whose holding period is up.
        still = []
        for p in positions:
            if (day - p["opened"]).days >= hold_days:
                out_px = px.close_on_or_before(p["ticker"], day, 6)
                if out_px:
                    proceeds = p["shares"] * out_px - COMMISSION
                    cash += proceeds
                    closed.append({**p, "exit": out_px, "closed": day,
                                   "pnl": proceeds - p["cost"]})
                    continue
            still.append(p)
        positions = still

        # Enter new signals, strongest first, equal weight.
        for r in signals.get(day, ()):
            if len(positions) >= max_positions:
                break
            if any(p["ticker"] == r["ticker"] for p in positions):
                continue
            considered += 1
            entry = next_close(px, r["ticker"], day)
            if not entry or entry <= 0:
                skipped_no_price += 1
                continue
            size = min(START_CASH / max_positions, cash)
            if size < entry:
                continue
            shares = size // entry
            if shares < 1:
                continue
            cost = shares * entry + COMMISSION
            if cost > cash:
                continue
            cash -= cost
            positions.append({"ticker": r["ticker"], "shares": shares,
                              "entry": entry, "cost": cost, "opened": day,
                              "person": r.get("person")})

        # Interest on idle cash, credited daily at the stated annual rate.
        if day.weekday() < 5 and cash > 0:
            cash *= (1 + CASH_APY / 252)

        if day.weekday() < 5:
            held = 0.0
            for p in positions:
                mark = px.close_on_or_before(p["ticker"], day, 6) or p["entry"]
                held += p["shares"] * mark
            equity_curve.append((day.isoformat(), round(cash + held, 2)))
        day += timedelta(days=1)

    # Liquidate whatever is open at the end.
    for p in positions:
        out_px = px.close_on_or_before(p["ticker"], end, 8) or p["entry"]
        proceeds = p["shares"] * out_px - COMMISSION
        cash += proceeds
        closed.append({**p, "exit": out_px, "closed": end,
                       "pnl": proceeds - p["cost"]})

    final = cash
    wins = [c for c in closed if c["pnl"] > 0]
    peak, dd = START_CASH, 0.0
    for _, v in equity_curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1.0)

    return {
        "final": round(final, 2),
        "return_pct": round(100 * (final / START_CASH - 1), 2),
        "trades": len(closed),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "avg_pnl": round(statistics.fmean([c["pnl"] for c in closed]), 2) if closed else None,
        "max_drawdown_pct": round(100 * dd, 2),
        # Coverage matters: a backtest that silently drops the signals it has
        # no price for is reporting on a biased subset, not the strategy.
        "commission_paid": round(COMMISSION * (len(closed) * 2), 2),
        "considered": considered,
        "skipped_no_price": skipped_no_price,
        "coverage_pct": round(100 * (1 - skipped_no_price / considered), 1)
                        if considered else None,
        "equity": equity_curve,
    }


def buy_and_hold(px, start, end, symbol="SPY"):
    entry = next_close(px, symbol, start, 8)
    exit_ = px.close_on_or_before(symbol, end, 8)
    if not entry or not exit_:
        return None
    shares = START_CASH // entry
    final = START_CASH - shares * entry + shares * exit_
    return {"final": round(final, 2),
            "return_pct": round(100 * (final / START_CASH - 1), 2),
            "trades": 1, "win_rate": None, "avg_pnl": None,
            "max_drawdown_pct": None, "equity": []}


def sweep(rows, px, universe, wl, clusters, start, end, out_path):
    """Run every strategy across several hold/position settings.

    A single backtest that beats the benchmark proves very little: test enough
    combinations and one will win by chance. Reporting the whole grid makes
    that visible -- if a strategy only works at one specific holding period,
    that is a fitted parameter, not an edge.
    """
    holds, sizes = [21, 42, 63, 90], [5, 10, 20]
    strategies = make_strategies(wl, clusters)

    bench = {}
    for b in ("SPY", "QQQ"):
        r = buy_and_hold(px, start, end, b)
        if r:
            bench[b] = r["return_pct"]
    spy = bench.get("SPY", 0.0)
    log.info("benchmark over window: SPY %+.2f%%  QQQ %+.2f%%",
             spy, bench.get("QQQ", 0.0))

    prepared = prepare(rows, universe, start, end)
    log.info("%d eligible signals in window", len(prepared))

    grid, beats, total = {}, 0, 0
    for name, fn in strategies.items():
        sig = build_signals(prepared, fn)
        row = {}
        for h in holds:
            for n in sizes:
                r = run(rows, px, universe, fn, h, n, start, end, signals=sig)
                row[f"h{h}_p{n}"] = r["return_pct"]
                total += 1
                if r["return_pct"] > spy:
                    beats += 1
        vals = list(row.values())
        grid[name] = {"cells": row,
                      "best": round(max(vals), 2), "worst": round(min(vals), 2),
                      "median": round(statistics.median(vals), 2),
                      "beat_spy": sum(1 for v in vals if v > spy),
                      "of": len(vals)}
        log.info("%-24s median %+7.2f%%  range %+.2f%% .. %+.2f%%  beat SPY %d/%d",
                 name, grid[name]["median"], grid[name]["worst"],
                 grid[name]["best"], grid[name]["beat_spy"], grid[name]["of"])
        px.save()

    log.info("")
    log.info("%d of %d strategy/parameter combinations beat SPY (%.0f%%)",
             beats, total, 100 * beats / total if total else 0)
    log.info("Chance alone would produce some winners; a strategy that only "
             "beats the benchmark at one setting is a fitted parameter.")

    surv = survivorship(rows, universe, start, end)
    log.info("survivorship: %.1f%% of in-window signals are on tickers not "
             "listed today and were skipped", surv["delisted_pct"] or 0)

    payload = {"generated": date.today().isoformat(), "mode": "sweep",
               "start": start.isoformat(), "end": end.isoformat(),
               "benchmarks": bench, "holds": holds, "positions": sizes,
               "grid": grid, "beat_count": beats, "combinations": total,
               "survivorship": surv}
    Path(out_path).with_name("sweep.json").write_text(json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s", Path(out_path).with_name("sweep.json"))
    return 0


def walkforward(rows, px, universe, wl, clusters, start, end, out_path,
                splits: int = 3):
    """Pick a strategy on older data, then test that choice on newer data.

    This is the honest substitute for forward paper trading when there is no
    time to run one. A backtest that reports its best strategy is answering
    "what worked?", which is always knowable after the fact. The question that
    matters is "if I had CHOSEN on past data, would the choice have held up?"
    -- so each split selects a winner using only what came before, then scores
    it on the period after, which the selection never saw.

    If the in-sample winner keeps winning out-of-sample, that is evidence. If
    it does not, the single-window result was curve-fitting.
    """
    strategies = make_strategies(wl, clusters)
    span = (end - start).days
    seg = span // (splits + 1)
    results = []

    for i in range(splits):
        train_start = start
        train_end = start + timedelta(days=seg * (i + 1))
        test_end = min(end, train_end + timedelta(days=seg))
        if (test_end - train_end).days < 30:
            continue

        train_prep = prepare(rows, universe, train_start, train_end)
        scored = {}
        for name, fn in strategies.items():
            sig = build_signals(train_prep, fn)
            r = run(rows, px, universe, fn, 63, 10, train_start, train_end,
                    signals=sig)
            scored[name] = r["return_pct"]
        pick = max(scored, key=scored.get)

        test_prep = prepare(rows, universe, train_end, test_end)
        sig = build_signals(test_prep, strategies[pick])
        oos = run(rows, px, universe, strategies[pick], 63, 10,
                  train_end, test_end, signals=sig)
        bench = buy_and_hold(px, train_end, test_end, "SPY")
        bench_ret = bench["return_pct"] if bench else None

        # What would picking the WORST in-sample strategy have done? If the
        # spread between best and worst is noise, this lands close by.
        worst = min(scored, key=scored.get)
        sigw = build_signals(test_prep, strategies[worst])
        oos_worst = run(rows, px, universe, strategies[worst], 63, 10,
                        train_end, test_end, signals=sigw)

        results.append({
            "split": i + 1,
            "train": [train_start.isoformat(), train_end.isoformat()],
            "test": [train_end.isoformat(), test_end.isoformat()],
            "picked": pick, "train_return": round(scored[pick], 2),
            "test_return": oos["return_pct"], "test_trades": oos["trades"],
            "spy": bench_ret,
            "edge": None if bench_ret is None else round(oos["return_pct"] - bench_ret, 2),
            "worst_pick": worst,
            "worst_test_return": oos_worst["return_pct"],
        })
        log.info("split %d: trained %s..%s -> picked %-22s "
                 "in-sample %+7.2f%% | out-of-sample %+7.2f%% vs SPY %+7.2f%%",
                 i + 1, train_start, train_end, pick, scored[pick],
                 oos["return_pct"], bench_ret if bench_ret is not None else 0.0)
        px.save()

    wins = sum(1 for r in results if (r["edge"] or 0) > 0)
    log.info("")
    log.info("the in-sample winner beat SPY out-of-sample in %d of %d splits",
             wins, len(results))
    if results:
        avg = statistics.fmean(r["edge"] for r in results if r["edge"] is not None)
        log.info("average out-of-sample edge vs SPY: %+.2f pp", avg)
    log.info("A strategy chosen on past data that does not hold up on unseen "
             "data was curve-fitted, however good the headline number looked.")

    payload = {"generated": date.today().isoformat(), "mode": "walkforward",
               "splits": results, "oos_wins": wins, "of": len(results),
               "survivorship": survivorship(rows, universe, start, end)}
    Path(out_path).with_name("walkforward.json").write_text(
        json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s", Path(out_path).with_name("walkforward.json"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=63, help="holding period in days")
    ap.add_argument("--positions", type=int, default=10)
    ap.add_argument("--days", type=int, default=365, help="backtest window length")
    ap.add_argument("--out", default="docs/backtest.json")
    ap.add_argument("--walkforward", action="store_true",
                    help="choose a strategy on older data, score it on newer")
    ap.add_argument("--sweep", action="store_true",
                    help="test several hold/position combinations and report "
                         "how many beat the benchmark")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    rows = store.load(TRADES_PATH)
    universe = fetch_universe(UNIVERSE_PATH)
    px = PriceStore(PRICES_PATH)
    wl = Watchlist(load_watchlist())
    clusters = find_clusters(rows)
    log.info("%d stored trades, %d cluster windows", len(rows), len(clusters))

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days)

    if args.walkforward:
        return walkforward(rows, px, universe, wl, clusters, start, end, args.out)

    if args.sweep:
        return sweep(rows, px, universe, wl, clusters, start, end, args.out)

    results = {}
    for name, fn in make_strategies(wl, clusters).items():
        r = run(rows, px, universe, fn, args.hold, args.positions, start, end)
        results[name] = r
        log.info("%-24s %+7.2f%%  trades=%-4d win=%-5s maxDD=%-7s coverage=%s%%",
                 name, r["return_pct"], r["trades"], r["win_rate"],
                 r["max_drawdown_pct"], r["coverage_pct"])
        px.save()

    for bench in ("SPY", "QQQ", "DIA"):
        b = buy_and_hold(px, start, end, bench)
        if b:
            results[f"Buy & hold {bench}"] = b
            log.info("%-24s %+7.2f%%", f"Buy & hold {bench}", b["return_pct"])
    px.save()

    payload = {"generated": date.today().isoformat(),
               "start": start.isoformat(), "end": end.isoformat(),
               "hold_days": args.hold, "max_positions": args.positions,
               "start_cash": START_CASH, "results": results,
               "survivorship": survivorship(rows, universe, start, end)}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
