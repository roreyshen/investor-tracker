#!/usr/bin/env python3
"""Check every watchlist entry actually matches somebody in the data.

A watchlist entry that never matches is the worst failure this project has:
it looks configured, logs nothing, and silently never fires. Two have already
slipped through -- "Tommy Tuberville" against "Tuberville, Thomas H", and
"Ro Khanna" against "Khanna, Rohit". Both were found by accident. This finds
them on purpose.

    python scripts/audit_watchlist.py
"""
from __future__ import annotations

import sys
from collections import Counter
from difflib import get_close_matches
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.config import TRADES_PATH, load_watchlist  # noqa: E402
from src.filters import Watchlist, name_tokens  # noqa: E402


def main() -> int:
    rows = store.load(TRADES_PATH)
    cfg = load_watchlist()
    wl = Watchlist(cfg)

    people = Counter(r["person"] for r in rows if r.get("person"))
    surnames: dict[str, list[str]] = {}
    for p in people:
        for tok in name_tokens(p):
            surnames.setdefault(tok, []).append(p)

    entries = []
    for group in ("congress", "insiders", "funds"):
        for item in (cfg.get(group) or []):
            n = item.get("name") if isinstance(item, dict) else str(item)
            if n:
                entries.append((group, n))

    matched, unmatched = [], []
    for group, name in entries:
        hits = [p for p in people if wl.match(p) == name]
        total = sum(people[p] for p in hits)
        (matched if hits else unmatched).append((group, name, len(hits), total))

    print(f"\n  Watchlist audit — {len(entries)} entries against "
          f"{len(people)} distinct filers, {sum(people.values()):,} trades\n")

    if matched:
        print("  MATCHING")
        for g, n, k, t in sorted(matched, key=lambda x: -x[3]):
            print(f"    {n:30} {g:9} {t:>6} trades  ({k} name form(s))")

    if unmatched:
        print("\n  NOT MATCHING ANYTHING — these will never alert")
        for g, n, _, _ in unmatched:
            # Suggest who they probably meant, via surname overlap.
            toks = name_tokens(n)
            cands: Counter = Counter()
            for tok in toks:
                for p in surnames.get(tok, []):
                    cands[p] += 1
            near = [p for p, _ in cands.most_common(3)]
            if not near:
                near = get_close_matches(n, list(people), n=3, cutoff=0.6)
            hint = f"  did you mean: {', '.join(near)}" if near else "  (no similar name in data)"
            print(f"    {n:30} {g:9}{hint}")
    else:
        print("\n  All entries match at least one filer.")

    print()
    return 1 if unmatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
