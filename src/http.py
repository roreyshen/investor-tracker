"""Polite, rate-limited HTTP.

The SEC publishes a hard rule: declare a real User-Agent with contact info and
stay under 10 requests/second. They ban IPs that ignore it, so the limiter here
is not optional politeness -- it keeps the bot alive.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)


class RateLimiter:
    """Token-free, dead-simple spacing limiter. Thread-safe."""

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            sleep_for = self.min_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


class Fetcher:
    """requests.Session + rate limiting + retry with exponential backoff."""

    def __init__(self, user_agent: str, per_second: float = 5,
                 timeout: int = 30, max_retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self.limiter = RateLimiter(per_second)
        self.timeout = timeout
        self.max_retries = max_retries

    def get(self, url: str, *, headers: dict | None = None,
            allow_404: bool = False, **kw) -> requests.Response | None:
        last_err = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                r = self.session.get(url, timeout=self.timeout,
                                     headers=headers, **kw)
                if r.status_code == 404 and allow_404:
                    return None
                # 429/503 mean we are being throttled -- back off hard.
                if r.status_code in (429, 503):
                    wait = 2 ** attempt * 5
                    log.warning("throttled (%s) on %s, backing off %ss",
                                r.status_code, url, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        log.error("giving up on %s: %s", url, last_err)
        return None

    def post(self, url: str, *, data=None, headers: dict | None = None,
             **kw) -> requests.Response | None:
        last_err = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                r = self.session.post(url, data=data, headers=headers,
                                      timeout=self.timeout, **kw)
                if r.status_code in (429, 503):
                    time.sleep(2 ** attempt * 5)
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        log.error("POST failed %s: %s", url, last_err)
        return None

    def get_text(self, url: str, **kw) -> str | None:
        r = self.get(url, **kw)
        return r.text if r is not None else None

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            log.error("bad JSON from %s", url)
            return None
