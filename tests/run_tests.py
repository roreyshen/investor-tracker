"""Offline tests. No network, no credentials.

    python tests/run_tests.py

Covers the logic most likely to break silently: name matching across the three
different formats the sources use, dollar formatting, and the filter rules.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from src.filters import Watchlist, evaluate, find_cluster_buys, name_tokens  # noqa: E402
from src.models import Trade  # noqa: E402
from src.notify.format import compact_range, sms_line  # noqa: E402
from src.sources.edgar_form4 import _submission_url, parse_form4  # noqa: E402

PASS = FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def mk(**kw) -> Trade:
    base = dict(source="form4", uid="u", person="X", action="BUY")
    base.update(kw)
    return Trade(**base)


# ── name matching: the three formats the sources actually emit ──────────────
wl = Watchlist(yaml.safe_load(
    Path(__file__).parent.parent.joinpath("config/watchlist.yml").read_text()))

for raw, want in [
    ("PELOSI NANCY", "Nancy Pelosi"),           # EDGAR: LAST FIRST
    ("Hon. Pelosi, Nancy", "Nancy Pelosi"),     # House clerk
    ("Nancy Pelosi", "Nancy Pelosi"),           # plain
    ("PELOSI NANCY PATRICIA", "Nancy Pelosi"),  # extra middle name
    ("Wasserman Schultz, Debbie", "Debbie Wasserman Schultz"),
    ("Pelosi, Paul", None),                     # different person
    ("PELOSI", None),                           # bare surname must not match
    ("Smith John", None),
]:
    check(f"watchlist({raw!r})", wl.match(raw), want)

check("cik match", wl.match("Whoever", cik="0001494730"), "Elon Musk")

# Nicknames: watchlists are written the way people are known, filings use the
# legal name. "Tommy Tuberville" vs "Tuberville, Thomas H" silently never
# matched before this -- a watchlist entry that looks configured and never
# fires is worse than no entry at all.
for _filed, _want in [("Tuberville, Thomas H", "Tommy Tuberville"),
                      ("Scott, Richard L", "Rick Scott"),
                      ("LaLota, Nicholas", "Nick LaLota"),
                      ("Allen, Richard W.", "Richard Allen")]:
    check(f"nickname: {_filed!r}", wl.match(_filed), _want)
check("nickname map doesn't create false matches", wl.match("Scott, Michael"), None)

# Aliases cover name forms no nickname map can reach. Jensen Huang files as
# "HUANG JEN HSUN" -- 271 trades that matched nothing before this.
check("alias matches the filed legal name", wl.match("HUANG JEN HSUN"), "Jensen Huang")
check("primary name still matches", wl.match("Huang Jensen"), "Jensen Huang")
check("alias does not match a different Huang", wl.match("Huang Victor"), None)
check("name_tokens drops titles", name_tokens("Hon. Pelosi, Nancy Jr."),
      {"pelosi", "nancy"})

# ── dollar formatting ──────────────────────────────────────────────────────
check("amount 4.2M", mk(value_usd=4_200_000).amount_str, "$4.2M")
check("amount exact 1M", mk(value_usd=1_000_000).amount_str, "$1M")
check("amount 28K", mk(value_usd=28_380).amount_str, "$28K")
check("range 1M-5M", compact_range("$1,000,001 - $5,000,000"), "$1M-5M")
check("range 1K-15K", compact_range("$1,001 - $15,000"), "$1K-15K")

# ── SMS length budget ──────────────────────────────────────────────────────
long_sms = sms_line(mk(
    person="Control Empresarial de Capitales S.A. de C.V.",
    role="President and Chief Executive Officer", ticker="LONGTICKER",
    company="A Very Long Company Name Indeed", value_usd=1_234_567_890,
    trade_date=date(2026, 1, 2), filed_date=date(2026, 3, 30), code="P",
    note="scanned PDF, tap to view"))
check("sms <= 160 chars", len(long_sms) <= 160, True)

# ── filter rules ───────────────────────────────────────────────────────────
settings = {"filters": {"min_insider_buy_usd": 1_000_000,
                        "min_insider_sell_usd": 10_000_000,
                        "signal_codes": ["P", "S"],
                        "cluster_min_insiders": 3, "cluster_window_days": 7}}
empty_wl = Watchlist({})

cases = [
    ("big code-P buy alerts",
     mk(code="P", action="BUY", value_usd=2_000_000, ticker="AAA"), 1),
    ("small code-P buy is silent",
     mk(code="P", action="BUY", value_usd=50_000, ticker="AAA"), 0),
    ("grant (code A) is silent even when huge",
     mk(code="A", action="BUY", value_usd=90_000_000, ticker="AAA"), 0),
    ("tax withholding (code F) is silent",
     mk(code="F", action="SELL", value_usd=90_000_000, ticker="AAA"), 0),
    ("option exercise (code M) is silent",
     mk(code="M", action="BUY", value_usd=90_000_000, ticker="AAA"), 0),
    ("sell under the higher sell floor is silent",
     mk(code="S", action="SELL", value_usd=2_000_000, ticker="AAA"), 0),
    ("big sell alerts",
     mk(code="S", action="SELL", value_usd=20_000_000, ticker="AAA"), 1),
]
for label, trade, want in cases:
    check(label, len(evaluate([trade], settings, empty_wl, [])), want)

# watchlist bypasses every threshold
check("watchlist trade alerts at any size",
      len(evaluate([mk(person="PELOSI NANCY", code="P", value_usd=1.0,
                       ticker="AAA")], settings, wl, [])), 1)

# ── cluster detection spans runs via persisted history ─────────────────────
today = date.today().isoformat()
history = [{"person": "A", "ticker": "ZZZ", "date": today},
           {"person": "B", "ticker": "ZZZ", "date": today}]
now = date.today()
check("cluster fires on 3rd distinct insider",
      find_cluster_buys([mk(code="P", ticker="ZZZ", person="C", trade_date=now)],
                        history, 3, 7), {"ZZZ"})
check("cluster does not fire on a repeat buyer",
      find_cluster_buys([mk(code="P", ticker="ZZZ", person="A", trade_date=now)],
                        history, 3, 7), set())

# ── Form 4 URL derivation ──────────────────────────────────────────────────
check("submission url",
      _submission_url("https://www.sec.gov/Archives/edgar/data/1/2/0001-26-1-index.htm",
                      "0001-26-1"),
      "https://www.sec.gov/Archives/edgar/data/1/2/0001-26-1.txt")
check("empty index link", _submission_url("", "x"), "")
check("garbage xml yields no trades", parse_form4("not xml", "a", "u"), [])

# ── House PDF parsing ──────────────────────────────────────────────────────
from src.sources.house import _merge_wrapped, _parse_page  # noqa: E402

# A "(TICKER)" that wrapped away from its "[ST]" must be rejoined, or the row
# loses its anchor and the whole transaction disappears.
check("merge wrapped ticker marker",
      _merge_wrapped(["Netflix, Inc. (NFLX)", "[ST]", "other"]),
      ["Netflix, Inc. (NFLX) [ST]", "other"])
check("merge leaves unrelated lines alone",
      _merge_wrapped(["Apple Inc. (AAPL) [ST]", "P 01/02/2026 01/03/2026 $1 - $2"]),
      ["Apple Inc. (AAPL) [ST]", "P 01/02/2026 01/03/2026 $1 - $2"])

PAGE = """$200?
Amazon.com, Inc. - Common Stock
(AMZN) [ST]
S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000
F      S     : New
D          : The full transaction included the following sales: T - 37 shares
sold @ $493.42/share SPY - 8.318 shares sold @ $670.02/share
AT&T Inc. (T) [ST] S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000
Aalo Atomics [OI] P 01/29/2026 01/29/2026 $15,001 -
$50,000
SP Allocate Alpha Fund II LP [OT] P 12/19/2025 12/31/2025 $1,001 - $15,000
* For the complete list
"""
rows = _parse_page(PAGE)
check("page yields every row", len(rows), 4)
check("ticker on its own line", (rows[0]["ticker"], rows[0]["action"]), ("AMZN", "SELL"))
check("asset name kept clean of description prose",
      rows[0]["asset"], "Amazon.com, Inc. - Common Stock")
check("asset and transaction sharing one line",
      (rows[1]["ticker"], rows[1]["asset"]), ("T", "AT&T Inc."))
check("no-ticker asset still parsed",
      (rows[2]["ticker"], rows[2]["asset"], rows[2]["action"]),
      ("", "Aalo Atomics", "BUY"))
check("wrapped amount rejoined", rows[2]["amount"], "$15,001 - $50,000")
check("owner prefix stripped", rows[3]["asset"], "Allocate Alpha Fund II LP")

# The clerk prefixes rows with an internal id, and some asset-type brackets
# are typo'd in the source ("[MF}"). Both must be stripped from the name.
ID_PAGE = """$200?
2000140446 American Funds AMCAP Fund
Class A M/F [MF} [OT]
S 06/03/2025 06/04/2025 $500,001 - $1,000,000
* For the complete list
"""
_idrows = _parse_page(ID_PAGE)
check("clerk transaction id stripped", _idrows[0]["asset"].startswith("American Funds"), True)
check("typo'd asset bracket stripped", "[MF}" in _idrows[0]["asset"], False)

# An amendment can correct a trade from over a year ago, so it surfaces with a
# huge lag. Flagging it is what stops that looking like a bug.
from src.sources.house import _AMENDED_RE  # noqa: E402
check("amended filing detected", bool(_AMENDED_RE.search("F      S     : Amended")), True)
check("new filing not flagged amended", bool(_AMENDED_RE.search("F      S     : New")), False)

# ── Senate HTML parsing ────────────────────────────────────────────────────
from src.sources.senate import parse_report  # noqa: E402

HTML = """<table><tbody>
<tr><td>1</td><td>08/27/2026</td><td>Joint</td><td>IVV</td>
<td>iShares Core S&amp;P 500 ETF</td><td>Stock</td><td>Sale (Partial)</td>
<td>$1,001 - $15,000</td><td>--</td></tr>
<tr><td>2</td><td>08/13/2026</td><td>Self</td><td>--</td>
<td>Some Corporate Bond</td><td>Corporate Bond</td><td>Purchase</td>
<td>$15,001 - $50,000</td><td>--</td></tr>
</tbody></table>"""
srows = parse_report(HTML)
check("senate row count", len(srows), 2)
check("senate sale maps to SELL", srows[0]["action"], "SELL")
check("senate html entity decoded", srows[0]["asset"], "iShares Core S&P 500 ETF")
check("senate purchase maps to BUY", srows[1]["action"], "BUY")
check("senate empty ticker normalized", srows[1]["ticker"], "")
check("senate no tbody -> no rows", parse_report("<p>nothing</p>"), [])

# ── 13F holdings ───────────────────────────────────────────────────────────
from src.sources.edgar_13f import _diff, parse_holdings  # noqa: E402

TABLE = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf">
<infoTable><nameOfIssuer>ALPHABET INC</nameOfIssuer><titleOfClass>CAP STK CL C</titleOfClass>
<cusip>02079K107</cusip><value>1000</value>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>ALPHABET INC</nameOfIssuer><titleOfClass>CAP STK CL C</titleOfClass>
<cusip>02079K107</cusip><value>500</value>
<shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>KROGER CO</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>501044101</cusip><value>900</value>
<shrsOrPrnAmt><sshPrnamt>90</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""

h = parse_holdings(TABLE)
check("13f aggregates duplicate cusips", len(h), 2)
# sshPrnamt is nested under shrsOrPrnAmt; reading it as a direct child yields
# 0 shares and silently disables every diff.
check("13f sums shares across managers", h["02079K107"]["shares"], 150.0)
check("13f sums value across managers", h["02079K107"]["value"], 1500.0)
check("13f keeps share class", h["02079K107"]["title"], "CAP STK CL C")
check("13f bad xml -> empty", parse_holdings("<nope/>"), {})

# Some filers put xsi:schemaLocation on the root. Removing the xmlns:xsi
# declaration without removing that attribute leaves an unbound prefix and the
# whole filing fails to parse.
NS_TABLE = TABLE.replace(
    '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf">',
    '<informationTable xsi:schemaLocation="http://x a.xsd" '
    'xmlns="http://www.sec.gov/edgar/document/thirteenf" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
check("13f parses prefixed-attribute variant", len(parse_holdings(NS_TABLE)), 2)

_prev = {"AAA": {"name": "Held", "title": "COM", "value": 100.0, "shares": 100.0},
         "BBB": {"name": "Gone", "title": "COM", "value": 50.0, "shares": 50.0}}
_cur = {"AAA": {"name": "Held", "title": "COM", "value": 200.0, "shares": 200.0},
        "CCC": {"name": "Fresh", "title": "COM", "value": 10.0, "shares": 10.0}}
_dd = {t.company: t for t in _diff("F", _prev, _cur, "u", None, "2026-06-30", "a")}
check("13f flags a doubled position", _dd["Held"].action, "BUY")
check("13f flags an exit", _dd["Gone"].action, "SELL")
check("13f exit reports the prior size", _dd["Gone"].value_usd, 50.0)
check("13f flags a new position", _dd["Fresh"].note, "NEW position")
check("13f ignores drift below the threshold",
      _diff("F", {"AAA": {"name": "H", "value": 100.0, "shares": 100.0}},
            {"AAA": {"name": "H", "value": 105.0, "shares": 105.0}},
            "u", None, "2026-06-30", "a"), [])

# ── seniority weighting ────────────────────────────────────────────────────
from src.filters import is_senior, range_low  # noqa: E402

_TITLES = ["CEO", "Chief Executive", "CFO", "Chief Financial", "President",
           "Chairman", "Chief Operating", "COO"]
check("CEO is senior", is_senior("President and Chief Executive Officer", _TITLES), True)
check("CFO is senior", is_senior("Chief Financial Officer", _TITLES), True)
check("VP is not senior", is_senior("VP of Sales", _TITLES), False)
check("plain Director is not senior", is_senior("Director", _TITLES), False)
check("empty role is not senior", is_senior("", _TITLES), False)

_S = {"filters": {"min_insider_buy_usd": 1_000_000, "min_senior_buy_usd": 250_000,
                  "senior_titles": _TITLES, "min_insider_sell_usd": 10_000_000,
                  "min_congress_usd": 50_000, "signal_codes": ["P", "S"],
                  "cluster_min_insiders": 3, "cluster_window_days": 7}}
_wl = Watchlist({})


def _alerts(role, value, action="BUY", code="P"):
    return len(evaluate([mk(role=role, action=action, code=code, ticker="AAA",
                            value_usd=value)], _S, _wl, []))


check("senior officer clears the lower bar", _alerts("Chief Executive Officer", 400_000), 1)
check("junior officer does not", _alerts("VP of Sales", 400_000), 0)
check("senior officer still has a floor", _alerts("Chief Executive Officer", 100_000), 0)
check("junior officer clears the general bar", _alerts("VP of Sales", 1_500_000), 1)

# Congress alerts off the bottom of the disclosed band, not an exact figure.
_c = mk(source="house", action="BUY", value_range="$50,001 - $100,000", ticker="AAA")
check("congress band above floor alerts", len(evaluate([_c], _S, _wl, [])), 1)
_c2 = mk(source="house", action="BUY", value_range="$1,001 - $15,000", ticker="AAA")
check("congress band below floor is silent", len(evaluate([_c2], _S, _wl, [])), 0)

# ── company size buckets ───────────────────────────────────────────────────
from src.prices import cap_tier  # noqa: E402

check("penny by price beats market cap", cap_tier(50e9, 3.0), "Penny (<$5)")
check("mega cap", cap_tier(500e9, 200.0), "Mega (>$200B)")
check("large cap", cap_tier(50e9, 200.0), "Large ($10-200B)")
check("micro cap", cap_tier(100e6, 12.0), "Micro (<$300M)")
check("unknown when nothing known", cap_tier(None, None), "Unknown")

# ── trade store dedupes by uid ─────────────────────────────────────────────
import tempfile  # noqa: E402
from src import store as _store  # noqa: E402
from src.notify import outbox as _outbox  # noqa: E402

with tempfile.TemporaryDirectory() as _td:
    _p = Path(_td) / "trades.json"
    _t1 = mk(uid="a", ticker="AAA", action="BUY")
    _t2 = mk(uid="b", ticker="BBB", action="SELL")
    check("store writes new trades", _store.append(_p, [_t1, _t2], {"a"}), 2)
    check("store ignores duplicates", _store.append(_p, [_t1], set()), 0)
    _rows = _store.load(_p)
    check("store keeps both", len(_rows), 2)
    check("store records alert status", _rows[0]["alerted"], True)
    check("store records non-alert", _rows[1]["alerted"], False)

    _o = Path(_td) / "outbox.json"
    check("outbox queues alerts", _outbox.append(_o, [_t1, _t2]), 2)
    check("outbox dedupes", _outbox.append(_o, [_t1]), 0)

# ── benchmark scoring sign convention ──────────────────────────────────────
# A buy is credited with the excess return; a sell is credited with its
# negative, because getting out before a fall is a good decision. Without
# this, sells would look like losses whenever they were correct.
from scripts.build_site import score_trades  # noqa: E402


class _FakePrices:
    """Stock doubles; the index is flat."""
    def history(self, sym, since):
        return {"2026-01-02": 100.0, "2026-09-01": 200.0 if sym != "SPY" else 100.0}

    def close_on_or_after(self, sym, when, window=10):
        return 100.0

    def latest(self, sym, since=None):
        return ("2026-09-01", 100.0 if sym == "SPY" else 200.0)


_rows = [
    {"uid": "buy", "ticker": "AAA", "trade_date": "2026-01-02", "action": "BUY",
     "source": "house", "person": "P"},
    {"uid": "sell", "ticker": "AAA", "trade_date": "2026-01-02", "action": "SELL",
     "source": "house", "person": "P"},
]
_sc = {t["uid"]: t for t in score_trades(_rows, _FakePrices(), {}, 0)}
check("buy on a doubling stock scores +100%", _sc["buy"]["return_pct"], 100.0)
check("buy beats a flat index by 100", _sc["buy"]["excess_pct"], 100.0)
check("sell out of a doubling stock scores -100", _sc["sell"]["excess_pct"], -100.0)
check("raw return is direction-free", _sc["sell"]["return_pct"], 100.0)

# ── SEC daily index is space-padded to fixed width ─────────────────────────
# rsplit-then-strip returned "" on every line, so every daily-index backfill
# silently parsed zero filings while reporting success.
import re as _re  # noqa: E402

_IDX_LINE = ("4                ADAMS JOHN K JR                       "
             "1625471     20260528    edgar/data/1625471/0001625471-26-000003.txt   ")
check("index path survives trailing padding",
      _IDX_LINE.split()[-1], "edgar/data/1625471/0001625471-26-000003.txt")
check("old rsplit-then-strip returned nothing",
      _IDX_LINE.rsplit(" ", 1)[-1].strip(), "")

# ── Form 144: notice of intent to sell, filed BEFORE the sale ─────────────
from src.sources.edgar_form144 import parse_144  # noqa: E402

F144 = """<edgarSubmission><headerData><submissionType>144</submissionType></headerData>
<formData><issuerInfo><issuerCik>0000320193</issuerCik>
<issuerName>Apple Inc.</issuerName>
<nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>DOE JANE</nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>
<relationshipsToIssuer><relationshipToIssuer>Officer</relationshipToIssuer></relationshipsToIssuer>
</issuerInfo><securitiesInformation><securitiesClassTitle>Common</securitiesClassTitle>
<noOfUnitsSold>1,000</noOfUnitsSold><aggregateMarketValue>7,500,000.00</aggregateMarketValue>
<approxSaleDate>09/11/2026</approxSaleDate></securitiesInformation>
<noticeSignature><noticeDate>09/11/2026</noticeDate></noticeSignature></formData></edgarSubmission>"""

_t144 = parse_144(F144, "acc-1", "http://u", {"320193": "AAPL"})
check("144 resolves ticker from CIK", _t144.ticker, "AAPL")
check("144 is always a sale", _t144.action, "SELL")
check("144 parses value", _t144.value_usd, 7_500_000.0)
check("144 parses units", _t144.shares, 1000.0)
check("144 names the person", _t144.person, "DOE JANE")
check("144 keeps the relationship", _t144.role, "Officer")
check("144 flags it as intent, not a completed sale",
      "intends to sell" in _t144.note, True)
check("144 garbage input is ignored", parse_144("nope", "a", "u", {}), None)

# Unknown CIK must not crash or invent a symbol.
check("144 unknown cik leaves ticker empty",
      parse_144(F144, "a", "u", {}).ticker, "")

_S144 = {"filters": {**_S["filters"], "min_form144_usd": 5_000_000}}
check("big proposed sale alerts",
      len(evaluate([_t144], _S144, _wl, [])), 1)
_small = parse_144(F144.replace("7,500,000.00", "100,000.00"), "b", "u", {})
check("small proposed sale is silent", len(evaluate([_small], _S144, _wl, [])), 0)

# ── price lookups must face the right direction ────────────────────────────
# Entries look forward (a trade on a Saturday fills at the next open session);
# exits and marks must look BACKWARD. Getting this wrong made positions near
# the end of a backtest mark at their entry price, reporting a flat 0.00%
# return that looked like a real result instead of missing data.
class _FakeStore:
    data = {}

    def history(self, sym, since):
        return {"2026-09-10": 100.0, "2026-09-11": 110.0}


from src.prices import PriceStore  # noqa: E402

_fs = _FakeStore()
_fwd = PriceStore.close_on_or_after.__get__(_fs, _FakeStore)
_bwd = PriceStore.close_on_or_before.__get__(_fs, _FakeStore)
_sunday = date(2026, 9, 13)
check("forward lookup finds nothing after the last close", _fwd("SPY", _sunday), None)
check("backward lookup finds the last close", _bwd("SPY", _sunday), 110.0)
check("forward lookup works from before a session", _fwd("SPY", date(2026, 9, 9)), 100.0)
check("backward lookup exact date", _bwd("SPY", date(2026, 9, 10)), 100.0)

# A ticker whose history starts later than the date asked for must not refetch
# on every call. Before this, min(closes) > since stayed true forever and a
# backtest sweep made thousands of redundant HTTP requests.
class _CountingStore(PriceStore):
    def __init__(self):
        self.data = {}
        self.fetches = 0
        self.min_interval = 0
        self._last = 0

    def _fetch(self, symbol, start, end):
        self.fetches += 1
        return {"2026-09-10": 100.0}


_cs = _CountingStore()
_old = date(2020, 1, 1)          # far earlier than the data goes
for _ in range(5):
    _cs.history("AAA", _old)
check("history does not refetch beyond available data", _cs.fetches, 1)
# Asking for something even older is a genuinely new request.
_cs.history("AAA", date(2015, 1, 1))
check("an older request does refetch once", _cs.fetches, 2)

# ── ntfy cloud push (dry-run: no network) ─────────────────────────────────
from src.notify import ntfy as _ntfy  # noqa: E402

check("ntfy with no topic sends nothing", _ntfy.send("", [mk(ticker="A")]), 0)
check("ntfy with no trades sends nothing", _ntfy.send("topic", []), 0)
_many = [mk(uid=f"n{i}", ticker=f"T{i}", value_usd=1e6) for i in range(10)]
# Six individual notifications plus one "+N more" summary, so a heavy filing
# day is a handful of buzzes rather than ten.
check("ntfy collapses a burst", _ntfy.send("topic", _many, dry_run=True),
      _ntfy.MAX_INDIVIDUAL + 1)
check("ntfy sends small batches individually",
      _ntfy.send("topic", _many[:3], dry_run=True), 3)

# HTTP headers are latin-1 only. A title with an em-dash or emoji raises
# UnicodeEncodeError inside http.client and the send fails outright -- which
# would have killed the scheduled digest silently.
check("em-dash transliterated in header",
      _ntfy._ascii_header("SMG picks \u2014 enter before 4pm"),
      "SMG picks - enter before 4pm")
check("smart quotes transliterated",
      _ntfy._ascii_header("\u201cbuy\u201d \u2018now\u2019"), '"buy" \'now\'')
check("emoji dropped rather than raising",
      _ntfy._ascii_header("\U0001F7E2 BUY"), " BUY")
_ntfy._ascii_header("x" * 300).encode("latin-1")  # must not raise

# ── Paper trading log ──────────────────────────────────────────────────────
# The point of this log is that it cannot cheat: a pick is recorded today and
# priced at a LATER real close, never the one that was already visible when it
# was chosen.
import scripts.paper as _paper  # noqa: E402

with tempfile.TemporaryDirectory() as _td:
    _paper.PAPER_DIR = Path(_td)
    _blank = _paper.load("teststrat")
    check("new log starts at the full portfolio", _blank["cash"], _paper.PORTFOLIO)
    check("new log has no positions", (_blank["open"], _blank["closed"]), ([], []))

    _blank["pending"] = [{"ticker": "AAA", "picked": "2026-09-14"}]
    _paper.save("teststrat", _blank)
    check("log round-trips", _paper.load("teststrat")["pending"][0]["ticker"], "AAA")
    check("log is written per strategy",
          _paper.path_for("teststrat").name, "teststrat.json")

# ── P/E must never be invented for a loss-making company ───────────────────
from src.fundamentals import Fundamentals  # noqa: E402

with tempfile.TemporaryDirectory() as _td:
    _f = Fundamentals(Path(_td) / "f.json")
    _f.data["PROF"] = {"fetched": date.today().isoformat(), "ttm_eps": 5.0}
    _f.data["LOSS"] = {"fetched": date.today().isoformat(), "ttm_eps": -0.88}
    _f.data["NONE"] = {"fetched": date.today().isoformat(), "ttm_eps": None}
    check("P/E from trailing earnings", _f.pe("PROF", 100.0), (20.0, ""))
    # A negative P/E is not "cheap", it is meaningless -- report the loss.
    check("loss-maker reports a loss, not a negative P/E",
          _f.pe("LOSS", 5.45), (None, "loss-making"))
    check("missing earnings reports no data", _f.pe("NONE", 10.0), (None, "no data"))

# ── Mac agent quiet hours ──────────────────────────────────────────────────
# The window wraps midnight, which is the easy thing to get wrong: a naive
# start <= h < end comparison silently never fires for 23->7.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mac_agent"))
from agent import in_quiet_hours  # noqa: E402

for _h, _want in [(22, False), (23, True), (0, True), (3, True), (6, True),
                  (7, False), (12, False)]:
    check(f"quiet hours at {_h:02d}:00",
          in_quiet_hours(datetime(2026, 9, 14, _h, 0)), _want)

# Regression guard. The first real alert failed because the message was
# interpolated into the AppleScript source: emoji became \ud83d\udd34 surrogate
# escapes that AppleScript cannot parse. The message must be passed as an
# argument instead, so no character in a company name can break the script.
import inspect  # noqa: E402
import agent as _agent  # noqa: E402

_src = inspect.getsource(_agent.send_message)
check("applescript reads argv, not interpolated text", "on run argv" in _src, True)
check("message is not embedded in the script body",
      "json.dumps(text)" not in _src, True)
check("message passed as a subprocess argument",
      "script, text, to]" in _src, True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
