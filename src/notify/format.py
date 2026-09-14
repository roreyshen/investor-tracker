"""Rendering trades for each channel."""
from __future__ import annotations

import re

from ..filters import _NOISE_TOKENS
from ..models import Trade

SMS_LIMIT = 160

# Officer titles are verbose ("President and Chief Executive Officer") and SMS
# has no room, so the common ones get collapsed to what people actually say.
_TITLE_ABBREV = [
    ("chief executive", "CEO"), ("chief financial", "CFO"),
    ("chief operating", "COO"), ("chief technology", "CTO"),
    ("chief accounting", "CAO"), ("chief legal", "CLO"),
    ("general counsel", "Counsel"), ("executive chairman", "Exec Chair"),
    ("chairman", "Chair"), ("president", "Pres"), ("director", "Director"),
    ("10% owner", "10% Owner"),
]

CODE_MEANING = {
    "P": "open-market buy", "S": "open-market sale", "A": "grant/award",
    "F": "tax withholding", "M": "option exercise", "G": "gift",
    "C": "conversion", "X": "option exercise",
}


def _emoji(t: Trade) -> str:
    return {"BUY": "\U0001F7E2", "SELL": "\U0001F534"}.get(t.action, "⚪")


# Form 4 filers are often companies, not people -- holding vehicles, trusts and
# funds that cross the 10% ownership line. "Control Empresarial de Capitales
# S.A." truncated to "Control" is meaningless, so entities keep more words.
_ENTITY_MARKERS = {
    "inc", "llc", "lp", "llp", "ltd", "corp", "corporation", "company",
    "co", "trust", "partners", "capital", "holdings", "group", "fund",
    "management", "associates", "ventures", "sa", "cv", "nv", "ag", "plc",
    "gmbh", "limited", "family", "foundation",
}


def _short_name(person: str) -> str:
    """'Hon. Pelosi, Nancy' -> 'Pelosi'. SMS has no room for full names."""
    base = person.split("(")[0].strip().rstrip(",")
    parts = [
        p for p in re.sub(r"[^A-Za-z\s]", " ", base).split()
        if len(p) > 1 and p.lower() not in _NOISE_TOKENS
    ]
    if not parts:
        return person[:12]
    # Marker words catch English entities; the token count catches the rest
    # (foreign-language vehicles like "Control Empresarial de Capitales").
    # People essentially never have 4+ name tokens once initials and suffixes
    # are stripped, so this direction of error is safe.
    if any(p.lower() in _ENTITY_MARKERS for p in parts) or len(parts) >= 4:
        return " ".join(parts[:3]).title()[:26]
    # Both EDGAR and the congressional clerks put the surname first.
    return parts[0].title()


def short_role(role: str) -> str:
    """Collapse a verbose officer title to a recognizable abbreviation."""
    low = role.lower()
    for needle, abbrev in _TITLE_ABBREV:
        if needle in low:
            return abbrev
    return role if len(role) <= 14 else role[:13].rsplit(" ", 1)[0]


_RANGE_RE = re.compile(r"\$?([\d,]+)\s*-\s*\$?([\d,]+)")


def _compact(n: float) -> str:
    # Round before the integer check: disclosure bands start at odd dollars
    # ($1,000,001), which would otherwise render as "$1.0M" instead of "$1M".
    if n >= 1_000_000:
        v = round(n / 1_000_000, 1)
        return f"${v:.0f}M" if v == int(v) else f"${v:.1f}M"
    if n >= 1_000:
        return f"${round(n/1_000):.0f}K"
    return f"${n:,.0f}"


def compact_range(text: str) -> str:
    """'$1,000,001 - $5,000,000' -> '$1M-5M'. Congressional filings only
    disclose bands, and the raw form eats half an SMS."""
    m = _RANGE_RE.search(text or "")
    if not m:
        return text
    try:
        lo, hi = (float(g.replace(",", "")) for g in m.groups())
    except ValueError:
        return text
    return f"{_compact(lo)}-{_compact(hi).lstrip('$')}"


def sms_line(t: Trade) -> str:
    """<=160 chars. Density matters more than grammar here."""
    amount = compact_range(t.amount_str) if t.value_range else t.amount_str
    bits = [_emoji(t), t.action, t.ticker or (t.company[:22] or "?"), amount]
    line = " ".join(b for b in bits if b)
    line += f" - {_short_name(t.person)}"
    if t.role:
        line += f" ({short_role(t.role)})"

    lag = t.lag_days
    if lag is not None and lag > 7:
        line += f" - traded {t.trade_date:%-m/%-d} ({lag}d ago)"
    elif t.trade_date:
        line += f" - {t.trade_date:%-m/%-d}"

    if t.note:
        line += f" - {t.note}"
    return line[:SMS_LIMIT]


def discord_embed(t: Trade) -> dict:
    color = {"BUY": 0x2ECC71, "SELL": 0xE74C3C}.get(t.action, 0x95A5A6)
    title = f"{_emoji(t)} {t.action} {t.ticker or t.company}"[:256]

    fields = [{"name": "Who", "value": t.person + (f"\n*{t.role}*" if t.role else ""),
               "inline": True},
              {"name": "Amount", "value": t.amount_str, "inline": True}]

    if t.shares and t.price:
        fields.append({"name": "Detail",
                       "value": f"{t.shares:,.0f} sh @ ${t.price:,.2f}",
                       "inline": True})
    if t.code:
        meaning = CODE_MEANING.get(t.code, "")
        fields.append({"name": "Type",
                       "value": f"`{t.code}`" + (f" {meaning}" if meaning else ""),
                       "inline": True})

    when = []
    if t.trade_date:
        when.append(f"Traded **{t.trade_date:%b %-d}**")
    if t.filed_date:
        when.append(f"Filed **{t.filed_date:%b %-d}**")
    lag = t.lag_days
    if lag is not None and lag > 7:
        # Surfaced prominently: a 40-day-old congressional trade should never
        # read like breaking news.
        when.append(f"⚠️ **{lag} days** between trade and disclosure")
    if when:
        fields.append({"name": "Timing", "value": " · ".join(when),
                       "inline": False})

    if t.source == "13f":
        # SMS can't carry this, so the caveat lives here where there is room.
        fields.append({
            "name": "\u26A0\uFE0F Context, not a signal",
            "value": ("13F is a quarterly snapshot filed up to 45 days after "
                      "quarter end, so this position may be months old and "
                      "already priced in. It shows only long US equity held at "
                      "quarter close \u2014 shorts, bonds, and anything opened "
                      "and closed inside the quarter are invisible."),
            "inline": False,
        })

    if t.reasons:
        fields.append({"name": "Why you're seeing this",
                       "value": "\n".join(f"• {r}" for r in t.reasons),
                       "inline": False})
    if t.note:
        fields.append({"name": "Note", "value": t.note, "inline": False})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"{t.company[:80]} · source: {t.source}"},
    }
    if t.url:
        embed["url"] = t.url
    return embed
