"""13F-HR institutional holdings -- Buffett, Burry, Ackman and friends.

Be clear-eyed about what this is: 13F is a quarterly SNAPSHOT filed up to 45
days after the quarter closes, so a "new Berkshire position" can be four and a
half months old and already priced in. It is context, not a signal, and every
alert says so.

It also only shows long US equity positions at quarter end. Shorts, bonds,
cash and anything opened and closed inside the quarter are invisible.

Only funds listed in watchlist.yml with a CIK are tracked -- there are
thousands of 13F filers and almost none of them are interesting.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

from ..models import Trade

log = logging.getLogger(__name__)

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

# Position changes below this are portfolio drift, not a decision.
MATERIAL_CHANGE = 0.20


def _strip_ns(xml: str) -> str:
    """Flatten namespaces so one set of tag names works across filers.

    Order matters. Some filers carry `xsi:schemaLocation` on the root; once
    the matching `xmlns:xsi` declaration is removed, that attribute is an
    unbound prefix and the parse fails outright, so prefixed attributes have
    to go too.
    """
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)   # namespace declarations
    xml = re.sub(r'\s\w+:\w+="[^"]*"', "", xml)        # prefixed attributes
    return re.sub(r"<(/?)\w+:", r"<\1", xml)          # prefixed tags


def parse_holdings(raw: str) -> dict[str, dict]:
    """Aggregate an information table by CUSIP.

    A single issuer appears once per managing entity, so the rows must be
    summed or every diff would be nonsense.
    """
    m = re.search(r"<informationTable[^>]*>.*?</informationTable>", raw, re.S)
    if not m:
        return {}
    try:
        root = ET.fromstring(_strip_ns(m.group(0)))
    except ET.ParseError as e:
        log.warning("13f: unparseable information table: %s", e)
        return {}

    agg: dict[str, dict] = defaultdict(
        lambda: {"name": "", "title": "", "value": 0.0, "shares": 0.0})
    for row in root.findall("infoTable"):
        cusip = (row.findtext("cusip") or "").strip().upper()
        if not cusip:
            continue
        try:
            # sshPrnamt is nested under <shrsOrPrnAmt>, not a direct child --
            # reading it as one silently yields 0 shares and kills every diff.
            shares_txt = (row.findtext("shrsOrPrnAmt/sshPrnamt")
                          or row.findtext(".//sshPrnamt") or "0")
            value = float((row.findtext("value") or "0").replace(",", ""))
            shares = float(shares_txt.replace(",", ""))
        except ValueError:
            continue
        e = agg[cusip]
        e["name"] = e["name"] or (row.findtext("nameOfIssuer") or "").strip()
        # Share classes are separate CUSIPs under one issuer name (Alphabet A
        # and C, Lennar A and B), so keep the class or the alerts read as
        # duplicates of each other.
        e["title"] = e["title"] or (row.findtext("titleOfClass") or "").strip()
        e["value"] += value
        e["shares"] += shares
    return dict(agg)


def latest_13f(fetcher, cik: str):
    """Most recent 13F-HR for a CIK: (accession, filed, period) or None."""
    data = fetcher.get_json(SUBMISSIONS.format(cik=cik))
    if not data:
        return None
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form == "13F-HR":
            return (recent["accessionNumber"][i], recent["filingDate"][i],
                    recent["reportDate"][i])
    return None


def _d(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _diff(fund: str, prev: dict, cur: dict, url: str, filed, period: str,
          acc: str) -> list[Trade]:
    trades: list[Trade] = []

    def mk(action, cusip, info, extra=""):
        label = info.get("name", "")
        title = (info.get("title") or "").strip()
        if title and title.upper() not in ("COM", "COMMON", "COMMON STOCK"):
            label = f"{label} ({title})"
        return Trade(
            source="13f", uid=f"{acc}:{cusip}", person=fund, role="13F filer",
            action=action, company=label[:80],
            value_usd=info.get("value"), shares=info.get("shares"),
            filed_date=filed, trade_date=_d(period), url=url,
            # Kept terse: SMS has ~160 characters total. The "this is a
            # quarterly snapshot, not a live trade" caveat is spelled out in
            # the Discord embed, which has room for it.
            note=extra or f"13F {period}",
        )

    for cusip, info in cur.items():
        if cusip not in prev:
            trades.append(mk("BUY", cusip, info, "NEW position"))
            continue
        old = prev[cusip].get("shares", 0) or 0
        new = info.get("shares", 0) or 0
        if old <= 0:
            continue
        change = (new - old) / old
        if abs(change) >= MATERIAL_CHANGE:
            trades.append(mk("BUY" if change > 0 else "SELL", cusip, info,
                             f"position {change:+.0%}"))

    for cusip, info in prev.items():
        if cusip not in cur:
            # Report what the position was worth last quarter. The current
            # value is zero by definition, which tells the reader nothing.
            trades.append(mk("SELL", cusip, info, "EXITED - was this size"))
    return trades


def collect(fetcher, state, watchlist_cfg: dict) -> list[Trade]:
    funds = [f for f in (watchlist_cfg.get("funds") or [])
             if isinstance(f, dict) and f.get("cik")]
    if not funds:
        log.info("13f: no funds with a CIK in the watchlist, skipping")
        return []

    store = state.data.setdefault("holdings_13f", {})
    trades: list[Trade] = []

    for fund in funds:
        cik = str(fund["cik"]).lstrip("0")
        name = fund.get("name", f"CIK {cik}")
        latest = latest_13f(fetcher, cik)
        if not latest:
            continue
        acc, filed, period = latest
        if state.is_seen("13f", acc):
            continue

        raw = fetcher.get_text(
            ARCHIVE.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc),
            allow_404=True)
        if not raw:
            # Deliberately NOT marked seen: a transient failure here would
            # otherwise skip this filing forever and the fund would never get
            # a baseline, silently disabling it until the next quarter.
            continue

        holdings = parse_holdings(raw)
        if not holdings:
            log.warning("13f: no holdings parsed for %s (%s)", name, acc)
            continue
        state.mark_seen("13f", acc)

        prev = store.get(cik, {}).get("holdings")
        url = INDEX_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc)

        if prev:
            found = _diff(name, prev, holdings, url, _d(filed), period, acc)
            trades.extend(found)
            log.info("13f: %s -> %d position changes", name, len(found))
        else:
            # First sight of this fund: store a baseline. Announcing every
            # existing holding as "new" would be dozens of false alerts.
            log.info("13f: baseline for %s (%d positions), no alerts",
                     name, len(holdings))

        store[cik] = {"period": period, "accession": acc, "holdings": holdings}

    return trades
