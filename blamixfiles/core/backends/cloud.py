"""Shared plumbing for the cloud drives: an HTTP client that signs every request with the site's OAuth
token, refreshes it once on 401, waits and retries on 429 / 5xx "slow down" answers, and turns the
providers' error bodies into short messages."""
from __future__ import annotations

import contextlib
import os
import time

import httpx

from ..oauth import OAuthError, TokenHolder
from ..vfs import Backend, BackendError

CHUNK = 8 * 1024 * 1024


class CloudBackend(Backend):
    api_env = ""                       # env var that points the API at a test server
    api_default = ""

    def __init__(self, site):
        self.site = site
        self.tokens = TokenHolder(site)
        self.http: httpx.Client | None = None
        self.api = os.environ.get(self.api_env, self.api_default).rstrip("/") if self.api_env else self.api_default

    # ---- lifecycle
    def connect(self) -> None:
        self.http = httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True,
                                 headers={"User-Agent": "BlamixFiles"})
        try:
            self.tokens.access_token()
            self.check()
        except OAuthError as e:
            raise BackendError(str(e)) from None

    def check(self) -> None:
        """One cheap call to prove the login works (overridden)."""

    def close(self) -> None:
        if self.http is not None:
            self.http.close()
            self.http = None

    @property
    def connected(self) -> bool:
        return self.http is not None

    # ---- requests
    def request(self, method: str, url: str, ok=(200, 201, 202, 204, 206), **kw) -> httpx.Response:
        if self.http is None:
            raise BackendError("Not connected")
        if not url.startswith("http"):
            url = self.api + url
        base_headers = dict(kw.pop("headers", None) or {})
        refreshed = False
        r = None
        for attempt in range(5):
            try:
                headers = dict(base_headers, Authorization="Bearer " + self.tokens.access_token())
                r = self.http.request(method, url, headers=headers, **kw)
                if r.status_code == 401 and not refreshed:        # expired early / revoked: refresh once
                    refreshed = True
                    self.tokens.access_token(force_refresh=True)
                    continue
            except OAuthError as e:
                raise BackendError(str(e)) from None
            except httpx.TransportError as e:
                raise ConnectionError(str(e)) from None
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(min(30, float(r.headers.get("Retry-After") or 2 ** attempt)))
                continue
            if r.status_code not in ok:
                raise self.error(r)
            return r
        raise self.error(r)

    @contextlib.contextmanager
    def stream(self, method: str, url: str, **kw):
        """Like request(), for big downloads: the body is read in pieces (`r.iter_bytes()`)."""
        if self.http is None:
            raise BackendError("Not connected")
        if not url.startswith("http"):
            url = self.api + url
        base_headers = dict(kw.pop("headers", None) or {})
        for attempt in range(3):
            try:
                headers = dict(base_headers, Authorization="Bearer " + self.tokens.access_token(force_refresh=attempt > 0))
                r = self.http.send(self.http.build_request(method, url, headers=headers, **kw), stream=True)
            except OAuthError as e:
                raise BackendError(str(e)) from None
            except httpx.TransportError as e:
                raise ConnectionError(str(e)) from None
            if r.status_code == 401 and attempt == 0:
                r.close()
                continue
            try:
                if r.status_code not in (200, 206):
                    r.read()
                    raise self.error(r)
                yield r
            except httpx.TransportError as e:
                raise ConnectionError(str(e)) from None
            finally:
                r.close()
            return
        raise BackendError("Not signed in: sign in again")

    def error(self, r: httpx.Response) -> Exception:
        """A BackendError with the provider's own message (overridden for its error format)."""
        return BackendError(f"HTTP {r.status_code}: {r.text[:200]}")

    # ---- helpers
    @staticmethod
    def iso(ts: str) -> float:
        """'2024-05-01T10:20:30.123Z' -> unix time."""
        if not ts:
            return 0.0
        from datetime import datetime
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def to_iso(t: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
