"""Discord webhook -- the primary channel.

Free, instant, reliable, and unlike SMS it can carry a clickable link straight
to the filing. Everything else in this package is best-effort on top of this.
"""
from __future__ import annotations

import logging
import time

import requests

from ..models import Trade
from .format import discord_embed

log = logging.getLogger(__name__)

MAX_EMBEDS = 10          # Discord's hard per-message limit


def send(webhook_url: str, trades: list[Trade], dry_run: bool = False) -> bool:
    if not webhook_url:
        log.warning("discord: no webhook configured, skipping")
        return False
    if not trades:
        return True

    ok = True
    for i in range(0, len(trades), MAX_EMBEDS):
        chunk = trades[i:i + MAX_EMBEDS]
        payload = {
            "username": "Investor Tracker",
            "embeds": [discord_embed(t) for t in chunk],
        }
        if dry_run:
            log.info("[dry-run] would post %d embeds to Discord", len(chunk))
            continue
        ok &= _post(webhook_url, payload)
    return ok


def send_text(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        return False
    return _post(webhook_url, {"username": "Investor Tracker", "content": text[:1900]})


def _post(url: str, payload: dict, attempts: int = 3) -> bool:
    for n in range(attempts):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:
                # Discord tells us exactly how long to wait; respect it or the
                # webhook gets temporarily disabled.
                wait = r.json().get("retry_after", 5) if r.content else 5
                log.warning("discord rate limited, waiting %ss", wait)
                time.sleep(float(wait) + 0.5)
                continue
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("discord post failed (attempt %d): %s", n + 1, e)
            time.sleep(2 ** n)
    return False
