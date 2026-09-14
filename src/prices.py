"""Price and company data from Nasdaq's public API.

Chosen after testing the alternatives: Stooq now sits behind a JavaScript
proof-of-work challenge, and Yahoo rate-limits unauthenticated clients (and
blocks cloud IPs like GitHub's especially hard). Nasdaq's endpoint needs no
API key, serves full daily history, and returns an empty result set rather
than an error for unknown tickers.

Everything is cached on disk. Historical closes never change, so a date once
fetched is never fetched again -- only the recent window is refreshed.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")
HIST = "https://api.nasdaq.com/api/quote/{sym}/historical"
SCREENER = ("https://api.nasdaq.com/api/screener/stocks"
            "?tableonly=true&limit=25&download=true")

BENCHMARKS = {"SPY": "S&P 500", "DIA": "Dow Jones", "QQQ": "Nasdaq 100"}
# Under this closing price a stock is treated as a penny stock.
PENNY_MAX_PRICE = 5.0


def _money(s) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


class PriceStore:
    def __init__(self, cache_path: Path, per_second: float = 4.0):
        self.path = Path(cache_path)
        self.min_interval = 1.0 / per_second
        self._last = 0.0
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("price cache unreadable, starting fresh")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
        })

    def _wait(self) -> None:
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def _fetch(self, symbol: str, start: date, end: date) -> dict[str, float]:
        out: dict[str, float] = {}
        # Nasdaq splits its universe and a symbol only resolves under the right
        # assetclass, so both are tried. The winning class is remembered, which
        # halves the request count on every subsequent refresh.
        known = self.data.get(symbol, {}).get("asset")
        classes = (known,) if known else ("stocks", "etf")
        for asset in classes:
            self._wait()
            try:
                r = self.session.get(
                    HIST.format(sym=symbol.upper()),
                    params={"assetclass": asset,
                            "fromdate": start.isoformat(),
                            "todate": end.isoformat(),
                            "limit": 9999},
                    timeout=30)
                if r.status_code != 200:
                    continue
                rows = ((r.json().get("data") or {})
                        .get("tradesTable") or {}).get("rows") or []
            except (requests.RequestException, ValueError) as e:
                log.debug("price fetch %s (%s): %s", symbol, asset, e)
                continue
            for row in rows:
                try:
                    d = datetime.strptime(row["date"], "%m/%d/%Y").date()
                except (ValueError, KeyError):
                    continue
                close = _money(row.get("close"))
                if close is not None:
                    out[d.isoformat()] = close
            if out:
                self.data.setdefault(symbol, {})["asset"] = asset
                break
        return out

    def history(self, symbol: str, since: date) -> dict[str, float]:
        """Daily closes for a symbol, from cache plus whatever is missing."""
        symbol = (symbol or "").upper().strip()
        if not symbol:
            return {}
        entry = self.data.setdefault(symbol, {"closes": {}, "checked": None,
                                              "missing": False})
        if entry.get("missing") and not entry["closes"]:
            return {}

        closes = entry["closes"]
        today = date.today()
        need_backfill = not closes or min(closes) > since.isoformat()
        last_check = entry.get("checked")
        stale = last_check != today.isoformat()

        if need_backfill:
            fetched = self._fetch(symbol, since - timedelta(days=7), today)
            closes.update(fetched)
            entry["missing"] = not closes
        elif stale:
            # Only the recent tail can change; older closes are immutable.
            fetched = self._fetch(symbol, today - timedelta(days=10), today)
            closes.update(fetched)

        entry["checked"] = today.isoformat()
        return closes

    def close_on_or_after(self, symbol: str, when: date,
                          window: int = 10) -> float | None:
        """Close on `when`, or the next trading day within `window` days.

        Trades land on weekends and holidays, and disclosure dates don't always
        line up with sessions, so an exact-date lookup misses constantly.
        """
        closes = self.history(symbol, when)
        if not closes:
            return None
        for i in range(window + 1):
            key = (when + timedelta(days=i)).isoformat()
            if key in closes:
                return closes[key]
        return None

    def close_on_or_before(self, symbol: str, when: date,
                           window: int = 10) -> float | None:
        """Close on `when`, or the most recent trading day before it.

        Exits and mark-to-market must look BACKWARD. Using a forward lookup
        means a position near the end of a window finds no future close and
        silently marks at its entry price -- which reads as a flat 0.00%
        return rather than as missing data.
        """
        closes = self.history(symbol, when - timedelta(days=window + 5))
        if not closes:
            return None
        for i in range(window + 1):
            key = (when - timedelta(days=i)).isoformat()
            if key in closes:
                return closes[key]
        return None

    def latest(self, symbol: str, since: date | None = None) -> tuple[str, float] | None:
        closes = self.history(symbol, since or date.today() - timedelta(days=30))
        if not closes:
            return None
        key = max(closes)
        return key, closes[key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
        tmp.replace(self.path)


def fetch_universe(path: Path, max_age_days: int = 7) -> dict[str, dict]:
    """Ticker -> sector, industry, market cap, last price.

    One request covers ~7,000 listed companies, which is what makes the
    by-industry and penny-stock breakdowns possible without a paid data feed.
    """
    path = Path(path)
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            fetched = datetime.fromisoformat(cached.get("fetched", "2000-01-01"))
            if datetime.now() - fetched < timedelta(days=max_age_days):
                return cached.get("tickers", {})
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    try:
        r = requests.get(SCREENER, headers={"User-Agent": BROWSER_UA,
                                            "Accept": "application/json"},
                         timeout=60)
        rows = ((r.json().get("data") or {}).get("rows")) or []
    except (requests.RequestException, ValueError) as e:
        log.warning("universe fetch failed: %s", e)
        if path.exists():
            try:
                return json.loads(path.read_text()).get("tickers", {})
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    tickers = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        tickers[sym] = {
            "name": (row.get("name") or "").strip(),
            "sector": (row.get("sector") or "").strip() or "Unknown",
            "industry": (row.get("industry") or "").strip() or "Unknown",
            "market_cap": _money(row.get("marketCap")),
            "price": _money(row.get("lastsale")),
            "country": (row.get("country") or "").strip(),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": datetime.now().isoformat(),
                                "tickers": tickers}, separators=(",", ":")))
    log.info("universe: %d tickers", len(tickers))
    return tickers


def cap_tier(market_cap: float | None, price: float | None) -> str:
    if price is not None and price < PENNY_MAX_PRICE:
        return "Penny (<$5)"
    if market_cap is None:
        return "Unknown"
    if market_cap >= 200e9:
        return "Mega (>$200B)"
    if market_cap >= 10e9:
        return "Large ($10-200B)"
    if market_cap >= 2e9:
        return "Mid ($2-10B)"
    if market_cap >= 300e6:
        return "Small ($300M-2B)"
    return "Micro (<$300M)"
