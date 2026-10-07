"""OAuth 2 sign-in for the cloud drives (Google Drive, Dropbox, OneDrive), the way desktop apps do it:
the browser opens the provider's sign-in page, the provider sends the browser back to a tiny web server
on this computer (http://127.0.0.1:53682/), and the app swaps the code it got for tokens (with PKCE, so
an intercepted code is useless).

Each provider needs an "app" registered by whoever ships BlamixFiles (a client id, and for Google a
client secret that isn't really secret for desktop apps). In order, they come from: the site (its
"App / client id" field), BLAMIXFILES_<PROVIDER>_CLIENT_ID / _CLIENT_SECRET, or the ids built into a
release (blamixfiles/_oauth_ids.py, written by the release workflow from repository secrets).
Tokens are kept in the site, inside the encrypted vault; a refreshed token is saved back through
TOKEN_SAVER.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

REDIRECT_PORT = 53682


@dataclass
class Provider:
    key: str
    label: str
    auth_url: str
    token_url: str
    scope: str
    extra: dict
    needs_secret: bool = False
    console: str = ""                 # where to register an app

    def endpoint(self, which: str) -> str:
        """Tests point these at a fake server (BLAMIXFILES_OAUTH_GDRIVE_TOKEN …)."""
        default = self.auth_url if which == "auth" else self.token_url
        return os.environ.get(f"BLAMIXFILES_OAUTH_{self.key.upper()}_{which.upper()}", default)


PROVIDERS = {
    "gdrive": Provider("gdrive", "Google Drive", "https://accounts.google.com/o/oauth2/v2/auth",
                       "https://oauth2.googleapis.com/token", "https://www.googleapis.com/auth/drive",
                       {"access_type": "offline", "prompt": "consent"}, needs_secret=True,
                       console="https://console.cloud.google.com/apis/credentials (OAuth client: Desktop app)"),
    "dropbox": Provider("dropbox", "Dropbox", "https://www.dropbox.com/oauth2/authorize",
                        "https://api.dropboxapi.com/oauth2/token", "",
                        {"token_access_type": "offline"},
                        console="https://www.dropbox.com/developers/apps (redirect URI http://127.0.0.1:53682/)"),
    "onedrive": Provider("onedrive", "OneDrive", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                         "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                         "Files.ReadWrite.All offline_access", {},
                         console="https://entra.microsoft.com > App registrations (public client, redirect "
                                 "http://localhost)"),
}

# set by the app / the CLI: (site_id, token_json) -> None, stores a refreshed token in the vault
TOKEN_SAVER: Callable[[str, str], None] | None = None
_save_lock = threading.Lock()


class OAuthError(Exception):
    pass


def builtin_ids(provider: str) -> tuple[str, str]:
    try:
        from .._oauth_ids import BUILTIN            # only in release builds
    except ImportError:
        return "", ""
    return tuple(BUILTIN.get(provider, ("", "")))  # type: ignore[return-value]


def client_for(site) -> tuple[str, str]:
    p = PROVIDERS[site.protocol]
    b_id, b_secret = builtin_ids(p.key)
    own = getattr(site, "oauth_client_id", "")
    cid = own or os.environ.get(f"BLAMIXFILES_{p.key.upper()}_CLIENT_ID", "") or b_id
    secret = (getattr(site, "oauth_client_secret", "")
              or os.environ.get(f"BLAMIXFILES_{p.key.upper()}_CLIENT_SECRET", "")
              or ("" if own else b_secret))
    if not cid:
        raise OAuthError(f"{p.label} needs an app id (client id). Register one at {p.console} and enter it "
                         "in the site, or set BLAMIXFILES_" + p.key.upper() + "_CLIENT_ID.")
    if p.needs_secret and not secret:
        raise OAuthError(f"{p.label} also needs the app's client secret (from the same page as the id).")
    return cid, secret


def _post_token(p: Provider, data: dict) -> dict:
    import httpx
    try:
        r = httpx.post(p.endpoint("token"), data=data, timeout=30, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError(f"Could not reach {p.label}: {e}") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        why = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise OAuthError(f"{p.label} refused the sign-in: {why}")
    return body


def _token_record(body: dict, old_refresh: str = "") -> dict:
    return {"access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or old_refresh,
            "expires_at": time.time() + float(body.get("expires_in", 3600)) - 60}


def authorize(provider: str, client_id: str, client_secret: str = "",
              open_browser: Callable[[str], object] | None = None, timeout: float = 300,
              port: int = REDIRECT_PORT) -> dict:
    """Run the browser sign-in; returns the token record. Blocks until the browser comes back (or timeout)."""
    p = PROVIDERS[provider]
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    redirect = f"http://127.0.0.1:{port}/" if provider != "onedrive" else f"http://localhost:{port}/"
    got: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got
            page = ("<h2>Signed in.</h2><p>You can close this tab and go back to BlamixFiles.</p>" if ok else
                    f"<h2>Sign-in didn't finish</h2><p>{got.get('error_description') or got.get('error', '')}</p>")
            body = f"<!doctype html><meta charset=utf-8><title>BlamixFiles</title><body style='font-family:sans-serif'>{page}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, *a):
            pass
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise OAuthError(f"Port {port} on this computer is busy (another sign-in still open?)") from None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", **p.extra}
    if p.scope:
        params["scope"] = p.scope
    url = p.endpoint("auth") + "?" + urllib.parse.urlencode(params)
    try:
        if open_browser is None:
            import webbrowser
            open_browser = webbrowser.open
        open_browser(url)
        if not done.wait(timeout):
            raise OAuthError("The sign-in wasn't finished in the browser in time")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in got:
        raise OAuthError(f"{p.label}: {got.get('error_description') or got.get('error') or 'sign-in cancelled'}")
    data = {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
            "client_id": client_id, "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    return _token_record(_post_token(p, data))


def refresh(provider: str, client_id: str, client_secret: str, refresh_token: str) -> dict:
    p = PROVIDERS[provider]
    if not refresh_token:
        raise OAuthError(f"Not signed in to {p.label}: edit the site and press Sign in")
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    if p.scope:
        data["scope"] = p.scope
    return _token_record(_post_token(p, data), refresh_token)


class TokenHolder:
    """A site's tokens: hands out a valid access token, refreshing (and saving) when it's about to run out."""

    def __init__(self, site):
        self.site = site
        self._lock = threading.Lock()
        try:
            self.token = json.loads(site.oauth_token) if site.oauth_token else {}
        except ValueError:
            self.token = {}

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not self.token.get("refresh_token") and not self.token.get("access_token"):
                raise OAuthError(f"Not signed in to {PROVIDERS[self.site.protocol].label}: edit the site and "
                                 "press Sign in")
            if force_refresh or time.time() >= float(self.token.get("expires_at", 0)):
                cid, secret = client_for(self.site)
                self.token = refresh(self.site.protocol, cid, secret, self.token.get("refresh_token", ""))
                self.site.oauth_token = json.dumps(self.token)
                if TOKEN_SAVER is not None and self.site.id:
                    with _save_lock:
                        try:
                            TOKEN_SAVER(self.site.id, self.site.oauth_token)
                        except Exception:  # noqa: BLE001 (saving is best effort; the token works anyway)
                            pass
            return self.token["access_token"]
