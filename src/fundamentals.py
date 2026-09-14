"""P/E and earnings timing from Nasdaq's public EPS endpoint.

No API key anywhere in this project, so P/E is derived rather than fetched:
sum the last four reported quarters into a trailing-twelve-month EPS and
divide the price by it.

Two things this deliberately does NOT do:
  * invent a P/E for a loss-making company -- a negative P/E is not "cheap",
    it is meaningless, and it is reported as a loss instead
  * use forward consensus EPS, which is an analyst forecast rather than a
    fact. Trailing earnings actually happened.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

EPS_URL = "https://api.nasdaq.com/api/quote/{sym}/eps"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")
# Earnings are quarterly; a week-old figure is still current.
CACHE_DAYS = 7


class Fundamentals:
    def __init__(self, cache_path: Path):
        self.path = Path(cache_path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("fundamentals cache unreadable, starting fresh")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BROWSER_UA,
                                     "Accept": "application/json"})

    def _fresh(self, entry: dict) -> bool:
        try:
            got = date.fromisoformat(entry.get("fetched", "2000-01-01"))
        except ValueError:
            return False
        return (date.today() - got).days < CACHE_DAYS

    def get(self, symbol: str) -> dict:
        symbol = (symbol or "").upper().strip()
        if not symbol:
            return {}
        entry = self.data.get(symbol)
        if entry and self._fresh(entry):
            return entry

        out = {"fetched": date.today().isoformat(), "ttm_eps": None,
               "next_earnings": None}
        try:
            r = self.session.get(EPS_URL.format(sym=symbol), timeout=20)
            rows = ((r.json().get("data") or {}).get("earningsPerShare")) or []
        except (requests.RequestException, ValueError) as e:
            log.debug("eps fetch %s: %s", symbol, e)
            rows = []

        reported = [x for x in rows if x.get("type") == "PreviousQuarter"]
        vals = []
        for x in reported[-4:]:
            try:
                vals.append(float(x.get("earnings")))
            except (TypeError, ValueError):
                pass
        if len(vals) == 4:
            out["ttm_eps"] = round(sum(vals), 4)

        upcoming = [x for x in rows if x.get("type") == "UpcomingQuarter"]
        if upcoming:
            out["next_earnings"] = (upcoming[0].get("period") or "").strip()

        self.data[symbol] = out
        return out

    def pe(self, symbol: str, price: float) -> tuple[float | None, str]:
        """(P/E, note). Loss-makers return (None, 'loss') rather than a
        negative number pretending to be a valuation."""
        f = self.get(symbol)
        eps = f.get("ttm_eps")
        if eps is None:
            return None, "no data"
        if eps <= 0:
            return None, "loss-making"
        return round(price / eps, 1), ""

    def earnings_within(self, symbol: str, days: int = 21) -> str | None:
        """Flag an earnings report inside the window.

        In a 13-week competition an earnings date is the single biggest
        single-day risk a position carries, and it is knowable in advance.
        """
        period = self.get(symbol).get("next_earnings")
        if not period:
            return None
        for fmt in ("%b %Y", "%B %Y"):
            try:
                when = datetime.strptime(period, fmt).date()
                break
            except ValueError:
                continue
        else:
            return None
        # Month-granularity only, so treat mid-month as the estimate.
        est = when.replace(day=15)
        delta = (est - date.today()).days
        return period if 0 <= delta <= days + 15 else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
        tmp.replace(self.path)
