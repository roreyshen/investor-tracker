"""One-shot channel test. Run this BEFORE trusting any channel.

    python -m src.notify.test --channel all

The SMS check exists because Xfinity Mobile's gateway may simply not accept
these messages. SMTP will report success either way -- handing mail to Gmail is
not the same as a carrier delivering it -- so the real verification is your
phone and your inbox, which is what this prints instructions for.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.config import load_settings                    # noqa: E402
from src.models import Trade                            # noqa: E402
from src.notify import discord, sms_gateway, twilio_sms  # noqa: E402
from src.notify.format import sms_line                  # noqa: E402

SAMPLE = Trade(
    source="form4", uid="test", person="TEST ALERT", role="Chief Executive Officer",
    action="BUY", ticker="TEST", company="Investor Tracker self-test",
    shares=100_000, price=42.0, value_usd=4_200_000, code="P",
    trade_date=date.today(), filed_date=date.today(),
    url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4",
    reasons=["this is a test message, not a real trade"],
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="all",
                    choices=["all", "discord", "sms", "twilio"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()
    n = settings.get("notify", {})
    results: dict[str, str] = {}

    if args.channel in ("all", "discord"):
        url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not url:
            results["discord"] = "SKIP - DISCORD_WEBHOOK_URL not set"
        else:
            ok = discord.send(url, [SAMPLE])
            results["discord"] = "SENT - check your Discord channel" if ok else "FAILED"

    if args.channel in ("all", "sms"):
        sent, _ = sms_gateway.send(
            n.get("sms_gateway", {}),
            os.environ.get("GMAIL_ADDRESS", ""),
            os.environ.get("GMAIL_APP_PASSWORD", ""),
            [SAMPLE], budget=1,
        )
        results["sms"] = (
            "HANDED OFF - delivery NOT confirmed" if sent else
            "SKIP/FAILED - see log above"
        )

    if args.channel in ("all", "twilio"):
        cfg = n.get("twilio", {})
        if not cfg.get("enabled"):
            results["twilio"] = "SKIP - disabled in settings.yml (expected)"
        else:
            sent = twilio_sms.send(cfg, os.environ.get("TWILIO_ACCOUNT_SID", ""),
                                   os.environ.get("TWILIO_AUTH_TOKEN", ""), [SAMPLE])
            results["twilio"] = "SENT" if sent else "FAILED"

    print("\n" + "=" * 62)
    print("  CHANNEL TEST RESULTS")
    print("=" * 62)
    for ch, res in results.items():
        print(f"  {ch:9} {res}")
    print("=" * 62)
    print(f"\n  Message body ({len(sms_line(SAMPLE))} chars):")
    print(f"  {sms_line(SAMPLE)}\n")

    if "sms" in results and results["sms"].startswith("HANDED OFF"):
        print("  >> SMS needs YOU to verify, SMTP cannot:")
        print("     1. Did a text arrive at 408-748-6507 within ~2 minutes?")
        print("     2. Check roreyshen@gmail.com for a bounce/undeliverable.")
        print("     If no text AND a bounce -> Xfinity blocks the gateway.")
        print("     Discord still works; see README for the Twilio upgrade.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
