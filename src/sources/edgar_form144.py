"""SEC Form 144 -- notice of a PROPOSED insider sale.

The earliest legal warning of large insider selling. An affiliate who intends
to sell restricted or control stock files this at or before placing the order,
so it lands *ahead* of the sale -- unlike Form 4, which reports a sale up to
two business days after the fact. Electronic filing has been mandatory since
2023, so it is all on EDGAR.

What it is not: a guarantee. A 144 states an intent to sell, and the sale can
be smaller than noticed or not happen at all. Alerts say "intends to sell" for
that reason.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from ..models import Trade

log = logging.getLogger(__name__)

CURRENT_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
               "&type=144&company=&dateb=&owner=include&count=100"
               "&output=atom&start={start}")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_ACC_RE = re.compile(r"accession-number=([\d-]+)")

_ticker_cache: dict[str, str] = {}


def ticker_map(fetcher) -> dict[str, str]:
    """CIK -> ticker. Form 144 names the issuer but never its symbol."""
    global _ticker_cache
    if _ticker_cache:
        return _ticker_cache
    data = fetcher.get_json(TICKER_MAP_URL)
    if not data:
        return {}
    try:
        _ticker_cache = {str(v["cik_str"]): v["ticker"] for v in data.values()}
    except (AttributeError, KeyError, TypeError):
        log.warning("144: unexpected ticker map shape")
        return {}
    return _ticker_cache


def _strip_ns(xml: str) -> str:
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)
    xml = re.sub(r'\s\w+:\w+="[^"]*"', "", xml)
    return re.sub(r"<(/?)\w+:", r"\1", xml).replace("<>", "<")


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def parse_144(raw: str, accession: str, url: str, tickers: dict) -> Trade | None:
    m = re.search(r"<edgarSubmission.*?</edgarSubmission>", raw, re.S)
    if not m:
        return None
    try:
        doc = ET.fromstring(_strip_ns(m.group(0)))
    except ET.ParseError as e:
        log.debug("144: unparseable %s: %s", accession, e)
        return None

    issuer = doc.find(".//issuerInfo")
    sec_info = doc.find(".//securitiesInformation")
    if issuer is None or sec_info is None:
        return None

    cik = (issuer.findtext("issuerCik") or "").strip().lstrip("0")
    company = (issuer.findtext("issuerName") or "").strip()
    person = (issuer.findtext(
        "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") or "").strip()
    rel = (issuer.findtext(".//relationshipToIssuer") or "").strip()

    value = _num(sec_info.findtext("aggregateMarketValue"))
    units = _num(sec_info.findtext("noOfUnitsSold"))
    sale_date = _date(sec_info.findtext("approxSaleDate") or "")
    notice = _date(doc.findtext(".//noticeDate") or "")

    return Trade(
        source="form144",
        uid=accession,
        person=person or company or "(unnamed affiliate)",
        role=rel or "Affiliate",
        action="SELL",
        company=company,
        ticker=tickers.get(cik, ""),
        shares=units,
        value_usd=value,
        code="144",
        trade_date=sale_date,
        filed_date=notice,
        url=url,
        note="intends to sell (Form 144 notice, filed before the sale)",
    )


def collect(fetcher, state, limit: int = 0, max_pages: int = 5) -> list[Trade]:
    found: dict[str, str] = {}
    for page in range(max_pages):
        xml = fetcher.get_text(CURRENT_URL.format(start=page * 100))
        if not xml:
            break
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            break
        entries = root.findall("a:entry", ATOM_NS)
        if not entries:
            break
        seen_here = 0
        for e in entries:
            ident = e.findtext("a:id", default="", namespaces=ATOM_NS)
            mm = _ACC_RE.search(ident)
            if not mm:
                continue
            acc = mm.group(1)
            if state.is_seen("form144", acc):
                seen_here += 1
                continue
            link = e.find("a:link", ATOM_NS)
            found.setdefault(acc, link.get("href", "") if link is not None else "")
        if seen_here > len(entries) * 0.8:
            break

    if not state.count("form144") and found:
        log.warning("144: first run -- marking %d existing notices as seen",
                    len(found))
        for acc in found:
            state.mark_seen("form144", acc)
        return []

    if limit and len(found) > limit:
        found = dict(list(found.items())[:limit])

    tickers = ticker_map(fetcher)
    trades: list[Trade] = []
    for acc, link in found.items():
        url = link.replace("-index.htm", ".txt") if link else ""
        if not url:
            continue
        raw = fetcher.get_text(url, allow_404=True)
        if not raw:
            continue
        state.mark_seen("form144", acc)
        t = parse_144(raw, acc, link, tickers)
        if t:
            trades.append(t)

    log.info("144: %d proposed sales from %d notices", len(trades), len(found))
    return trades
