"""Dedupe state, stored as JSON and committed back to the repo each run.

This is the whole database. It also doubles as the cron keep-alive: GitHub
disables scheduled workflows on repos with 60 days of no activity, and the
commit this file produces counts as activity.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Keep the file from growing without bound. Form 4 runs ~500 unique accessions
# a day, so this is roughly a 60-day memory -- far longer than any feed reaches
# back, which is all that dedupe actually requires.
MAX_SEEN_PER_SOURCE = 30_000


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"seen": {}, "last_run": None, "runs": 0}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("state unreadable (%s), starting fresh", e)
        self.data.setdefault("seen", {})

    def _bucket(self, source: str) -> dict:
        return self.data["seen"].setdefault(source, {})

    def is_seen(self, source: str, uid: str) -> bool:
        return uid in self._bucket(source)

    def mark_seen(self, source: str, uid: str) -> None:
        self._bucket(source)[uid] = int(datetime.now(timezone.utc).timestamp())

    def count(self, source: str) -> int:
        return len(self._bucket(source))

    def _prune(self) -> None:
        """Drop the oldest entries once a source's bucket exceeds the cap."""
        for source, bucket in self.data["seen"].items():
            if len(bucket) > MAX_SEEN_PER_SOURCE:
                keep = sorted(bucket.items(), key=lambda kv: kv[1],
                              reverse=True)[:MAX_SEEN_PER_SOURCE]
                self.data["seen"][source] = dict(keep)
                log.info("pruned %s state to %d entries", source, len(keep))

    def save(self) -> None:
        self._prune()
        self.data["last_run"] = datetime.now(timezone.utc).isoformat()
        self.data["runs"] = self.data.get("runs", 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)   # atomic: a killed run can't corrupt state
