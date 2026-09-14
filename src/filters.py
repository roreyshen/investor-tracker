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


# Watchlists are written the way people are known ("Tommy Tuberville") while
# filings use the legal name ("Tuberville, Thomas H"). Without this map the
# entry silently never matches -- the worst kind of failure, because the
# watchlist looks configured and simply never fires.
_NICKNAMES = {
    "tommy": "thomas", "tom": "thomas", "thom": "thomas",
    "bill": "william", "billy": "william", "will": "william",
    "bob": "robert", "bobby": "robert", "rob": "robert",
    "dick": "richard", "rick": "richard", "ricky": "richard", "rich": "richard",
    "mike": "michael", "mickey": "michael",
    "jim": "james", "jimmy": "james",
    "joe": "joseph", "joey": "joseph",
    "dan": "daniel", "danny": "daniel",
    "dave": "david", "davey": "david",
    "chris": "christopher", "steve": "stephen", "steven": "stephen",
    "tony": "anthony", "andy": "andrew", "matt": "matthew",
    "greg": "gregory", "jeff": "jeffrey", "ken": "kenneth",
    "larry": "lawrence", "ron": "ronald", "sam": "samuel",
    "ben": "benjamin", "pete": "peter", "jerry": "gerald",
    "terry": "terrence", "doug": "douglas", "phil": "philip",
    "ed": "edward", "eddie": "edward", "ted": "edward",
    "nick": "nicholas", "tim": "timothy", "pat": "patrick",
    "marge": "marjorie", "debbie": "deborah", "deb": "deborah",
    "liz": "elizabeth", "beth": "elizabeth", "kathy": "katherine",
    "cathy": "katherine", "sue": "susan", "nan": "nancy",
    "alex": "alexander", "gil": "gilbert", "gus": "august",
    # Found by scripts/audit_watchlist.py against real filer names:
    "ro": "rohit",          # Rep. Ro Khanna files as "Khanna, Rohit"
    "jamie": "james",       # Jamie Dimon files as "DIMON JAMES"
    "tommy": "thomas", "thomas": "thomas",
    "mitch": "mitchell", "hal": "harold", "gene": "eugene",
    "art": "arthur", "bernie": "bernard", "charlie": "charles",
    "chuck": "charles", "frank": "francis", "hank": "henry",
    "jack": "john", "johnny": "john", "kate": "katherine",
    "maggie": "margaret", "peggy": "margaret", "sandy": "alexander",
    "vinny": "vincent", "vince": "vincent", "walt": "walter",
}


def name_tokens(name: str) -> set[str]:
    """Order-independent name key.

    EDGAR writes "AULT MILTON C III" (last first), Congress writes
    "Pelosi, Nancy", people write "Nancy Pelosi". Comparing token *sets*
    sidesteps the ordering problem entirely instead of guessing a convention.
    """
    cleaned = re.sub(r"[^a-z\s]", " ", name.lower())
    return {
        _NICKNAMES.get(t, t) for t in cleaned.split()
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
                # Aliases exist for name forms no nickname map can reach:
                # Jensen Huang files as "HUANG JEN HSUN", where "Jensen" is an
                # English name and "Jen-Hsun" the legal one. Without this the
                # entry silently matches nothing.
                forms = [name]
                if isinstance(item, dict):
                    forms += [a for a in (item.get("aliases") or []) if a]
                for form in forms:
                    toks = name_tokens(form)
                    if len(toks) >= 2:
                        self.entries.append((toks, name, group))
                    else:
                        log.warning("watchlist entry %r is too vague to match "
                                    "safely (needs first + last name), skipping", form)
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


_RANGE_BOTH_RE = re.compile(r"\$?([\d,]+)\s*-\s*\$?([\d,]+)")


def range_mid(value_range: str) -> float:
    """Midpoint of a disclosed band, for summing notional across trades.

    Congress discloses only a band, so any total is an estimate. The midpoint
    is the least-wrong single number; totals built from it are labelled as
    estimates wherever they're shown.
    """
    m = _RANGE_BOTH_RE.search(value_range or "")
    if not m:
        return 0.0
    try:
        lo, hi = (float(g.replace(",", "")) for g in m.groups())
    except ValueError:
        return 0.0
    return (lo + hi) / 2


def is_senior(role: str, titles: list[str]) -> bool:
    """True for C-suite and board-chair roles.

    Research consistently finds top-officer purchases more informative than
    those of junior insiders (Lakonishok & Lee 2002; Cohen, Malloy & Pomorski
    2012), so these clear a lower dollar bar.
    """
    low = (role or "").lower()
    return any(t.lower() in low for t in titles)


_RANGE_LOW_RE = re.compile(r"\$?([\d,]+)")


def range_low(value_range: str) -> float:
    """Bottom of a disclosed band. Congress never publishes an exact amount,
    so the conservative read is the smallest it could have been."""
    m = _RANGE_LOW_RE.search(value_range or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


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
    min_congress = f.get("min_congress_usd", 50_000)
    min_senior = f.get("min_senior_buy_usd", 250_000)
    min_144 = f.get("min_form144_usd", 5_000_000)
    senior_titles = f.get("senior_titles", ["CEO", "CFO", "President", "Chairman"])
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

        if t.source == "form144":
            if t.value_usd and t.value_usd >= min_144:
                reasons.append(f"insider intends to sell {t.amount_str} "
                               "(Form 144, filed before the sale)")
        elif t.source in ("house", "senate"):
            low = range_low(t.value_range)
            if low >= min_congress and t.action in ("BUY", "SELL"):
                reasons.append(f"large congressional trade ({t.amount_str})")
        elif t.code in codes and t.value_usd:
            senior = is_senior(t.role, senior_titles)
            floor = min(min_buy, min_senior) if senior else min_buy
            if t.action == "BUY" and t.value_usd >= floor:
                who = f"{t.role} " if senior else ""
                reasons.append(f"large insider buy ({who}{t.amount_str})".replace("  ", " "))
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
