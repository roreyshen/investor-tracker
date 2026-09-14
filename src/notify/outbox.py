"""A durable queue of alerts for the Mac agent to text.

GitHub Actions does the detection and writes here; the Mac reads this file and
sends the messages. The two are decoupled on purpose -- the Mac is asleep most
of the time, and alerts must survive that rather than being dropped.

The file is committed with the rest of the state, so the Mac only needs to
`git pull` to see new work. It never pushes, which keeps it out of the
commit race with the scheduled runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..models import Trade
from .format import sms_line

log = logging.getLogger(__name__)

# Bounded so a bad day can't grow the repo without limit. The Mac only ever
# cares about recent entries; anything older it would skip as stale anyway.
MAX_ENTRIES = 500


def append(path: Path, trades: list[Trade]) -> int:
    if not trades:
        return 0
    path = Path(path)
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text()).get("pending", [])
        except (json.JSONDecodeError, OSError) as e:
            log.warning("outbox unreadable (%s), starting fresh", e)

    known = {e.get("uid") for e in entries}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for t in trades:
        if t.uid in known:
            continue
        entries.append({
            "uid": t.uid,
            "queued_at": now,
            "text": sms_line(t),
            "source": t.source,
            "url": t.url,
        })
        added += 1

    entries = entries[-MAX_ENTRIES:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": entries}, indent=1))
    tmp.replace(path)
    log.info("outbox: queued %d alert(s) for the Mac agent", added)
    return added
