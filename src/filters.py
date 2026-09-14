"""Decide which trades are worth a notification.

Raw Form 4 volume is ~1,500 filings/day. Unfiltered that is hundreds of alerts
daily, which is the same as no alerts at all. Almost all of that volume is
compensation mechanics -- grants vesting, shares withheld for taxes, option
exercises -- not anyone choosing to buy.

The signal is code P: an insider spending their own money on the open market.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, timedelta

from .models import Trade

log = logging.getLogger(__name__)

# Dropped before name comparison so "Hon. Pelosi, Nancy Jr." == "Nancy Pelosi".
_NOISE_TOKENS = {
    "hon", "mr", "mrs", "ms", "dr", "rep", "sen", "senator", "representative",
    "jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "the", "of",
}


def name_tokens(name: str) -> set[str]:
    """Order-independent name key.

    EDGAR writes "AULT MILTON C III" (last first), Congress writes
    "Pelosi, Nancy", people write "Nancy Pelosi". Comparing token *sets*
    sidesteps the ordering problem entirely instead of guessing a convention.
    """
    cleaned = re.sub(r"[^a-z\s]", " ", name.lower())
    return {
        t for t in cleaned.split()
        if len(t) > 1 and t not in _NOISE_TOKENS
    }


class Watchlist:
    """Names that alert on every trade, at any size."""

    def __init__(self, cfg: dict):
        self.entries: list[tuple[set[str], str, str]] = []   # tokens, name, group
        self.ciks: dict[str, str] = {}
        self.tickers = {t.upper() for t in (cfg.get("tickers") or [])}

        for group in ("congress", "insiders", "funds"):
            for item in (cfg.get(group) or []):
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                if not name:
                    continue
                toks = name_tokens(name)
                if len(toks) >= 2:
                    self.entries.append((toks, name, group))
                else:
                    log.warning("watchlist entry %r is too vague to match safely "
                                "(needs first + last name), skipping", name)
                if isinstance(item, dict) and item.get("cik"):
                    self.ciks[str(item["cik"]).lstrip("0")] = name

    def match(self, person: str, cik: str = "") -> str | None:
        """Return the matched watchlist name, or None."""
        if cik and cik.lstrip("0") in self.ciks:
            return self.ciks[cik.lstrip("0")]
        toks = name_tokens(person)
        if not toks:
            return None
        for entry_toks, name, _group in self.entries:
            # Subset match: watchlist "Nancy Pelosi" matches a filing's
            # "PELOSI NANCY PATRICIA", but never the reverse-direction
            # false positive of a bare surname.
            if entry_toks <= toks:
                return name
        return None


def _is_open_market(trade: Trade) -> bool:
    return trade.code in ("P", "S")


def find_cluster_buys(trades: list[Trade], history: list[dict],
                      min_insiders: int, window_days: int) -> set[str]:
    """Tickers where several insiders bought inside the same window.

    Reads from persisted history, not just this run's batch -- a cluster
    almost always spans multiple days and therefore multiple polling runs.
    """
    cutoff = date.today() - timedelta(days=window_days)
    by_ticker: dict[str, set[str]] = defaultdict(set)

    for h in history:
        try:
            d = date.fromisoformat(h["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if d >= cutoff and h.get("ticker"):
            by_ticker[h["ticker"]].add(h.get("person", ""))

    for t in trades:
        if t.code == "P" and t.ticker and t.trade_date and t.trade_date >= cutoff:
            by_ticker[t.ticker].add(t.person)

    return {tk for tk, people in by_ticker.items() if len(people) >= min_insiders}


def evaluate(trades: list[Trade], settings: dict, watchlist: Watchlist,
             history: list[dict] | None = None) -> list[Trade]:
    """Attach alert reasons; return only the trades that earned one."""
    f = settings.get("filters", {})
    min_buy = f.get("min_insider_buy_usd", 1_000_000)
    min_sell = f.get("min_insider_sell_usd", 10_000_000)
    codes = set(f.get("signal_codes", ["P", "S"]))

    clusters = find_cluster_buys(
        trades, history or [],
        f.get("cluster_min_insiders", 3),
        f.get("cluster_window_days", 7),
    )

    alerts: list[Trade] = []
    for t in trades:
        reasons: list[str] = []

        matched = watchlist.match(t.person)
        if matched:
            reasons.append(f"watchlist: {matched}")

        if t.ticker and t.ticker.upper() in watchlist.tickers:
            reasons.append(f"watched ticker {t.ticker}")

        # Congressional filings carry ranges, not exact values, and have no
        # transaction code -- they bypass the dollar rules and rely on the
        # watchlist match above.
        if t.source in ("house", "senate"):
            pass
        elif t.code in codes and t.value_usd:
            if t.action == "BUY" and t.value_usd >= min_buy:
                reasons.append(f"large insider buy ({t.amount_str})")
            elif t.action == "SELL" and t.value_usd >= min_sell:
                reasons.append(f"large insider sell ({t.amount_str})")

        if t.code == "P" and t.ticker in clusters:
            reasons.append(f"cluster buy: 3+ insiders in {t.ticker}")

        if reasons:
            t.reasons = reasons
            alerts.append(t)

    # Biggest first, so a burst of alerts leads with what matters.
    alerts.sort(key=lambda x: x.value_usd or 0, reverse=True)
    log.info("filters: %d alerts from %d trades", len(alerts), len(trades))
    return alerts


def history_entries(trades: list[Trade]) -> list[dict]:
    """Compact records of open-market buys, for future cluster detection."""
    return [
        {"person": t.person, "ticker": t.ticker,
         "date": t.trade_date.isoformat() if t.trade_date else ""}
        for t in trades
        if t.code == "P" and t.ticker and t.trade_date
    ]
