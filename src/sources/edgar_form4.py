"""SEC Form 4 -- corporate insider trades. The fast source.

Officers, directors and 10%+ owners must file within 2 business days of a
trade, and the filing is public on EDGAR within minutes. This is the only feed
in the project with genuinely fresh data.

Two ways in, used together:
  * `getcurrent` Atom feed  -> low latency, but only a rolling window
  * daily index files       -> complete, used for backfill and the nightly sweep
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from ..models import Trade

log = logging.getLogger(__name__)

CURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               "&type=4&company=&dateb=&owner=include&count=100"
               "&output=atom&start={start}")
DAILY_IDX = ("https://www.sec.gov/Archives/edgar/daily-index/"
             "{year}/QTR{qtr}/form.{ymd}.idx")

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# A single filing appears in the feed once per reporting owner AND once for the
# issuer, so the raw entry count badly overstates the real filing count.
_ACC_RE = re.compile(r"accession-number=([\d-]+)")


def _acc_from_entry(entry) -> str | None:
    ident = entry.findtext("a:id", default="", namespaces=ATOM_NS)
    m = _ACC_RE.search(ident)
    return m.group(1) if m else None


def _link_from_entry(entry) -> str:
    link = entry.find("a:link", ATOM_NS)
    return link.get("href", "") if link is not None else ""


def iter_current_accessions(fetcher, state, max_pages: int = 20):
    """Walk the live feed newest-first, stopping once we reach known filings.

    Pagination matters: Form 4 volume is bursty right after the 4pm close and
    a single 100-entry page can easily overflow a 15-minute polling window.
    """
    found: dict[str, str] = {}
    for page in range(max_pages):
        url = CURRENT_URL.format(start=page * 100)
        xml = fetcher.get_text(url)
        if not xml:
            break
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log.error("bad atom on page %d: %s", page, e)
            break

        entries = root.findall("a:entry", ATOM_NS)
        if not entries:
            break

        seen_on_page = 0
        for entry in entries:
            acc = _acc_from_entry(entry)
            if not acc:
                continue
            if state.is_seen("form4", acc):
                seen_on_page += 1
                continue
            found.setdefault(acc, _link_from_entry(entry))

        # Most of this page is already processed -> we've caught up. One more
        # page of margin guards against the feed reordering under us.
        if seen_on_page > len(entries) * 0.8:
            log.info("caught up at page %d (%d known)", page, seen_on_page)
            break

    log.info("form4: %d new accessions from live feed", len(found))
    return found


def iter_accessions_for_date(fetcher, day: date) -> dict[str, str]:
    """All Form 4 accessions filed on a date, from the daily index."""
    qtr = (day.month - 1) // 3 + 1
    url = DAILY_IDX.format(year=day.year, qtr=qtr, ymd=day.strftime("%Y%m%d"))
    text = fetcher.get_text(url, allow_404=True)
    if not text:
        return {}

    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("4 "):        # form type column
            continue
        path = line.rsplit(" ", 1)[-1].strip()
        if not path.endswith(".txt"):
            continue
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")
        out[acc] = "https://www.sec.gov/Archives/" + path.lstrip("/")
    log.info("form4: %d filings in daily index for %s", len(out), day)
    return out


def _submission_url(index_link: str, accession: str) -> str:
    """Turn an -index.htm link into the complete-submission .txt.

    One request instead of two (index.json, then the XML), because the .txt
    carries the ownership XML inline.
    """
    if index_link:
        for suffix in ("-index.htm", "-index.html"):
            if index_link.endswith(suffix):
                return index_link[: -len(suffix)] + ".txt"
    return ""


def _f(el, path: str) -> str:
    """findtext that tolerates the optional <value> wrapper EDGAR uses."""
    if el is None:
        return ""
    v = el.findtext(path + "/value")
    if v is None:
        v = el.findtext(path)
    return (v or "").strip()


def _num(s: str):
    try:
        return float(s.replace(",", "").replace("$", ""))
    except (ValueError, AttributeError):
        return None


def _parse_date(s: str):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_form4(raw: str, accession: str, url: str) -> list[Trade]:
    """Extract non-derivative transactions from a complete submission file."""
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", raw, re.S)
    if not m:
        return []
    try:
        doc = ET.fromstring(m.group(0))
    except ET.ParseError as e:
        log.warning("unparseable XML in %s: %s", accession, e)
        return []

    issuer = doc.find("issuer")
    company = _f(issuer, "issuerName") or ""
    ticker = _f(issuer, "issuerTradingSymbol") or ""

    # A filing can carry several reporting owners (an exec plus their holding
    # company, say). Name the first and count the rest rather than emitting
    # duplicate alerts for one economic trade.
    owners, roles = [], []
    for ro in doc.findall("reportingOwner"):
        name = (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        if name:
            owners.append(name)
        rel = ro.find("reportingOwnerRelationship")
        if rel is not None:
            title = (rel.findtext("officerTitle") or "").strip()
            if title:
                roles.append(title)
            elif (rel.findtext("isDirector") or "").strip() in ("1", "true"):
                roles.append("Director")
            elif (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
                roles.append("10% Owner")

    person = owners[0] if owners else "(unknown)"
    if len(owners) > 1:
        person += f" (+{len(owners)-1})"
    role = roles[0] if roles else ""
    filed = _parse_date(doc.findtext("periodOfReport") or "")

    trades: list[Trade] = []
    for i, tx in enumerate(doc.findall(".//nonDerivativeTransaction")):
        coding = tx.find("transactionCoding")
        code = (coding.findtext("transactionCode") or "").strip() if coding is not None else ""
        amounts = tx.find("transactionAmounts")
        shares = _num(_f(amounts, "transactionShares"))
        price = _num(_f(amounts, "transactionPricePerShare"))
        ad = _f(amounts, "transactionAcquiredDisposedCode")

        value = shares * price if (shares is not None and price) else None
        action = "BUY" if ad == "A" else "SELL" if ad == "D" else "OTHER"

        trades.append(Trade(
            source="form4",
            uid=f"{accession}:{i}",
            person=person,
            role=role,
            action=action,
            company=company,
            ticker=ticker,
            shares=shares,
            price=price,
            value_usd=value,
            code=code,
            trade_date=_parse_date(_f(tx, "transactionDate")),
            filed_date=filed,
            url=url,
        ))
    return trades


def collect(fetcher, state, backfill_days: int = 0,
            limit: int = 0) -> list[Trade]:
    """Fetch and parse every new Form 4. Returns normalized trades.

    `limit` caps how many filings are fetched in one run. It exists as a
    safety valve: on a first run the state file is empty, so without it the
    bot would try to pull every filing the feed still remembers.
    """
    accessions = iter_current_accessions(fetcher, state)

    if backfill_days:
        today = date.today()
        for n in range(backfill_days):
            day = today - timedelta(days=n)
            if day.weekday() >= 5:          # SEC doesn't publish on weekends
                continue
            for acc, link in iter_accessions_for_date(fetcher, day).items():
                if not state.is_seen("form4", acc):
                    accessions.setdefault(acc, link)

    # Cold start: the live feed still remembers ~1,100 filings. Processing all
    # of them would fire a burst of alerts about trades that are already days
    # old and that the user never asked to be told about. Instead, record them
    # as seen and start clean -- an explicit --backfill is the way to ask for
    # history on purpose.
    if not state.count("form4") and not backfill_days:
        log.warning("form4: first run -- marking %d existing filings as seen "
                    "without alerting (use --backfill N to pull history)",
                    len(accessions))
        for acc in accessions:
            state.mark_seen("form4", acc)
        return []

    if limit and len(accessions) > limit:
        log.warning("form4: capping %d filings at %d for this run",
                    len(accessions), limit)
        accessions = dict(list(accessions.items())[:limit])

    all_trades: list[Trade] = []
    for acc, link in accessions.items():
        url = _submission_url(link, acc) or link
        if not url:
            continue
        raw = fetcher.get_text(url, allow_404=True)
        if not raw:
            continue        # retry next run rather than lose the filing
        all_trades.extend(parse_form4(raw, acc, link or url))
        state.mark_seen("form4", acc)

    log.info("form4: parsed %d transactions from %d filings",
             len(all_trades), len(accessions))
    return all_trades
