"""Offline tests. No network, no credentials.

    python tests/run_tests.py

Covers the logic most likely to break silently: name matching across the three
different formats the sources use, dollar formatting, and the filter rules.
"""
from __future__ import annotations

import sys
from datetime import date
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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
