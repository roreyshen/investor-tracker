"""Twilio SMS -- paid, guaranteed delivery. Off by default.

Written and ready so switching on later is a config change, not a rewrite.
To enable, see the "Upgrading to real SMS" section of the README: register an
A2P 10DLC sole-proprietor brand ($4 one-time + $15 campaign vetting, ~$3-4/mo),
add the three secrets, and set notify.twilio.enabled = true.

Uses the REST API directly -- no `twilio` package needed for one endpoint.
"""
from __future__ import annotations

import logging

import requests

from ..models import Trade
from .format import sms_line

log = logging.getLogger(__name__)

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def send(cfg: dict, sid: str, token: str, trades: list[Trade],
         dry_run: bool = False) -> int:
    if not trades:
        return 0
    if not (sid and token):
        log.warning("twilio: enabled but TWILIO_ACCOUNT_SID/AUTH_TOKEN unset")
        return 0

    from_number = cfg.get("from_number", "")
    to_number = cfg.get("to_number", "")
    if not (from_number and to_number):
        log.warning("twilio: from_number/to_number not configured")
        return 0

    sent = 0
    for t in trades[: int(cfg.get("max_per_run", 10))]:
        body = sms_line(t)
        if dry_run:
            log.info("[dry-run] Twilio -> %s", body)
            sent += 1
            continue
        try:
            r = requests.post(
                API.format(sid=sid),
                auth=(sid, token),
                data={"From": from_number, "To": to_number, "Body": body},
                timeout=20,
            )
            r.raise_for_status()
            sent += 1
        except requests.RequestException as e:
            detail = ""
            if getattr(e, "response", None) is not None:
                detail = f" -- {e.response.text[:200]}"
            log.error("twilio send failed: %s%s", e, detail)
    return sent
