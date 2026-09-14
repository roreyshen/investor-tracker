#!/usr/bin/env python3
"""Check real positions against their stops and targets, and alert on breaks.

SMG has no API, so positions live in config/positions.yml and you keep that
file current. The value here is not the arithmetic -- it is that nobody
reliably checks ten stop levels every afternoon for thirteen weeks.

Runs after the close so it compares against a settled price, not an intraday
wobble that would have alerted and then recovered.

    python scripts/stop_watch.py
    python scripts/stop_watch.py --send
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (CONFIG_DIR, FUNDAMENTALS_PATH, OUTBOX_PATH,  # noqa: E402
                        PRICES_PATH, load_settings)
from src.fundamentals import Fundamentals  # noqa: E402
from src.models import Trade  # noqa: E402
from src.notify import discord, ntfy, outbox  # noqa: E402
from src.prices import PriceStore  # noqa: E402

log = logging.getLogger("stops")
POSITIONS = CONFIG_DIR / "positions.yml"


def load_positions() -> list[dict]:
    if not POSITIONS.exists():
        return []
    try:
        return (yaml.safe_load(POSITIONS.read_text()) or {}).get("positions") or []
    except yaml.YAMLError as e:
        log.error("positions.yml is not valid YAML: %s", e)
        return []


def check() -> tuple[list[dict], list[str]]:
    px = PriceStore(PRICES_PATH)
    fund = Fundamentals(FUNDAMENTALS_PATH)
    today = date.today()
    rows, alerts = [], []

    for pos in load_positions():
        tk = str(pos.get("ticker", "")).upper().strip()
        if not tk:
            continue
        shares = float(pos.get("shares") or 0)
        entry = float(pos.get("entry") or 0)
        px.history(tk, today - timedelta(days=120))
        last = px.close_on_or_before(tk, today, 6)
        if not last:
            log.warning("%s: no price available", tk)
            continue

        pnl = (last - entry) * shares if entry and shares else None
        pct = (last / entry - 1) * 100 if entry else None
        stop, target = pos.get("stop"), pos.get("target")
        state = "ok"
        if stop and last <= float(stop):
            state = "STOP HIT"
            alerts.append(f"STOP HIT {tk} ${last:.2f} <= ${float(stop):.2f} "
                          f"({pct:+.1f}%) — sell at the close")
        elif target and last >= float(target):
            state = "TARGET HIT"
            alerts.append(f"TARGET {tk} ${last:.2f} >= ${float(target):.2f} "
                          f"({pct:+.1f}%) — consider taking profit")
        elif stop and last <= float(stop) * 1.03:
            state = "near stop"
            alerts.append(f"NEAR STOP {tk} ${last:.2f}, stop ${float(stop):.2f} "
                          f"({pct:+.1f}%)")

        soon = fund.earnings_within(tk, 10)
        if soon:
            alerts.append(f"EARNINGS SOON {tk} ({soon}) — single-day risk")

        rows.append({"ticker": tk, "last": last, "entry": entry, "shares": shares,
                     "pnl": pnl, "pct": pct, "stop": stop, "target": target,
                     "state": state})
    fund.save()
    px.save()
    return rows, alerts


def render(rows, alerts) -> str:
    if not rows:
        return ("No positions tracked. Add what you bought to "
                "config/positions.yml so stops get watched for you.")
    total = sum(r["pnl"] or 0 for r in rows)
    out = [f"POSITIONS — {len(rows)} open, P/L ${total:+,.0f}", ""]
    for r in rows:
        flag = "" if r["state"] == "ok" else f"  <<< {r['state']}"
        out.append(f"{r['ticker']:6} ${r['last']:8.2f}  {r['pct']:+6.1f}%  "
                   f"${r['pnl']:+8,.0f}{flag}")
    if alerts:
        out += ["", "ACTION:"] + [f"  {a}" for a in alerts]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    rows, alerts = check()
    text = render(rows, alerts)
    print("\n" + text + "\n")

    # Only push when something needs doing. A daily "nothing happened" text
    # trains you to ignore the channel, which costs you the one that matters.
    if not args.send or not alerts:
        return 0

    n = load_settings().get("notify", {})
    if n.get("discord", {}).get("enabled"):
        discord.send_text(os.environ.get("DISCORD_WEBHOOK_URL", ""), text)
    if n.get("ntfy", {}).get("enabled"):
        ntfy.send_text(os.environ.get("NTFY_TOPIC", ""), text,
                       "SMG: position action needed", priority="high")
    if n.get("mac_agent", {}).get("enabled"):
        outbox.append(OUTBOX_PATH, [Trade(
            source="stops", uid=f"stops:{date.today()}:{len(alerts)}",
            person="Stop watch", action="OTHER",
            company="; ".join(alerts)[:90], filed_date=date.today())])
    log.info("sent %d alert(s)", len(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
