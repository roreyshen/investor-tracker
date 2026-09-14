#!/usr/bin/env python3
"""Enrich stored trades with performance and generate the static dashboard.

The question the site answers is not "what did they trade" -- plenty of sites
show that -- but "did it beat just holding the index". So every trade with a
ticker and a date is scored against SPY, DIA and QQQ over the identical window.

Sign convention: a BUY is credited with the stock's excess return over the
benchmark; a SELL is credited with the negative of it, because avoiding a drop
is a good decision. That makes buys and sells comparable on one axis.

    python scripts/build_site.py                 # incremental
    python scripts/build_site.py --max-tickers 50
    python scripts/build_site.py --full          # re-score everything
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (PRICES_PATH, SITE_DIR, TRADES_PATH,  # noqa: E402
                        UNIVERSE_PATH)
from src.config import load_watchlist  # noqa: E402
from src.filters import Watchlist, range_mid  # noqa: E402
from src.prices import (BENCHMARKS, PriceStore, cap_tier,  # noqa: E402
                        fetch_universe)
from src import store  # noqa: E402

log = logging.getLogger("build_site")

# A trade needs some time to be judged; anything younger is recorded but not
# scored, or the averages are dominated by noise.
MIN_AGE_DAYS = 5
SOURCE_LABEL = {"house": "House", "senate": "Senate", "form4": "Insiders",
                "13f": "Funds", "form144": "Form 144"}

# Horizons measured from the ACTIONABLE entry, in trading-ish calendar days.
# 63d ~ a quarter, which is about the length of an SMG session.
HORIZONS = [7, 30, 63]

# Stock Market Game eligibility: NYSE/NASDAQ only, at least $3/share, at least
# $25M market cap. A signal on an ineligible security is useless in the game,
# so the page can filter them out entirely.
SMG_MIN_PRICE = 3.0
SMG_MIN_MCAP = 25_000_000


def pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100, 2)


def score_trades(rows: list[dict], px: PriceStore, universe: dict,
                 max_tickers: int) -> list[dict]:
    today = date.today()
    cutoff = today - timedelta(days=MIN_AGE_DAYS)

    scored: list[dict] = []
    # Group by ticker so each symbol's history is fetched once.
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tk = (r.get("ticker") or "").upper().strip()
        td = r.get("trade_date")
        if not tk or not td:
            continue
        try:
            if date.fromisoformat(td) > cutoff:
                continue
        except ValueError:
            continue
        by_ticker[tk].append(r)

    # Benchmarks first -- every trade needs them.
    earliest = today - timedelta(days=800)
    for b in BENCHMARKS:
        px.history(b, earliest)

    # The cap throttles NEW network fetches only. Anything already in the price
    # cache is always scored -- capping the scoring instead would make the site
    # silently shed trades on every run, which it did before this was fixed.
    cached = set(getattr(px, "data", {}))
    tickers = sorted(by_ticker, key=lambda t: -len(by_ticker[t]))
    budget = max_tickers or len(tickers)
    fetched = 0

    for n, tk in enumerate(tickers, 1):
        if tk not in cached:
            if fetched >= budget:
                continue
            fetched += 1
        group = by_ticker[tk]
        oldest = min(date.fromisoformat(r["trade_date"]) for r in group)
        px.history(tk, oldest)
        latest = px.latest(tk, oldest)
        if not latest:
            continue
        _, now_price = latest

        info = universe.get(tk, {})
        for r in group:
            td = date.fromisoformat(r["trade_date"])
            entry = px.close_on_or_after(tk, td)
            if not entry or entry <= 0:
                continue
            raw = now_price / entry - 1.0

            benches = {}
            for b in BENCHMARKS:
                b_entry = px.close_on_or_after(b, td)
                b_latest = px.latest(b)
                if b_entry and b_latest and b_entry > 0:
                    benches[b] = b_latest[1] / b_entry - 1.0

            sign = 1.0 if r.get("action") == "BUY" else -1.0
            spy = benches.get("SPY")
            excess = None if spy is None else sign * (raw - spy)

            # ── The number that actually matters for trading this ──────────
            # Entry at the first close on or after the filing became public --
            # the earliest price anyone could really have paid. Scoring from
            # the trade date flatters congressional filings enormously,
            # because it credits a 40-day head start nobody could act on.
            fd = r.get("filed_date")
            act_entry = act_ret = act_excess = None
            horizons = {}
            if fd:
                try:
                    fdate = date.fromisoformat(fd)
                except ValueError:
                    fdate = None
                if fdate:
                    act_entry = px.close_on_or_after(tk, fdate)
                    if act_entry and act_entry > 0:
                        act_ret = now_price / act_entry - 1.0
                        b_entry = px.close_on_or_after("SPY", fdate)
                        b_last = px.latest("SPY")
                        if b_entry and b_last and b_entry > 0:
                            act_excess = sign * (act_ret - (b_last[1] / b_entry - 1.0))
                        for h in HORIZONS:
                            end = fdate + timedelta(days=h)
                            if end > today:
                                continue
                            p_end = px.close_on_or_after(tk, end)
                            b_end = px.close_on_or_after("SPY", end)
                            b_st = px.close_on_or_after("SPY", fdate)
                            if p_end and b_end and b_st and b_st > 0:
                                stock = p_end / act_entry - 1.0
                                bench = b_end / b_st - 1.0
                                horizons[f"d{h}"] = pct(sign * (stock - bench))

            mcap = info.get("market_cap")
            last_px = info.get("price")
            smg_ok = (last_px is not None and last_px >= SMG_MIN_PRICE
                      and mcap is not None and mcap >= SMG_MIN_MCAP)

            scored.append({
                **{k: r.get(k) for k in
                   ("uid", "source", "person", "role", "ticker", "company",
                    "action", "trade_date", "filed_date", "value_usd",
                    "value_range", "code", "url", "alerted")},
                "entry_price": round(entry, 4),
                "current_price": round(now_price, 4),
                "return_pct": pct(raw),
                "signed_return_pct": pct(sign * raw),
                "bench_pct": {b: pct(v) for b, v in benches.items()},
                "excess_pct": pct(excess),
                # What you could actually have captured, entering at the first
                # close after the filing was public.
                "actionable_entry": round(act_entry, 4) if act_entry else None,
                "actionable_pct": pct(sign * act_ret) if act_ret is not None else None,
                "actionable_excess_pct": pct(act_excess),
                "horizons": horizons,
                "smg_eligible": smg_ok,
                "days_held": (today - td).days,
                "days_since_filing": (today - fdate).days if fd and fdate else None,
                "sector": info.get("sector", "Unknown"),
                "industry": info.get("industry", "Unknown"),
                "cap_tier": cap_tier(info.get("market_cap"), info.get("price")),
            })
        if n % 25 == 0:
            log.info("  %d/%d tickers (%d newly fetched this run)",
                     n, len(tickers), fetched)
            px.save()
    log.info("priced %d cached + %d new of %d tickers",
             len(cached & set(tickers)), fetched, len(tickers))
    return scored


def _agg(items: list[dict]) -> dict:
    ex = [t["excess_pct"] for t in items if t.get("excess_pct") is not None]
    act = [t["actionable_excess_pct"] for t in items
           if t.get("actionable_excess_pct") is not None]
    rets = [t["signed_return_pct"] for t in items
            if t.get("signed_return_pct") is not None]
    days = [t["days_held"] for t in items if t.get("days_held") is not None]
    if not ex:
        return {"trades": len(items), "avg_excess": None, "median_excess": None,
                "win_rate": None, "avg_return": None, "avg_days": None,
                "avg_actionable": None, "actionable_win": None,
                "actionable_n": 0, "horizons": {}}
    return {
        "trades": len(items),
        "avg_excess": round(statistics.fmean(ex), 2),
        "median_excess": round(statistics.median(ex), 2),
        # "Win" = beat the S&P over the same window, not merely went up.
        "win_rate": round(100 * sum(1 for v in ex if v > 0) / len(ex), 1),
        "avg_return": round(statistics.fmean(rets), 2) if rets else None,
        # Averages mix trades held 3 days with trades held 300. Surfacing the
        # mean window stops a short-window group being read as a fair
        # comparison against a long-window one.
        "avg_days": round(statistics.fmean(days)) if days else None,
        # The tradeable number: entering at the first close after the filing
        # was public. For congressional data this is usually far worse than
        # avg_excess, and that gap is the entire point of showing both.
        "avg_actionable": round(statistics.fmean(act), 2) if act else None,
        "actionable_win": round(100 * sum(1 for v in act if v > 0) / len(act), 1)
                          if act else None,
        "actionable_n": len(act),
        "horizons": _horizon_agg(items),
    }


def _horizon_agg(items: list[dict]) -> dict:
    """Average excess at fixed windows after the filing.

    A competition runs a fixed number of weeks, so "return to today" is the
    wrong shape -- a filing from January and one from last week aren't
    comparable. These windows are.
    """
    out = {}
    for h in HORIZONS:
        key = f"d{h}"
        vals = [t["horizons"][key] for t in items
                if t.get("horizons", {}).get(key) is not None]
        if vals:
            out[key] = {"avg": round(statistics.fmean(vals), 2), "n": len(vals),
                        "win": round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)}
    return out


def notional(row: dict) -> float:
    """Best available dollar figure for a trade.

    Form 4 reports an exact value; congressional filings only give a band, so
    the midpoint stands in. Any total built from these is an estimate and is
    labelled as one on the page.
    """
    v = row.get("value_usd")
    if v:
        return float(v)
    return range_mid(row.get("value_range") or "")


def _top(items: list[dict], key, n: int = 5, reverse: bool = True) -> list[dict]:
    vals = [i for i in items if key(i) is not None]
    return sorted(vals, key=key, reverse=reverse)[:n]


def recap(all_rows: list[dict], scored_by_uid: dict[str, dict],
          days: int, label: str) -> dict:
    """What happened in the last N days, by FILING date.

    Filing date, not trade date: this is a tracker, so "the last 24 hours"
    means what became public in the last 24 hours. With congressional trades
    disclosed 30-45 days late, grouping by trade date would show an empty
    yesterday and bury everything you actually just learned.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = [r for r in all_rows if (r.get("filed_date") or "") >= cutoff]
    scored = [scored_by_uid[r["uid"]] for r in rows if r["uid"] in scored_by_uid]

    people = defaultdict(float)
    tickers = defaultdict(float)
    sectors = defaultdict(float)
    for r in rows:
        amt = notional(r)
        people[r.get("person") or "?"] += amt
        if r.get("ticker"):
            tickers[r["ticker"]] += amt
    for t in scored:
        sectors[t.get("sector") or "Unknown"] += notional(t)

    def slim(t: dict) -> dict:
        return {k: t.get(k) for k in
                ("person", "ticker", "action", "company", "filed_date",
                 "trade_date", "source", "excess_pct", "signed_return_pct",
                 "value_usd", "value_range", "url")}

    biggest = _top(rows, notional, 6)
    return {
        "label": label,
        "days": days,
        "filings": len(rows),
        "scored": len(scored),
        "buys": sum(1 for r in rows if r.get("action") == "BUY"),
        "sells": sum(1 for r in rows if r.get("action") == "SELL"),
        "notional_est": round(sum(notional(r) for r in rows)),
        "people": len({r.get("person") for r in rows}),
        "performance": _agg(scored),
        "top_people": [{"name": k, "notional": round(v)}
                       for k, v in sorted(people.items(), key=lambda kv: -kv[1])[:6]],
        "top_tickers": [{"name": k, "notional": round(v)}
                        for k, v in sorted(tickers.items(), key=lambda kv: -kv[1])[:8]],
        "top_sectors": [{"name": k, "notional": round(v)}
                        for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])[:6]],
        "biggest": [slim(b) for b in biggest],
        "best": [slim(t) for t in _top(scored, lambda t: t.get("excess_pct"), 5)],
        "worst": [slim(t) for t in _top(scored, lambda t: t.get("excess_pct"), 5, False)],
    }


def group_by(scored: list[dict], key, min_trades: int = 3) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in scored:
        k = key(t)
        if k:
            buckets[k].append(t)
    out = [{"name": k, **_agg(v)} for k, v in buckets.items()
           if len(v) >= min_trades]
    return sorted(out, key=lambda d: (d["avg_excess"] is None, -(d["avg_excess"] or 0)))


def build(max_tickers: int, full: bool) -> dict:
    rows = store.load(TRADES_PATH)
    log.info("loaded %d stored trades", len(rows))
    universe = fetch_universe(UNIVERSE_PATH)
    px = PriceStore(PRICES_PATH)

    scored = score_trades(rows, px, universe, 0 if full else max_tickers)
    px.save()
    log.info("scored %d trades", len(scored))

    sources = group_by(scored, lambda t: SOURCE_LABEL.get(t["source"], t["source"]), 1)

    # Mark who is already tracked, so the page can separate "this is working"
    # from "you aren't watching this person yet".
    wl = Watchlist(load_watchlist())
    all_people = group_by(scored, lambda t: t["person"], 3)
    for row in all_people:
        row["watched"] = wl.match(row["name"]) is not None
    people = all_people[:40]

    # Candidates worth adding: enough trades to mean something, beating the
    # index, and not already on the list. MIN_SAMPLE is the honest constraint --
    # a 3-trade sample ranking first is luck, and promoting it would be the
    # same mistake as seeding the watchlist from last year's winners.
    MIN_SAMPLE = 10
    suggestions = [r for r in all_people
                   if not r["watched"] and r["trades"] >= MIN_SAMPLE
                   and (r["avg_excess"] or 0) > 0
                   and (r["median_excess"] or 0) > 0][:10]
    watched_perf = _agg([t for t in scored if wl.match(t["person"])])
    sectors = group_by(scored, lambda t: t["sector"], 3)
    industries = group_by(scored, lambda t: t["industry"], 3)[:25]
    caps = group_by(scored, lambda t: t["cap_tier"], 1)
    actions = group_by(scored, lambda t: t["action"], 1)

    congress = [t for t in scored if t["source"] in ("house", "senate")]
    insiders = [t for t in scored if t["source"] == "form4"]
    penny = [t for t in scored if t["cap_tier"].startswith("Penny")]
    smg = [t for t in scored if t.get("smg_eligible")]
    alerted = [t for t in scored if t.get("alerted")]

    bench_now = {}
    for b, label in BENCHMARKS.items():
        vals = [t["bench_pct"].get(b) for t in scored if t.get("bench_pct", {}).get(b)]
        bench_now[b] = {"label": label,
                        "avg_window_return": round(statistics.fmean(vals), 2) if vals else None}

    recent = sorted(scored, key=lambda t: t.get("filed_date") or "", reverse=True)[:400]

    by_uid = {t["uid"]: t for t in scored}
    recaps = [recap(rows, by_uid, 1, "Last 24 hours"),
              recap(rows, by_uid, 7, "Last 7 days"),
              recap(rows, by_uid, 30, "Last 30 days")]

    # Per-source actionable performance answers the question directly: is any
    # of this still worth acting on once it is public?
    smg_by_source = group_by(smg, lambda t: SOURCE_LABEL.get(t["source"], t["source"]), 1)

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "smg": {
            "min_price": SMG_MIN_PRICE,
            "min_mcap": SMG_MIN_MCAP,
            "horizons": HORIZONS,
            "by_source": smg_by_source,
            "eligible_trades": len(smg),
        },
        "totals": {
            "stored_trades": len(rows),
            "scored_trades": len(scored),
            "tickers": len({t["ticker"] for t in scored}),
            "people": len({t["person"] for t in scored}),
        },
        "headline": {
            "all": _agg(scored),
            "congress": _agg(congress),
            "insiders": _agg(insiders),
            "penny": _agg(penny),
            "smg": _agg(smg),
            "alerted": _agg(alerted),
        },
        "benchmarks": bench_now,
        "by_source": sources,
        "by_person": people,
        "suggestions": suggestions,
        "min_sample": MIN_SAMPLE,
        "watched_performance": watched_perf,
        "by_sector": sectors,
        "by_industry": industries,
        "by_cap": caps,
        "by_action": actions,
        "recaps": recaps,
        "trades": recent,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=120,
                    help="cap symbols priced per run; the cache warms over time")
    ap.add_argument("--full", action="store_true", help="price every ticker")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    payload = build(args.max_tickers, args.full)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "data.json").write_text(json.dumps(payload, separators=(",", ":")))
    log.info("wrote %s (%d KB)", SITE_DIR / "data.json",
             (SITE_DIR / "data.json").stat().st_size // 1024)
    t = payload["totals"]
    log.info("totals: %d stored, %d scored, %d tickers, %d people",
             t["stored_trades"], t["scored_trades"], t["tickers"], t["people"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
