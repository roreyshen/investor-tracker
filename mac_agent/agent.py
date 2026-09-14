#!/usr/bin/env python3
"""Mac-side texter. Reads the outbox from the repo and sends real messages.

Why this exists: 408-748-6507 is on Xfinity Mobile, whose email-to-SMS gateway
is unreliable, and Twilio costs money. But a Mac signed into iMessage can drive
Messages.app directly through AppleScript, which sends an actual message to
your phone for free.

Split of responsibility:
  * GitHub Actions runs 24/7, detects trades, writes state/outbox.json.
  * This agent runs on the Mac, pulls, and sends whatever is unsent.

The Mac being asleep is expected, not a failure -- work queues in the repo and
goes out on the next wake. Sent state is tracked in the HOME directory, never
committed, so this agent never pushes and never races the scheduled runs.

    python3 mac_agent/agent.py            # send pending
    python3 mac_agent/agent.py --test     # send one test message
    python3 mac_agent/agent.py --dry-run  # show what would send
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTBOX = REPO / "state" / "outbox.json"
SENT_DB = Path.home() / ".investor-tracker-sent.json"
CONFIG = Path.home() / ".investor-tracker-agent.json"

DEFAULT_RECIPIENT = "+14087486507"
# Nothing is urgent enough to wake you. Alerts inside this window stay queued
# and go out as a digest afterwards -- congressional filings are already 30+
# days old, and even Form 4 is two days old, so an overnight hold costs nothing.
QUIET_START, QUIET_END = 23, 7      # 11pm - 7am local
# Alerts older than this are summarized instead of sent one by one. Coming back
# from a long sleep should not mean 40 buzzes.
STALE_AFTER = timedelta(hours=12)
MAX_INDIVIDUAL = 8
# A hung osascript means macOS is showing the Automation permission dialog.
OSA_TIMEOUT = 25


def in_quiet_hours(now: datetime | None = None) -> bool:
    cfg = load_json(CONFIG, {})
    start = cfg.get("quiet_start", QUIET_START)
    end = cfg.get("quiet_end", QUIET_END)
    if start == end:
        return False
    h = (now or datetime.now()).hour
    # The window wraps midnight, so it's "after start OR before end".
    return h >= start or h < end if start > end else start <= h < end


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return default


def recipient() -> str:
    return load_json(CONFIG, {}).get("recipient") or os.environ.get(
        "TRACKER_SMS_TO", DEFAULT_RECIPIENT)


def send_message(text: str, to: str) -> tuple[bool, str]:
    """Send via Messages.app. Returns (ok, detail).

    Tries iMessage first, then falls back to the SMS service, which is what
    carries the message when the recipient isn't on iMessage (this requires
    Text Message Forwarding enabled on the paired iPhone).
    """
    script = f'''
    on run
        set msg to {json.dumps(text)}
        set dest to {json.dumps(to)}
        tell application "Messages"
            try
                set svc to 1st service whose service type = iMessage
                send msg to participant dest of svc
                return "imessage"
            on error errMsg
                try
                    set svc to 1st service whose service type = SMS
                    send msg to participant dest of svc
                    return "sms"
                on error errMsg2
                    return "ERROR: " & errMsg & " / " & errMsg2
                end try
            end try
        end tell
    end run
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=OSA_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, ("timed out -- macOS is probably showing the Automation "
                       "permission dialog. Approve it once, then rerun.")
    out = (r.stdout or "").strip()
    err = (r.returncode and (r.stderr or "").strip()) or ""
    if out.startswith("ERROR") or err:
        return False, out or err
    return True, out or "sent"


def pending_alerts() -> list[dict]:
    entries = load_json(OUTBOX, {}).get("pending", [])
    sent = set(load_json(SENT_DB, {}).get("sent", []))
    return [e for e in entries if e.get("uid") not in sent]


def mark_sent(uids: list[str]) -> None:
    db = load_json(SENT_DB, {"sent": []})
    db["sent"] = (db.get("sent", []) + uids)[-5000:]
    SENT_DB.write_text(json.dumps(db))


def git_pull() -> None:
    try:
        r = subprocess.run(["git", "-C", str(REPO), "pull", "--rebase", "--autostash"],
                           capture_output=True, text=True, timeout=90)
        if r.returncode:
            log(f"git pull failed (continuing with local state): {r.stderr.strip()[:120]}")
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"git pull skipped: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send one test message")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pull", action="store_true")
    args = ap.parse_args()
    to = recipient()

    if args.test:
        text = ("Investor Tracker test - if you got this, the Mac texter works. "
                f"({datetime.now():%-I:%M %p})")
        if args.dry_run:
            log(f"[dry-run] would text {to}: {text}")
            return 0
        ok, detail = send_message(text, to)
        log(f"test -> {to}: {'OK via ' + detail if ok else 'FAILED: ' + detail}")
        return 0 if ok else 1

    if not args.no_pull:
        git_pull()

    alerts = pending_alerts()
    if not alerts:
        log("nothing pending")
        return 0

    if in_quiet_hours() and not args.dry_run:
        log(f"quiet hours -- holding {len(alerts)} alert(s) until morning")
        return 0

    now = datetime.now(timezone.utc)
    fresh, stale = [], []
    for a in alerts:
        try:
            queued = datetime.fromisoformat(a["queued_at"])
        except (ValueError, KeyError):
            queued = now
        (stale if now - queued > STALE_AFTER else fresh).append(a)

    sent_uids: list[str] = []

    # Anything that waited out a long sleep goes as one digest, not a barrage.
    if stale:
        text = (f"{len(stale)} older alert(s) while your Mac was asleep - "
                "full detail in Discord.")
        if args.dry_run:
            log(f"[dry-run] digest: {text}")
        else:
            ok, detail = send_message(text, to)
            log(f"digest ({len(stale)}) -> {'OK' if ok else 'FAILED: ' + detail}")
            if not ok:
                return 1
        sent_uids += [a["uid"] for a in stale]

    overflow = fresh[MAX_INDIVIDUAL:]
    for a in fresh[:MAX_INDIVIDUAL]:
        if args.dry_run:
            log(f"[dry-run] would text: {a['text']}")
            sent_uids.append(a["uid"])
            continue
        ok, detail = send_message(a["text"], to)
        log(f"{'sent' if ok else 'FAILED'}: {a['text'][:70]}"
            + ("" if ok else f" -- {detail}"))
        if not ok:
            # Stop on failure so the queue is retried rather than lost.
            mark_sent(sent_uids)
            return 1
        sent_uids.append(a["uid"])

    if overflow:
        text = f"+{len(overflow)} more alert(s) - see Discord."
        if not args.dry_run:
            ok, _ = send_message(text, to)
            if ok:
                sent_uids += [a["uid"] for a in overflow]
        else:
            log(f"[dry-run] overflow: {text}")
            sent_uids += [a["uid"] for a in overflow]

    if not args.dry_run:
        mark_sent(sent_uids)
    log(f"done, {len(sent_uids)} alert(s) handled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
