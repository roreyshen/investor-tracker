"""Shared data shapes. Every source normalizes into a Trade."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


@dataclass
class Trade:
    """One disclosed transaction, normalized across all four sources."""

    source: str               # "form4" | "house" | "senate" | "13f"
    uid: str                  # stable dedupe key (accession + index, or DocID)
    person: str               # who traded
    action: str               # "BUY" | "SELL" | "OTHER"
    filed_date: Optional[date] = None
    trade_date: Optional[date] = None

    company: str = ""
    ticker: str = ""
    role: str = ""            # "CEO", "Director", "Rep. CA-11", ...

    shares: Optional[float] = None
    price: Optional[float] = None
    value_usd: Optional[float] = None   # exact, when known (Form 4)
    value_range: str = ""               # congressional ranges, e.g. "$1,001 - $15,000"

    code: str = ""            # Form 4 transaction code (P/S/A/F/M/G...)
    url: str = ""
    note: str = ""            # e.g. "scanned PDF, details not machine-readable"

    # Set by filters.py -- why this trade earned an alert.
    reasons: list[str] = field(default_factory=list)

    @property
    def lag_days(self) -> Optional[int]:
        """Days between the trade happening and it becoming public."""
        if self.trade_date and self.filed_date:
            return (self.filed_date - self.trade_date).days
        return None

    @property
    def amount_str(self) -> str:
        """Human dollar amount, exact or range depending on source."""
        if self.value_usd is not None:
            v = self.value_usd
            if v >= 1_000_000_000:
                b = round(v / 1_000_000_000, 1)
                return f"${b:.0f}B" if b == int(b) else f"${b:.1f}B"
            if v >= 1_000_000:
                m = round(v / 1_000_000, 1)
                return f"${m:.0f}M" if m == int(m) else f"${m:.1f}M"
            if v >= 1_000:
                return f"${v/1_000:.0f}K"
            return f"${v:,.0f}"
        return self.value_range or "amount undisclosed"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("filed_date", "trade_date"):
            d[k] = d[k].isoformat() if d[k] else None
        return d
