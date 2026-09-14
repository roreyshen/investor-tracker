"""Durable record of every trade seen, alerted or not.

The alerting path is deliberately noisy-averse, but the website needs the
opposite: the full population, so "congress vs insiders" or "penny stocks vs
mega caps" is measured against everything rather than a filtered slice.

Append-only and deduped by uid, stored as JSON and committed with the repo.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Trade

log = logging.getLogger(__name__)

# ~2 years of congressional plus filtered insider flow. Far more than the site
# charts, and keeps the committed file to a sane size.
# A decade of insider history is the point: two years is one market regime,
# and concluding "no edge" from a single bull market is not a conclusion.
# The file is gitignored derived data, so size is a local concern only.
MAX_TRADES = 600_000
RETAIN_DAYS = 3800

# Form 4 codes worth keeping: P = open-market purchase, S = open-market sale.
# Everything else (A grant, F tax withholding, M option exercise, G gift) is
# compensation mechanics -- nobody chose to trade -- and it is ~90% of the
# volume. Storing it would bloat the committed file and drag every average
# toward noise.
KEEP_FORM4_CODES = {"P", "S"}


def _row(t: Trade, alerted: bool) -> dict:
    d = t.to_dict()
    d.pop("reasons", None)
    d["alerted"] = alerted
    d["reasons"] = t.reasons
    return d


def append(path: Path, trades: list[Trade], alert_uids: set[str]) -> int:
    if not trades:
        return 0
    path = Path(path)
    rows: list[dict] = []
    if path.exists():
        try:
            rows = json.loads(path.read_text()).get("trades", [])
        except (json.JSONDecodeError, OSError) as e:
            log.warning("trade store unreadable (%s), starting fresh", e)

    known = {r.get("uid") for r in rows}
    added = 0
    for t in trades:
        if t.uid in known:
            continue
        if t.source == "form4" and t.code and t.code not in KEEP_FORM4_CODES:
            continue
        rows.append(_row(t, t.uid in alert_uids))
        known.add(t.uid)
        added += 1

    cutoff = (date.today() - timedelta(days=RETAIN_DAYS)).isoformat()
    rows = [r for r in rows if (r.get("filed_date") or r.get("trade_date") or "9999") >= cutoff]
    rows = rows[-MAX_TRADES:]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": datetime.now().isoformat(),
                               "trades": rows}, separators=(",", ":")))
    tmp.replace(path)
    log.info("store: +%d trades (%d total)", added, len(rows))
    return added


def load(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text()).get("trades", [])
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return []
