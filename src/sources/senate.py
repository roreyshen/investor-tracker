"""US Senate periodic transaction reports (STOCK Act).

Easier than the House in one way -- e-filed reports are HTML tables, not PDFs,
so no PDF parsing. Harder in another: the search sits behind a one-time
"prohibition agreement" gate, so every session has to walk three steps before
it can query anything:

    GET  /search/home/            -> scrape csrfmiddlewaretoken
    POST /search/home/            -> accept the agreement (sets a cookie)
    POST /search/report/data/     -> the actual search endpoint

Same 30-45 day statutory lag as the House. Senators who file on paper get the
same link-only fallback.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime

from ..models import Trade

log = logging.getLogger(__name__)

BASE = "https://efdsearch.senate.gov"
HOME = f"{BASE}/search/home/"
SEARCH = f"{BASE}/search/report/data/"
REPORT_TYPE_PTR = 11

# The site serves no API and rejects unfamiliar clients, so present as a normal
# browser. Requests stay slow and low-volume.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")

CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
LINK_RE = re.compile(r'/search/view/(ptr|paper)/([0-9a-f-]+)/')
ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)

ACTION = {"purchase": "BUY", "sale": "SELL", "exchange": "OTHER"}


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _d(s: str):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def _authenticate(fetcher) -> bool:
    """Walk the agreement gate. Cookies persist on this fetcher's session."""
    home = fetcher.get_text(HOME)
    if not home:
        return False
    m = CSRF_RE.search(home)
    if not m:
        log.error("senate: no CSRF token on home page (layout changed?)")
        return False
    r = fetcher.post(HOME, data={"prohibition_agreement": "1",
                                 "csrfmiddlewaretoken": m.group(1)},
                     headers={"Referer": HOME})
    return r is not None


def search_ptrs(fetcher, since: date, limit: int = 100) -> list[dict]:
    token = fetcher.session.cookies.get("csrftoken")
    if not token:
        log.error("senate: no csrftoken cookie after agreement")
        return []

    r = fetcher.post(
        SEARCH,
        headers={"X-CSRFToken": token, "X-Requested-With": "XMLHttpRequest",
                 "Referer": f"{BASE}/search/"},
        data={
            "start": "0", "length": str(limit),
            "report_types": f"[{REPORT_TYPE_PTR}]", "filer_types": "[]",
            "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"),
            "submitted_end_date": "", "candidate_state": "",
            "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
        },
    )
    if r is None:
        return []
    try:
        rows = r.json().get("data", [])
    except ValueError:
        log.error("senate: search returned non-JSON")
        return []

    out = []
    for row in rows:
        if len(row) < 5:
            continue
        m = LINK_RE.search(row[3])
        if not m:
            continue
        out.append({
            "kind": m.group(1), "uuid": m.group(2),
            "first": _clean(row[0]), "last": _clean(row[1]),
            "office": _clean(row[2]), "filed": _clean(row[4]),
        })
    log.info("senate: %d PTR filings since %s", len(out), since)
    return out


def parse_report(text: str) -> list[dict]:
    """Parse the transaction table.

    Columns: # | Transaction Date | Owner | Ticker | Asset Name | Asset Type |
             Type | Amount | Comment
    """
    body = re.search(r"<tbody>(.*?)</tbody>", text, re.S)
    if not body:
        return []

    out = []
    for tr in ROW_RE.finditer(body.group(1)):
        cells = [_clean(c) for c in CELL_RE.findall(tr.group(1))]
        if len(cells) < 8:
            continue
        ttype = cells[6]
        # "Sale (Partial)" / "Sale (Full)" / "Purchase" / "Exchange"
        action = ACTION.get(ttype.split()[0].lower().rstrip(":"), "OTHER")
        ticker = cells[3] if cells[3] not in ("--", "") else ""
        out.append({
            "trade_date": _d(cells[1]), "owner": cells[2], "ticker": ticker,
            "asset": cells[4][:120], "asset_type": cells[5],
            "action": action, "ttype": ttype, "amount": cells[7],
        })
    return out


def collect(fetcher, state, since: date | None = None,
            limit: int = 0) -> list[Trade]:
    since = since or date(date.today().year, 1, 1)
    # Isolated session: this site needs a browser User-Agent, and reusing the
    # shared one would leave that string set for subsequent SEC requests,
    # which answer 403 to browser agents.
    fetcher = fetcher.with_user_agent(BROWSER_UA)
    if not _authenticate(fetcher):
        log.error("senate: could not pass the agreement gate, skipping")
        return []

    filings = search_ptrs(fetcher, since)
    new = [f for f in filings if not state.is_seen("senate", f["uuid"])]

    if not state.count("senate") and new:
        log.warning("senate: first run -- marking %d existing filings as seen",
                    len(new))
        for f in new:
            state.mark_seen("senate", f["uuid"])
        return []

    if limit and len(new) > limit:
        log.warning("senate: capping %d new filings at %d", len(new), limit)
        new = new[:limit]

    trades: list[Trade] = []
    for f in new:
        url = f"{BASE}/search/view/{f['kind']}/{f['uuid']}/"
        state.mark_seen("senate", f["uuid"])
        # "Last, First" to match how EDGAR and the House clerk write names --
        # the shared formatter takes the leading token as the surname.
        person = (f"{f['last']}, {f['first']}".strip(", ")
                  if f["last"] else f["office"])
        filed = _d(f["filed"])

        if f["kind"] == "paper":
            trades.append(Trade(
                source="senate", uid=f["uuid"], person=person, role="Senator",
                action="OTHER", filed_date=filed, url=url,
                note="paper filing - open the report for details",
            ))
            continue

        page = fetcher.get_text(url, allow_404=True)
        rows = parse_report(page) if page else []
        if not rows:
            trades.append(Trade(
                source="senate", uid=f["uuid"], person=person, role="Senator",
                action="OTHER", filed_date=filed, url=url,
                note="could not read transaction table - open the report",
            ))
            continue

        for i, t in enumerate(rows):
            owner = f" ({t['owner']})" if t["owner"] not in ("Self", "--", "") else ""
            trades.append(Trade(
                source="senate", uid=f"{f['uuid']}:{i}", person=person,
                role="Senator" + owner, action=t["action"],
                company=t["asset"], ticker=t["ticker"],
                value_range=t["amount"], trade_date=t["trade_date"],
                filed_date=filed, url=url, code=t["ttype"],
            ))

    log.info("senate: %d transactions from %d new filings", len(trades), len(new))
    return trades
