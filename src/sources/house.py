"""US House periodic transaction reports (STOCK Act).

Two-step source: a ZIP index lists every filing, and the trade details live in
a separate PDF per filing.

Reality check on speed -- members have a 30-45 day reporting window, and the
Clerk refreshes the ZIP on business days. So this is never breaking news. Every
alert carries its lag so a 40-day-old trade doesn't read like one.

Some members still file on paper. Those PDFs are scanned images with no
extractable text, so rather than pull in an OCR dependency we alert with a link
and say plainly that the details aren't machine-readable.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, datetime

from ..models import Trade

log = logging.getLogger(__name__)

ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

# "P 12/22/2025 12/22/2025 $1,001 - $15,000", with the amount free to wrap.
TXN_RE = re.compile(
    r"(?P<ttype>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notify>\d{2}/\d{2}/\d{4})\s+"
    r"\$(?P<lo>[\d,]+)\s*-\s*\$(?P<hi>[\d,]+)"
)
TICKER_RE = re.compile(r"\(([A-Z][A-Z.\-]{0,5})\)\s*\[[A-Z]{2}\]")
NAME_RE = re.compile(r"Name:\s*(.+)")
DISTRICT_RE = re.compile(r"State/District:\s*([A-Z]{2}\d{2})")

ACTION = {"P": "BUY", "S": "SELL", "S (partial)": "SELL", "E": "OTHER"}

# The PDFs render field labels with letter-spacing, so extraction turns
# "Filing Status: New" into "F      S     : New". Those lines sit between
# transactions and would otherwise be swept into the next asset's name.
# Matching the shape (letter, gap, letters, colon) is more robust than trying
# to anticipate every mangled label.
# Matches "F      S     : New", "S          O : Putnam", and the single-letter
# "D          : The full transaction included..." -- one or more spaced-out
# capitals ending in a colon.
_JUNK_LINE_RE = re.compile(r"^\s*[A-Z](?:\s{2,}[A-Z]+)*\s*:")
_HEADER_END = "$200?"
# Footer and label debris that can survive line-level filtering once the text
# is flattened, e.g. "Filing ID #20034916" or a mangled "L : US D : T".
# Free-text description debris: prices, share counts, en-dashes. Real asset
# names don't contain these.
_DESC_SMELL_RE = re.compile(
    r"(shares?\s+sold|@\s*\$|\u2013|\u2014|included the following)")
_TRAILING_JUNK_RE = re.compile(
    r"(Filing ID #?\d+|\b[A-Z]\s*:\s*[A-Z]{1,3}\b|Digitally Signed.*?$)")


def _d(s: str):
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def fetch_index(fetcher, year: int, etag: str | None = None):
    """Return (rows, new_etag). rows are PTR entries only.

    The ZIP is small but refetched every run, so a conditional request keeps
    this nearly free on the ~95% of runs where nothing changed.
    """
    headers = {"If-None-Match": etag} if etag else None
    r = fetcher.get(ZIP_URL.format(year=year), headers=headers, allow_404=True)
    if r is None:
        return [], etag
    if r.status_code == 304:
        log.info("house: index unchanged")
        return [], etag

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        text = zf.read(name).decode("latin-1")
    except (zipfile.BadZipFile, StopIteration, KeyError) as e:
        log.error("house: bad index zip: %s", e)
        return [], etag

    rows = []
    for line in text.splitlines()[1:]:
        f = line.split("\t")
        if len(f) > 8 and f[4].strip() == "P":      # P = periodic transaction report
            rows.append({
                "last": f[1].strip(), "first": f[2].strip(),
                "district": f[5].strip(), "filed": f[7].strip(),
                "doc": f[8].strip(),
            })
    log.info("house: %d PTR filings in %d index", len(rows), year)
    return rows, r.headers.get("ETag", etag)


def parse_ptr_pdf(data: bytes) -> tuple[list[dict], str, str]:
    """Return (transactions, member_name, district). Empty list => scanned."""
    try:
        import pypdf
        pages = [p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(data)).pages]
    except Exception as e:                       # noqa: BLE001 - pypdf raises broadly
        log.warning("house: unreadable PDF: %s", e)
        return [], "", ""

    # These PDFs letter-space their field labels with NUL bytes, so
    # "Filing Status:" extracts as "F\x00\x00\x00\x00\x00 S...". NUL is not
    # whitespace to `re`, so normalize it before anything tries to match.
    pages = [pg.replace("\x00", " ") for pg in pages]
    full = "\n".join(pages)

    name_m, dist_m = NAME_RE.search(full), DISTRICT_RE.search(full)
    name = name_m.group(1).strip() if name_m else ""
    district = dist_m.group(1).strip() if dist_m else ""

    out = []
    for page in pages:
        out.extend(_parse_page(page))
    return out, name, district


# The asset-TYPE bracket ends every asset name. The parenthesized ticker is
# optional -- plenty of holdings are private funds or bonds with no symbol
# ("Aalo Atomics [OI]"), and anchoring on the ticker silently dropped them.
_ASSET_END_RE = re.compile(r"\[[A-Z]{2}\]")
# A transaction whose amount wrapped, e.g. "... $15,001 -" / "$50,000". Matched
# anywhere in the line, since the asset can share the line with it.
_TXN_WRAP_RE = re.compile(
    r"(?:S \(partial\)|P|S|E)\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}"
    r"\s+\$[\d,]+\s*-\s*$")
_ORPHAN_TICKER_RE = re.compile(r"\([A-Z][A-Z.\-]{0,5}\)\s*$")
_ORPHAN_TYPE_RE = re.compile(r"^\[[A-Z]{2}\]")
# The "Owner" column (SP = spouse, JT = joint, DC = dependent child) renders
# as a prefix on the asset name.
_OWNER_PREFIX_RE = re.compile(r"^(SP|JT|DC)\s+")
# A trailing date from the previous row's description can lead the next asset
# name, e.g. "1/16/26. SP Apple Inc. - Common Stock".
_LEAD_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}\.?\s*")


def _merge_wrapped(lines: list[str]) -> list[str]:
    """Rejoin an asset marker that wrapped, e.g. "... (NFLX)" / "[ST]"."""
    out: list[str] = []
    for ln in lines:
        if out and _ORPHAN_TYPE_RE.match(ln) and _ORPHAN_TICKER_RE.search(out[-1]):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    return out


def _parse_page(text: str) -> list[dict]:
    """Transactions on one page.

    Line-based rather than a flat regex sweep, because the optional
    "Description:" field wraps across lines that look exactly like asset names
    ("sold @ $493.42/share SPY - 8.318 shares..."). Anchoring each row on its
    "(TICKER) [XX]" marker keeps that prose out of the asset field.

    Per page, because a multi-page PTR repeats the header and footer and
    bounding on the first footer lets later pages' boilerplate leak in.
    """
    start = text.find(_HEADER_END)
    if start >= 0:
        start += len(_HEADER_END)
    else:
        # Page 2+ of a long filing repeats no header, so an absent marker means
        # "continuation", not "nothing here". Bailing lost whole pages.
        start = text.find("ID Owner")
        start = 0 if start < 0 else start
    end = text.find("* For the complete", start)
    lines = _merge_wrapped(
        [ln.strip() for ln in
         text[start: end if end > 0 else len(text)].splitlines()])

    out: list[dict] = []
    asset_lines: list[str] = []
    prev: str = ""
    skipping = False
    i = 0

    while i < len(lines):
        ln = lines[i]
        i += 1
        if not ln:
            continue

        # A spaced-out label ("D          : ...") starts free-text that runs
        # until the next asset row.
        if _JUNK_LINE_RE.match(ln):
            skipping, asset_lines, prev = True, [], ""
            continue

        # Resolve the transaction match first: a row can carry its asset
        # marker and its transaction on one line ("AT&T Inc. (T) [ST] S
        # (partial) 03/16/2026 ..."), and the amount can wrap to the next.
        m = TXN_RE.search(ln)
        if not m and _TXN_WRAP_RE.search(ln) and i < len(lines):
            m = TXN_RE.search(ln + " " + lines[i])
            if m:
                i += 1

        # The "[ST]" asset-type bracket is the reliable end of an asset name.
        if _ASSET_END_RE.search(ln):
            if skipping:
                # Description prose ran right up to this row; keep only the
                # line before the marker, and only if it reads like a name.
                asset_lines = [prev] if prev and not _DESC_SMELL_RE.search(prev) else []
                skipping = False
            # Combined line: the asset is only the part before the transaction.
            asset_lines.append(ln[:min(m.start(), len(ln))] if m else ln)
            if not m:
                prev = ln
                continue

        if m:
            blob = " ".join(asset_lines)
            tk = TICKER_RE.search(blob)
            ticker = tk.group(1) if tk else ""
            asset = re.sub(r"\([A-Z.\-]{1,6}\)\s*\[[A-Z]{2}\]", "", blob)
            # Assets with no symbol carry a bare type bracket ("Aalo Atomics
            # [OI]"), which the combined pattern above doesn't reach.
            asset = _ASSET_END_RE.sub("", asset)
            asset = _TRAILING_JUNK_RE.sub(" ", asset)
            asset = re.sub(r"\s+", " ", asset).strip(" -,:")
            asset = _OWNER_PREFIX_RE.sub("", _LEAD_DATE_RE.sub("", asset))
            if _DESC_SMELL_RE.search(asset) or len(re.sub(r"[^A-Za-z0-9]", "", asset)) < 3:
                asset = ""          # formatter falls back to the ticker

            ttype = m.group("ttype")
            out.append({
                "asset": asset[:120], "ticker": ticker,
                "action": ACTION.get(ttype, "OTHER"), "ttype": ttype,
                "trade_date": _d(m.group("date")),
                "amount": f"${m.group('lo')} - ${m.group('hi')}",
            })
            asset_lines, prev, skipping = [], "", False
            continue

        if not skipping:
            asset_lines.append(ln)
        prev = ln

    return out


def collect(fetcher, state, year: int | None = None,
            limit: int = 0) -> list[Trade]:
    year = year or date.today().year
    etag = state.data.get("house_etag")
    rows, new_etag = fetch_index(fetcher, year, etag)
    if new_etag:
        state.data["house_etag"] = new_etag

    new_rows = [r for r in rows if not state.is_seen("house", r["doc"])]

    # Cold start: don't dump a year of back-filings on the user.
    if not state.count("house") and new_rows:
        log.warning("house: first run -- marking %d existing filings as seen",
                    len(new_rows))
        for r in new_rows:
            state.mark_seen("house", r["doc"])
        return []

    if limit and len(new_rows) > limit:
        log.warning("house: capping %d new filings at %d", len(new_rows), limit)
        new_rows = new_rows[:limit]

    trades: list[Trade] = []
    for row in new_rows:
        doc = row["doc"]
        url = PDF_URL.format(year=year, doc=doc)
        resp = fetcher.get(url, allow_404=True)
        state.mark_seen("house", doc)
        if resp is None:
            continue

        txns, name, district = parse_ptr_pdf(resp.content)
        # The index has surname and given name in separate columns; the PDF
        # only has "Hon. Mark Alford", where the surname is last. Use the
        # index and write "Last, First" so the shared formatter picks the
        # surname like it does for every other source.
        person = (f"{row['last']}, {row['first']}".strip(", ")
                  or name or "(unknown)")
        role = f"Rep. {district or row['district']}"
        filed = _d(row["filed"])

        if not txns:
            # Scanned paper filing: report that it happened, link to the source.
            trades.append(Trade(
                source="house", uid=doc, person=person, role=role,
                action="OTHER", filed_date=filed, url=url,
                note="scanned paper filing - open the PDF for details",
            ))
            continue

        for i, t in enumerate(txns):
            # Some rows sit inside a long free-text "Description" block that
            # swallows the asset name. The amount and date are still exact, so
            # report the trade and point at the filing for the security.
            unnamed = not t["ticker"] and not t["asset"]
            trades.append(Trade(
                source="house", uid=f"{doc}:{i}", person=person, role=role,
                action=t["action"], company=t["asset"], ticker=t["ticker"],
                value_range=t["amount"], trade_date=t["trade_date"],
                filed_date=filed, url=url, code=t["ttype"],
                note="security not machine-readable - open the filing" if unnamed else "",
            ))

    log.info("house: %d transactions from %d new filings", len(trades), len(new_rows))
    return trades
