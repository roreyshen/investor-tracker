"""Free carrier email-to-SMS. Best effort, and deliberately budgeted.

Every carrier runs a gateway that turns an email into a text. It costs nothing
and needs no registration, which is the appeal. The catch, for this number
specifically:

  * 408-748-6507 is on Xfinity Mobile, a Verizon MVNO. Xfinity users widely
    report @vtext.com and @mypixmessages.com bouncing as undeliverable.
  * Verizon has announced a hard sunset of its gateway on 2027-03-31.

So this is tried, measured, and never depended on. Discord carries the load.
Run `python -m src.notify.test --channel sms` to find out whether it actually
works for this number, then check the Gmail inbox for a bounce.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..models import Trade
from .format import sms_line

log = logging.getLogger(__name__)

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587


def send(cfg: dict, gmail_user: str, gmail_pass: str, trades: list[Trade],
         budget: int, dry_run: bool = False) -> tuple[int, int]:
    """Returns (sent, skipped). `budget` is the remaining daily allowance."""
    if not trades:
        return 0, 0
    if not (gmail_user and gmail_pass):
        log.warning("sms: GMAIL_ADDRESS/GMAIL_APP_PASSWORD unset, skipping")
        return 0, len(trades)

    number = str(cfg.get("number", "")).strip()
    gateways = cfg.get("gateways") or ["vtext.com"]
    if not number:
        log.warning("sms: no number configured")
        return 0, len(trades)

    per_run = int(cfg.get("max_per_run", 5))
    allowed = max(0, min(per_run, budget))
    to_send, skipped = trades[:allowed], trades[allowed:]

    if skipped:
        # Bursts get truncated rather than dropped silently -- the tail is
        # always in Discord, and hammering a carrier gateway is how it gets
        # blocked outright.
        log.info("sms: capped at %d, %d more in Discord", allowed, len(skipped))

    if dry_run:
        for t in to_send:
            log.info("[dry-run] SMS -> %s", sms_line(t))
        return len(to_send), len(skipped)
    if not to_send:
        return 0, len(skipped)

    sent = 0
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(gmail_user, gmail_pass)
            for t in to_send:
                body = sms_line(t)
                for gw in gateways:
                    msg = EmailMessage()
                    msg["From"] = gmail_user
                    msg["To"] = f"{number}@{gw}"
                    # Carrier gateways prepend the subject; an empty one keeps
                    # the 160-char budget for content that matters.
                    msg["Subject"] = ""
                    msg.set_content(body)
                    try:
                        s.send_message(msg)
                    except smtplib.SMTPException as e:
                        log.warning("sms: %s rejected: %s", gw, e)
                sent += 1
    except (smtplib.SMTPException, OSError) as e:
        log.error("sms: SMTP failed: %s", e)
        return sent, len(trades) - sent

    log.info("sms: handed %d message(s) to %d gateway(s) -- delivery unconfirmed",
             sent, len(gateways))
    return sent, len(skipped)
