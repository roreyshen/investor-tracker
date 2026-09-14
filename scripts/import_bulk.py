#!/usr/bin/env python3
"""Import SEC bulk insider-transaction datasets.

Fetching Form 4s one at a time gave 84 open-market buys. The SEC publishes the
same data quarterly in bulk: one ~14MB zip holds ~104,000 transactions, of
which ~5,900 are genuine open-market purchases. Nine quarters is a few minutes
of downloading instead of seven hours of polling, and it is the difference
between a backtest that can conclude something and one that cannot.

    python scripts/import_bulk.py --quarters 8
    python scripts/import_bulk.py --quarters 12 --min-sell 5000000
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import TRADES_PATH  # noqa: E402
from src.models import Trade  # noqa: E402

log = logging.getLogger("import_bulk")

URL = ("https://www.sec.gov/files/structureddata/data/"
       "insider-transactions-data-sets/{q}_form345.zip")
UA = {"User-Agent": "Rorey Shen roreyshen@gmail.com"}

# Sanity bounds. The raw files contain occasional malformed rows -- share
# counts in the billions, prices of zero -- and a single bad row can dominate
# any dollar-weighted statistic.
MAX_SANE_VALUE = 5_000_000_000
MAX_SANE_PRICE = 1_000_000

SENIOR_HINTS = ("chief", "president", "chairman", "ceo", "cfo", "coo")


def quarters_back(n: int) -> list[str]:
    """Most recent n published quarters, newest first.

    The current quarter is never available -- the SEC publishes a few weeks
    after it closes -- so start from the previous one.
    """
    today = date.today()
    q = (today.month - 1) // 3 + 1
    y = today.year
    out = []
    for _ in range(n + 1):
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        out.append(f"{y}q{q}")
    return out[:n + 1]


def _f(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _d(v):
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        yield from csv.DictReader(text, delimiter="\t")


def load_quarter(q: str, min_sell: float, attempts: int = 3) -> list[Trade]:
    # These are ~14MB downloads and the connection does get reset. A silently
    # skipped quarter is a hole in the history that nothing later reveals, so
    # retry rather than log-and-continue.
    content = None
    for n in range(attempts):
        try:
            r = requests.get(URL.format(q=q), headers=UA, timeout=240)
            if r.status_code != 200:
                log.warning("%s unavailable (%s) -- likely not published yet",
                            q, r.status_code)
                return []
            content = r.content
            break
        except requests.RequestException as e:
            log.warning("%s attempt %d failed: %s", q, n + 1, e)
            time.sleep(3 * (n + 1))
    if content is None:
        log.error("%s: giving up after %d attempts -- HISTORY GAP", q, attempts)
        return []
    zf = zipfile.ZipFile(io.BytesIO(content))

    subs = {}
    for row in _rows(zf, "SUBMISSION.tsv"):
        acc = row["ACCESSION_NUMBER"]
        # A 10b5-1 trade was scheduled months in advance under a written plan,
        # so it reflects no opinion about the stock on the day it executes.
        # Treating it the same as a discretionary purchase dilutes the signal.
        plan = (row.get("AFF10B5ONE") or "").strip().lower() in ("1", "true", "y")
        subs[acc] = (row.get("ISSUERNAME", ""), row.get("ISSUERTRADINGSYMBOL", ""),
                     _d(row.get("FILING_DATE")), plan)

    owners = {}
    for row in _rows(zf, "REPORTINGOWNER.tsv"):
        acc = row["ACCESSION_NUMBER"]
        if acc in owners:
            continue        # first named owner is enough for attribution
        rel = (row.get("RPTOWNER_RELATIONSHIP") or "").strip()
        title = (row.get("RPTOWNER_TITLE") or "").strip()
        owners[acc] = (row.get("RPTOWNERNAME", "").strip(), title or rel)

    trades: list[Trade] = []
    for i, row in enumerate(_rows(zf, "NONDERIV_TRANS.tsv")):
        code = (row.get("TRANS_CODE") or "").strip()
        if code not in ("P", "S"):
            continue
        acc = row["ACCESSION_NUMBER"]
        issuer, ticker, filed, planned = subs.get(acc, ("", "", None, False))
        if not ticker or not filed:
            continue

        shares = _f(row.get("TRANS_SHARES"))
        price = _f(row.get("TRANS_PRICEPERSHARE"))
        if not shares or not price or price <= 0 or price > MAX_SANE_PRICE:
            continue
        value = shares * price
        if value <= 0 or value > MAX_SANE_VALUE:
            continue

        ad = (row.get("TRANS_ACQUIRED_DISP_CD") or "").strip()
        action = "BUY" if ad == "A" else "SELL" if ad == "D" else "OTHER"
        if action == "SELL" and value < min_sell:
            continue            # sells are lower signal and far more numerous
        if action == "OTHER":
            continue

        person, role = owners.get(acc, ("(unknown)", ""))
        trades.append(Trade(
            source="form4", uid=f"{acc}:{row.get('NONDERIV_TRANS_SK', i)}",
            person=person or issuer, role=role, action=action,
            company=issuer, ticker=ticker.strip().upper(),
            shares=shares, price=price, value_usd=value, code=code,
            trade_date=_d(row.get("TRANS_DATE")), filed_date=filed,
            note="10b5-1 plan (scheduled in advance)" if planned else "",
            url=f"https://www.sec.gov/Archives/edgar/data/{acc.replace('-', '')}",
        ))
    log.info("%s: %d usable transactions", q, len(trades))
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=8)
    ap.add_argument("--min-sell", type=float, default=1_000_000,
                    help="ignore sells below this (they dwarf buys in volume)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    all_trades: list[Trade] = []
    for q in quarters_back(args.quarters):
        try:
            all_trades.extend(load_quarter(q, args.min_sell))
        except (requests.RequestException, zipfile.BadZipFile, KeyError) as e:
            log.warning("%s failed: %s", q, e)

    if not all_trades:
        log.error("nothing imported")
        return 1

    buys = sum(1 for t in all_trades if t.action == "BUY")
    log.info("importing %d transactions (%d buys, %d sells)",
             len(all_trades), buys, len(all_trades) - buys)
    added = store.append(TRADES_PATH, all_trades, set())
    log.info("stored %d new", added)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
