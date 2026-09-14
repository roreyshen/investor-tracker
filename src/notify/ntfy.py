"""ntfy.sh push — reaches your phone with the Mac closed.

The Mac agent can only send while the Mac is awake and online. This runs
inside GitHub Actions instead, so delivery does not depend on any machine of
yours being on. It is a push notification rather than an SMS, but it arrives
the same way: the phone buzzes, with a title and a tappable link.

Free, no account, no registration.

Topic security: an ntfy topic is a shared secret -- anyone who knows the
string can read and publish to it. This repo is public, so the topic lives in
the NTFY_TOPIC GitHub secret and never in config.
"""
from __future__ import annotations

import logging
import time

import requests

from ..models import Trade
from .format import sms_line

log = logging.getLogger(__name__)

BASE = "https://ntfy.sh"
# Bursts get collapsed so a heavy filing day is one buzz plus a summary,
# matching how the Mac agent behaves.
MAX_INDIVIDUAL = 6


def _ascii_header(value: str) -> str:
    """HTTP headers are latin-1 only.

    The message BODY is sent as UTF-8 and can hold anything, but a title with
    an em-dash or emoji raises UnicodeEncodeError inside http.client and the
    send fails outright. Common punctuation is transliterated rather than
    dropped so titles stay readable.
    """
    swaps = {"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
             "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00b7": "-"}
    for bad, good in swaps.items():
        value = value.replace(bad, good)
    return value.encode("latin-1", "ignore").decode("latin-1")


def _post(topic: str, text: str, title: str, url: str = "",
          priority: str = "default", attempts: int = 3) -> bool:
    headers = {"Title": _ascii_header(title)[:200], "Priority": priority,
               "Tags": "chart_with_upwards_trend"}
    if url:
        # Renders as a tappable button straight to the filing.
        headers["Actions"] = _ascii_header(f"view, Open filing, {url}")
    for n in range(attempts):
        try:
            r = requests.post(f"{BASE}/{topic}", data=text.encode("utf-8"),
                              headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** n * 3)
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.warning("ntfy attempt %d failed: %s", n + 1, e)
            time.sleep(2 ** n)
    return False


def send_text(topic: str, text: str, title: str = "Investor Tracker",
              priority: str = "default") -> bool:
    """Send one arbitrary message (digests, summaries, status)."""
    if not topic:
        log.info("ntfy: no NTFY_TOPIC set, skipping")
        return False
    return _post(topic, text, title, priority=priority)


def send(topic: str, trades: list[Trade], dry_run: bool = False) -> int:
    if not topic:
        log.info("ntfy: no NTFY_TOPIC set, skipping")
        return 0
    if not trades:
        return 0

    head, tail = trades[:MAX_INDIVIDUAL], trades[MAX_INDIVIDUAL:]
    sent = 0
    for t in head:
        title = f"{t.action} {t.ticker or t.company[:20]}"
        if dry_run:
            log.info("[dry-run] ntfy -> %s", sms_line(t))
            sent += 1
            continue
        # Watchlist hits are the ones actually asked for, so they buzz louder.
        pri = "high" if any("watchlist" in r for r in t.reasons) else "default"
        if _post(topic, sms_line(t), title, t.url, pri):
            sent += 1

    if tail:
        msg = f"+{len(tail)} more alert(s) — see the dashboard or Discord."
        if dry_run:
            # Counted like any other message: a dry run has to report the same
            # number a real run would send, or it isn't a preview.
            log.info("[dry-run] ntfy overflow: %s", msg)
            sent += 1
        elif _post(topic, msg, "More alerts"):
            sent += 1

    log.info("ntfy: sent %d notification(s)", sent)
    return sent
