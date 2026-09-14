"""Orchestrator.

    python -m src.main --dry-run --backfill 7   # replay a week, send nothing
    python -m src.main                          # normal polling run

Exit code is 0 unless the run itself broke; finding no new trades is a normal,
successful outcome.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import (OUTBOX_PATH, STATE_PATH, TRADES_PATH,
                     load_settings, load_watchlist)
from .filters import Watchlist, evaluate, history_entries
from .http import Fetcher
from .models import Trade
from . import store
from .notify import discord, ntfy, outbox, twilio_sms
from .notify.format import sms_line
from .sources import edgar_13f, edgar_form4, edgar_form144, house, senate
from .state import State

log = logging.getLogger("tracker")

# Cluster detection reads recent buys back out of state, so this list has to
# outlive a single run. Trimmed to the window the filter actually looks at.
HISTORY_KEEP = 400


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Track insider and congressional trades.")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent; send nothing, save nothing")
    p.add_argument("--backfill", type=int, default=0, metavar="DAYS",
                   help="also sweep the SEC daily index for the last N days")
    p.add_argument("--source", action="append", default=None,
                   choices=["form4", "form144", "house", "senate", "13f"],
                   help="limit to one source (repeatable)")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="cap filings fetched this run (safety valve / testing)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def collect_trades(settings: dict, fetcher: Fetcher, state: State,
                   args) -> list[Trade]:
    enabled = settings.get("sources", {})
    wanted = set(args.source) if args.source else None
    trades: list[Trade] = []

    _CFG_KEY = {"form4": "edgar_form4", "13f": "edgar_13f",
                "form144": "edgar_form144"}

    def want(name: str) -> bool:
        if wanted is not None:
            return name in wanted
        return bool(enabled.get(_CFG_KEY.get(name, name)))

    if want("form4"):
        try:
            trades += edgar_form4.collect(fetcher, state, args.backfill,
                                          limit=args.limit)
        except Exception:
            # One broken source must not take down the others.
            log.exception("form4 source failed")

    if want("house"):
        try:
            trades += house.collect(fetcher, state, limit=args.limit)
        except Exception:
            log.exception("house source failed")

    if want("senate"):
        try:
            trades += senate.collect(fetcher, state, limit=args.limit)
        except Exception:
            log.exception("senate source failed")

    if want("form144"):
        try:
            trades += edgar_form144.collect(fetcher, state, limit=args.limit)
        except Exception:
            log.exception("form144 source failed")

    if want("13f"):
        try:
            trades += edgar_13f.collect(fetcher, state, load_watchlist())
        except Exception:
            log.exception("13f source failed")

    return trades


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    watchlist = Watchlist(load_watchlist())
    state = State(STATE_PATH)

    http_cfg = settings.get("http", {})
    fetcher = Fetcher(
        user_agent=os.environ.get("SEC_USER_AGENT") or http_cfg.get(
            "user_agent", "investor-tracker contact@example.com"),
        per_second=http_cfg.get("requests_per_second", 5),
        timeout=http_cfg.get("timeout_seconds", 30),
        max_retries=http_cfg.get("max_retries", 3),
    )

    trades = collect_trades(settings, fetcher, state, args)
    if not trades:
        log.info("no new filings this run")
        if not args.dry_run:
            state.save()
        return 0

    history = state.data.get("recent_buys", [])
    alerts = evaluate(trades, settings, watchlist, history)

    # The website analyses the full population, not the alerted subset, so the
    # comparisons ("congress vs insiders", "penny vs mega cap") aren't measured
    # against an already-filtered sample.
    if not args.dry_run:
        store.append(TRADES_PATH, trades, {a.uid for a in alerts})

    # Remember open-market buys regardless of whether they alerted -- a cluster
    # is built from individually unremarkable trades.
    state.data["recent_buys"] = (history + history_entries(trades))[-HISTORY_KEEP:]

    if not alerts:
        log.info("%d trades scanned, none met the alert criteria", len(trades))
        if not args.dry_run:
            state.save()
        return 0

    log.info("=== %d ALERT(S) ===", len(alerts))
    for t in alerts:
        log.info("  %s  [%s]", sms_line(t), "; ".join(t.reasons))

    n = settings.get("notify", {})

    if n.get("discord", {}).get("enabled"):
        discord.send(os.environ.get("DISCORD_WEBHOOK_URL", ""), alerts,
                     dry_run=args.dry_run)

    if n.get("ntfy", {}).get("enabled"):
        ntfy.send(os.environ.get("NTFY_TOPIC", ""), alerts, dry_run=args.dry_run)

    if n.get("mac_agent", {}).get("enabled") and not args.dry_run:
        outbox.append(OUTBOX_PATH, alerts)

    tw_cfg = n.get("twilio", {})
    if tw_cfg.get("enabled"):
        twilio_sms.send(tw_cfg, os.environ.get("TWILIO_ACCOUNT_SID", ""),
                        os.environ.get("TWILIO_AUTH_TOKEN", ""), alerts,
                        dry_run=args.dry_run)

    if args.dry_run:
        log.info("dry run: nothing sent, state not written")
    else:
        state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
