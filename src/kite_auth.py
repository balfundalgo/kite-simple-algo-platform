"""
kite_auth.py
============
Kite Connect login (same flow as kite-sensex-fib-breakout).

  1. open kite.login_url() in the browser (user enters password + TOTP)
  2. Kite redirects to http://127.0.0.1:<port>/?request_token=...&status=success
  3. a tiny local server catches it, generate_session() swaps it for an
     access_token, which is valid until ~6 AM next day
  4. the token is cached in tokens/<account>.json and reused all day

The redirect URL registered on developers.kite.trade must match host:port
exactly, e.g. http://127.0.0.1:8765/
"""

import json
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

import config
from logger import logger

LOGIN_TIMEOUT_SEC = 300
_login_lock = threading.Lock()   # one browser login at a time (one port)


def _token_path(name: str):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name) or "account"
    return config.TOKEN_DIR / f"{safe}.json"


class _Catcher(BaseHTTPRequestHandler):
    request_token = None

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        token = q.get("request_token", [None])[0]
        status = q.get("status", [None])[0]
        if token and status == "success":
            _Catcher.request_token = token
            self.send_response(200)
            body = ("<html><body style='font-family:sans-serif;padding:40px'>"
                    "<h2>Kite login successful.</h2>"
                    "<p>You can close this tab and return to the app.</p></body></html>")
        else:
            self.send_response(400)
            body = "<h2>No request_token in this request.</h2>"
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


def _browser_login(kite: KiteConnect, host: str, port: int) -> str:
    _Catcher.request_token = None
    server = HTTPServer((host, port), _Catcher)
    server.timeout = 1
    url = kite.login_url()
    logger.info("Opening Kite login in the browser. If it does not open: %s", url)
    webbrowser.open(url)
    deadline = time.time() + LOGIN_TIMEOUT_SEC
    try:
        while _Catcher.request_token is None:
            if time.time() > deadline:
                raise TimeoutError("Kite login not completed within 5 minutes")
            server.handle_request()
    finally:
        server.server_close()
    return _Catcher.request_token


def login(account: dict, host: str, port: int, force: bool = False):
    """Returns (kite, profile). Reuses today's cached token unless force=True."""
    name = account["name"]
    api_key = account["api_key"].strip()
    api_secret = account["api_secret"].strip()
    if not api_key or not api_secret:
        raise ValueError(f"{name}: API key / secret missing")

    kite = KiteConnect(api_key=api_key)
    path = _token_path(name)

    if not force and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("api_key") == api_key:
                kite.set_access_token(cached["access_token"])
                profile = kite.profile()
                logger.info("%s: reusing today's session (%s)", name, profile["user_id"])
                return kite, profile
        except TokenException:
            logger.info("%s: saved token expired, fresh login needed", name)
        except Exception as e:
            logger.warning("%s: could not reuse saved token (%s)", name, e)

    with _login_lock:
        request_token = _browser_login(kite, host, port)
        session = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(session["access_token"])
    path.write_text(json.dumps({
        "api_key": api_key,
        "access_token": session["access_token"],
        "user_id": session.get("user_id"),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    profile = kite.profile()
    logger.info("%s: logged in as %s (%s)", name, profile["user_id"], profile.get("user_name", ""))
    return kite, profile
