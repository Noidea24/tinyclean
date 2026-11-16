"""The dashboard: login, sessions, and passing only the allowed requests on to a real agent."""
import http.cookiejar
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import ROOT, SCRIPT

TOKEN = "a" * 32
PASSWORD = "correct horse battery"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dash():
    return load("dashboard_under_test", ROOT / "dashboard" / "server.py")


@pytest.fixture
def agent_url(tmp_path):
    agent = load("agent_for_dashboard", ROOT / "agent" / "agent.py")
    lib = tmp_path / "data" / "Movies" / "Film (2001)"
    lib.mkdir(parents=True)
    cfg = {"token": TOKEN, "roots": [(tmp_path / "data").resolve()], "name": "w1", "engine": SCRIPT,
           "env_file": "x", "plex": False, "state": tmp_path / "a", "engine_state": tmp_path / "e"}
    jobs = agent.Jobs(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), agent.make_handler(cfg, jobs, time.time()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "data"
    jobs.stop_all(wait=5)
    srv.shutdown()


@pytest.fixture
def site(dash, agent_url):
    url, data = agent_url
    cfg = dash.load_config({"MEDIATOOL_PASSWORD_HASH": dash.make_hash(PASSWORD, 1000),
                            "MEDIATOOL_AGENTS": f"w1={url}={TOKEN},off=http://127.0.0.1:9={TOKEN}"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.make_handler(cfg, dash.Throttle()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", data
    srv.shutdown()


class Browser:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, body=None, headers=None, page=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
            if page:
                req.add_header("X-Mediatool", "1")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=15) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw[:1] == b"{" else raw.decode()
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw[:1] == b"{" else raw.decode()

    def login(self):
        return self.go("/api/login", {"password": PASSWORD})


def test_password_hash(dash):
    stored = dash.make_hash("hunter2-hunter2", 1000)
    assert stored.startswith("pbkdf2:1000:") and "$" not in stored
    assert dash.check_password("hunter2-hunter2", stored)
    assert not dash.check_password("hunter3-hunter3", stored)
    assert not dash.check_password("x", "garbage")


def test_agents_setting(dash):
    agents = dash.parse_agents(f"t2=http://10.10.10.54:8770={TOKEN}, t1=http://10.10.10.56:8770/={TOKEN}")
    assert list(agents) == ["t2", "t1"] and agents["t1"]["url"] == "http://10.10.10.56:8770"
    for bad in ("t2=http://x:1", "t 2=http://x:1=tok", "t2=ftp://x=tok", ""):
        with pytest.raises(ValueError):
            dash.parse_agents(bad)


def test_sessions_expire_and_follow_the_password(dash):
    cfg = dash.load_config({"MEDIATOOL_PASSWORD_HASH": dash.make_hash("one-password", 1000),
                            "MEDIATOOL_AGENTS": f"w=http://h:1={TOKEN}"})
    s = dash.make_session(cfg)
    assert dash.session_ok(cfg, s)
    assert not dash.session_ok(cfg, s, now=time.time() + 31 * 86400)
    assert not dash.session_ok(cfg, s.replace(s[-1], "0" if s[-1] != "0" else "1"))
    other = dash.load_config({"MEDIATOOL_PASSWORD_HASH": dash.make_hash("other-password", 1000),
                              "MEDIATOOL_AGENTS": f"w=http://h:1={TOKEN}"})
    assert not dash.session_ok(other, s)


def test_page_is_public_but_data_needs_login(site):
    url, _ = site
    b = Browser(url)
    code, page = b.go("/")
    assert code == 200 and "mediatool" in page
    assert b.go("/api/session") == (200, {"logged_in": False})
    assert b.go("/api/agents")[0] == 401
    assert b.go("/api/agents/w1/status")[0] == 401
    assert b.go("/api/login", {"password": "nope"})[0] == 401
    assert b.login()[0] == 200
    assert b.go("/api/session") == (200, {"logged_in": True})


def test_login_and_jobs_need_the_page_header_and_origin(site):
    url, data = site
    b = Browser(url)
    assert b.go("/api/login", {"password": PASSWORD}, page=False)[0] == 403
    b.login()
    body = {"kind": "check", "args": {"paths": [str(data)]}}
    assert b.go("/api/agents/w1/jobs", body, headers={"Origin": "http://evil.example"})[0] == 403
    assert b.go("/api/agents/w1/jobs", body, page=False)[0] == 403


def test_status_of_both_workers_with_one_off(site):
    url, _ = site
    b = Browser(url)
    b.login()
    _, agents = b.go("/api/agents")
    by = {a["name"]: a for a in agents["agents"]}
    assert by["w1"]["online"] and by["w1"]["status"]["name"] == "w1"
    assert not by["off"]["online"] and by["off"]["offline"]


def test_requests_pass_through_to_the_agent(site):
    url, data = site
    b = Browser(url)
    b.login()
    code, folder = b.go("/api/agents/w1/browse?path=" + urllib.request.quote(str(data / "Movies")))
    assert code == 200 and [d["name"] for d in folder["dirs"]] == ["Film (2001)"]
    code, job = b.go("/api/agents/w1/jobs", {"kind": "check", "args": {"paths": [str(data)]}})
    assert code == 201
    for _ in range(100):
        _, j = b.go(f"/api/agents/w1/jobs/{job['id']}")
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "finished"
    code, err = b.go("/api/agents/w1/jobs", {"kind": "replace", "args": {"paths": [str(data)]}})
    assert code == 400 and "confirm" in err["error"]


def test_only_listed_requests_reach_an_agent(site):
    url, _ = site
    b = Browser(url)
    b.login()
    for path in ("/api/agents/w1/../status", "/api/agents/w1/plans/../../x", "/api/agents/w1/secret",
                 "/api/agents/nobody/status", "/api/agents/w1/plans/latest"):
        assert b.go(path)[0] == 404, path
    assert b.go("/api/agents/w1/status", {})[0] == 404


def test_password_guessing_is_slowed(dash):
    t = dash.Throttle()
    for _ in range(5):
        assert t.wait_for("1.2.3.4") == 0
        t.failed("1.2.3.4")
    assert t.wait_for("1.2.3.4") > 0 and t.wait_for("5.6.7.8") == 0
    t.cleared("1.2.3.4")
    assert t.wait_for("1.2.3.4") == 0
