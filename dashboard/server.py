#!/usr/bin/env python3
"""
mediatool dashboard - one page for both transcoders. Python standard library only.

It holds the agents' tokens and the login, serves the page, and passes the page's requests on to the
agents. It never touches media files itself. LAN and Tailscale only: never put it on the Cloudflare tunnel.

Environment:
  MEDIATOOL_PASSWORD_HASH  from:  python /app/server.py hash   (asks for the password twice)
  MEDIATOOL_AGENTS         name=url=token, comma separated, e.g.
                           transcoder2=http://10.10.10.54:8770=<token>,transcoder=http://10.10.10.56:8770=<token>
  MEDIATOOL_PORT           default 8780
"""
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
COOKIE = "mediatool_session"
SESSION_DAYS = 30
ITERATIONS = 600_000
STATIC = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/icon.svg": "icon.svg"}
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
# What the page may ask an agent for: (method, pattern on the path after /api/).
ALLOWED = [
    ("GET", r"status"), ("GET", r"jobs"), ("GET", r"jobs/[\w-]+"), ("POST", r"jobs"), ("POST", r"jobs/[\w-]+/stop"),
    ("GET", r"plans"), ("GET", r"plans/plan-[\w.-]+\.json"), ("GET", r"undo"), ("GET", r"undo/undo-[\w.-]+\.json"),
    ("GET", r"ledger"), ("GET", r"browse"), ("GET", r"audit"), ("GET", r"update"), ("POST", r"update"), ("POST", r"leave"),
]


# ------------------------------------------------------------------ password --

def make_hash(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"pbkdf2:{iterations}:{b64(salt)}:{b64(digest)}"


def check_password(password, stored):
    try:
        kind, iterations, salt, digest = stored.split(":")
        if kind != "pbkdf2":
            return False
        unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), unb64(salt), int(iterations))
        return hmac.compare_digest(got, unb64(digest))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ settings --

def parse_agents(text):
    agents = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("=")
        if len(bits) != 3 or not all(bits):
            raise ValueError(f"MEDIATOOL_AGENTS: '{part.split('=')[0]}' should look like name=http://host:8770=token")
        name, url, token = (b.strip() for b in bits)
        if not re.fullmatch(r"[\w-]{1,40}", name):
            raise ValueError(f"MEDIATOOL_AGENTS: agent name '{name}' may only use letters, digits, - and _")
        if not re.fullmatch(r"https?://[\w.:\[\]-]+/?", url):
            raise ValueError(f"MEDIATOOL_AGENTS: '{name}' needs an address like http://10.10.10.54:8770")
        agents[name] = {"url": url.rstrip("/"), "token": token}
    if not agents:
        raise ValueError("MEDIATOOL_AGENTS is empty")
    return agents


def load_config(env=None):
    env = os.environ if env is None else env
    stored = env.get("MEDIATOOL_PASSWORD_HASH", "")
    if not stored.startswith("pbkdf2:"):
        sys.exit("MEDIATOOL_PASSWORD_HASH is missing. Make one with:  python /app/server.py hash")
    try:
        agents = parse_agents(env.get("MEDIATOOL_AGENTS"))
    except ValueError as exc:
        sys.exit(str(exc))
    return {"password_hash": stored, "agents": agents, "port": int(env.get("MEDIATOOL_PORT", 8780)),
            # Sessions are signed with a key made from the password hash: changing the password signs everyone out.
            "key": hashlib.sha256(b"mediatool-session:" + stored.encode()).digest()}


# ------------------------------------------------------------------ sessions --

def make_session(cfg, now=None):
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(cfg["key"], str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def session_ok(cfg, value, now=None):
    try:
        expires, sig = value.split(".")
        good = hmac.new(cfg["key"], expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(expires) > (now or time.time())
    except (ValueError, AttributeError):
        return False


class Throttle:
    """Slows down password guessing: after 5 wrong tries from one address, one try per 30 seconds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = {}

    def wait_for(self, ip):
        with self.lock:
            n, last = self.fails.get(ip, (0, 0))
            if n < 5:
                return 0
            return max(0, int(last + 30 - time.time()))

    def failed(self, ip):
        with self.lock:
            n, _ = self.fails.get(ip, (0, 0))
            self.fails[ip] = (n + 1, time.time())

    def cleared(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


# -------------------------------------------------------------------- agents --

def agent_call(agent, method, path, query="", body=None, timeout=15):
    """(status, bytes) from an agent. An unreachable agent gives 502 with a JSON reason."""
    url = f"{agent['url']}/api/{path}" + (f"?{query}" if query else "")
    data = body if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {agent['token']}", "Accept": "application/json"})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return 502, json.dumps({"error": f"agent not reachable: {reason}", "offline": True}).encode()


def all_status(cfg):
    def one(item):
        name, agent = item
        code, raw = agent_call(agent, "GET", "status", timeout=4)
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"error": "bad answer from the agent"}
        if code == 401:
            data = {"error": "the agent refused the token: check MEDIATOOL_AGENTS"}
        return {"name": name, "online": code == 200, **({"status": data} if code == 200 else data)}
    with ThreadPoolExecutor(max_workers=max(1, len(cfg["agents"]))) as pool:
        return list(pool.map(one, cfg["agents"].items()))


# ---------------------------------------------------------------------- HTTP --

def make_handler(cfg, throttle):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mediatool"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, ctype="application/json", headers=()):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def client_ip(self):
            return self.client_address[0]

        def logged_in(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and session_ok(cfg, v):
                    return True
            return False

        def same_site(self):
            """POSTs must come from this page: same Origin, and a header plain forms cannot send."""
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return False
            return self.headers.get("X-Mediatool") == "1"

        def body(self, limit=65536):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(length, limit)) if length else b""

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            if u.path in STATIC:
                f = WEB / STATIC[u.path]
                return self.send(200, f.read_bytes(), TYPES[f.suffix])
            if u.path == "/api/session":
                return self.send(200, {"logged_in": self.logged_in()})
            if not self.logged_in():
                return self.send(401, {"error": "log in first"})
            if u.path == "/api/agents":
                return self.send(200, {"agents": all_status(cfg),
                                       "dashboard": {"version": os.environ.get("MEDIATOOL_VERSION", "dev")}})
            return self.proxy("GET", u)

        def do_POST(self):
            u = urlparse(self.path)
            if not self.same_site():
                return self.send(403, {"error": "refused: not sent from the mediatool page"})
            if u.path == "/api/login":
                wait = throttle.wait_for(self.client_ip())
                if wait:
                    return self.send(429, {"error": f"too many wrong passwords; try again in {wait} s"})
                try:
                    password = json.loads(self.body() or b"{}").get("password", "")
                except (ValueError, AttributeError):
                    password = ""
                if not isinstance(password, str) or not check_password(password, cfg["password_hash"]):
                    throttle.failed(self.client_ip())
                    time.sleep(1)
                    return self.send(401, {"error": "wrong password"})
                throttle.cleared(self.client_ip())
                cookie = (f"{COOKIE}={make_session(cfg)}; Path=/; HttpOnly; SameSite=Strict; "
                          f"Max-Age={SESSION_DAYS * 86400}")
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", cookie)])
            if u.path == "/api/logout":
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")])
            if not self.logged_in():
                return self.send(401, {"error": "log in first"})
            return self.proxy("POST", u)

        def proxy(self, method, u):
            m = re.fullmatch(r"/api/agents/([\w-]+)/(.+)", u.path)
            if not m or m.group(1) not in cfg["agents"]:
                return self.send(404, {"error": "not found"})
            rest = m.group(2)
            if not any(method == am and re.fullmatch(p, rest) for am, p in ALLOWED):
                return self.send(404, {"error": "not found"})
            body = self.body() if method == "POST" else None
            code, raw = agent_call(cfg["agents"][m.group(1)], method, quote(rest, safe="/"), u.query, body)
            if code == 401:
                code, raw = 502, json.dumps({"error": "the agent refused the token: check MEDIATOOL_AGENTS"}).encode()
            return self.send(code, raw)

    return Handler


def serve(cfg):
    server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(cfg, Throttle()))
    server.daemon_threads = True
    print(f"mediatool dashboard on port {cfg['port']}, agents: {', '.join(cfg['agents'])}", flush=True)
    server.serve_forever()


def hash_command():
    first = getpass.getpass("New dashboard password: ")
    if len(first) < 10:
        sys.exit("Use at least 10 characters.")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two did not match.")
    print("\nPut this line in the Komodo stack's Environment box:\n")
    print(f"MEDIATOOL_PASSWORD_HASH={make_hash(first)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["hash"]:
        hash_command()
    else:
        serve(load_config())
