from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    COOKIE_FILE,
    IMPERSONATE,
    MAX_RETRIES,
    REQUEST_JITTER,
    REQUEST_MIN_INTERVAL,
    REQUEST_TIMEOUT,
)

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover - import guard for environments without curl_cffi
    cffi_requests = None


class BlockedError(RuntimeError):
    """Raised when the edge (ESA) bot filter returns a 403 despite impersonation."""


class AuthRequiredError(RuntimeError):
    """Raised when the endpoint needs a login session we don't have yet."""


def _load_cookies() -> dict[str, str]:
    """Load cookies exported from a logged-in browser session.

    Accepts either a simple {name: value} map or the array format that most
    'Cookie-Editor' style extensions export.
    """
    path = Path(COOKIE_FILE)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    jar: dict[str, str] = {}
    for item in raw:
        name = item.get("name")
        value = item.get("value")
        if name is not None and value is not None:
            jar[str(name)] = str(value)
    return jar


class Fetcher:
    """Thin browser-impersonating HTTP client with a global politeness throttle.

    All requests share one lock so we never fire two hits back-to-back at the
    target, which is what protects the merchant account from风控/rate limits.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._cookies = _load_cookies()

    def reload_cookies(self) -> int:
        self._cookies = _load_cookies()
        return len(self._cookies)

    @property
    def has_session(self) -> bool:
        return bool(self._cookies)

    def _throttle(self) -> None:
        with self._lock:
            wait = REQUEST_MIN_INTERVAL + random.random() * REQUEST_JITTER
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < wait:
                time.sleep(wait - elapsed)
            self._last_request_at = time.monotonic()

    @retry(
        retry=retry_if_exception_type(BlockedError),
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        reraise=True,
    )
    def get(self, url: str, params: Optional[dict[str, Any]] = None, referer: Optional[str] = None) -> Any:
        if cffi_requests is None:
            raise RuntimeError(
                "curl_cffi is not installed. Run: pip install -r crawler/requirements.txt"
            )
        self._throttle()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        resp = cffi_requests.get(
            url,
            params=params,
            headers=headers,
            cookies=self._cookies,
            impersonate=IMPERSONATE,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 403:
            raise BlockedError(f"403 blocked by edge bot filter for {url}")
        if resp.status_code in (401, 302) or "/login" in str(resp.url):
            raise AuthRequiredError(f"login session required for {url}")
        resp.raise_for_status()
        return resp
