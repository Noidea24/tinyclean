"""The mediatool agent: logins, which jobs it will start, and following a job's output and progress."""
import base64
import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import ROOT, SCRIPT

TOKEN = "t" * 32


@pytest.fixture
def agent_mod():
    spec = importlib.util.spec_from_file_location("agent_under_test", ROOT / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "data" / "Movies" / "Film (2001)"
    lib.mkdir(parents=True)
    (lib / "Film.mkv").write_bytes(b"not really a video")
    return tmp_path / "data"


@pytest.fixture
def cfg(tmp_path, library):
    return {"token": TOKEN, "port": 0, "bind": "127.0.0.1", "roots": [library.resolve()], "name": "test-worker",
            "engine": SCRIPT, "env_file": str(tmp_path / "mediatool.env"), "plex": False,
            "state": tmp_path / "agent-state", "engine_state": tmp_path / "engine-state"}


FAKE_ENGINE = """
import json, signal, sys, time
signal.signal(signal.SIGINT, lambda *a: (print("stopping cleanly", flush=True), sys.exit(3)))
print("args:", " ".join(sys.argv[1:]), flush=True)
for i in range(1, 4):
    print("@progress " + json.dumps({"files_total": 3, "files_finished": i, "active": []}), flush=True)
    print(f"file {i} done", flush=True)
    time.sleep(0.05)
if "--slow" in sys.argv or any("Slow" in a for a in sys.argv):
    time.sleep(30)
print("all done", flush=True)
"""


@pytest.fixture
def server(agent_mod, cfg):
    started = []

    def start(**overrides):
        c = {**cfg, **overrides}
        jobs = agent_mod.Jobs(c)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), agent_mod.make_handler(c, jobs, time.time()))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append((srv, jobs))
        return f"http://127.0.0.1:{srv.server_address[1]}", jobs
    yield start
    for srv, jobs in started:
        jobs.stop_all(wait=5)
        srv.shutdown()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(url, body=None, auth=f"Bearer {TOKEN}", headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with OPENER.open(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw[:1] == b"{" else raw.decode()


def wait_done(url, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        _, job = call(f"{url}/api/jobs/{job_id}")
        if job["state"] not in ("running", "stopping"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# --------------------------------------------------------------------- login --

def test_every_page_needs_the_token(server):
    url, _ = server()
    assert call(f"{url}/healthz", auth=None) == (200, {"ok": True})
    for path in ("/", "/api/status", "/api/jobs"):
        assert call(url + path, auth=None)[0] == 401
        assert call(url + path, auth="Bearer wrong-token-wrong-token-xx")[0] == 401
    assert call(f"{url}/api/status")[0] == 200


def test_browser_login_uses_the_token_as_password(server):
    url, _ = server()
    basic = "Basic " + base64.b64encode(f"anyone:{TOKEN}".encode()).decode()
    code, page = call(url + "/", auth=basic)
    assert code == 200 and "mediatool agent" in page
    wrong = "Basic " + base64.b64encode(f"{TOKEN}:nope".encode()).decode()
    assert call(url + "/", auth=wrong)[0] == 401


def test_jobs_cannot_be_started_from_another_site_or_by_form(server, library):
    url, _ = server()
    body = {"kind": "check", "args": {"paths": [str(library)]}}
    code, _ = call(f"{url}/api/jobs", body, headers={"Origin": "http://evil.example"})
    assert code == 403
    req = urllib.request.Request(f"{url}/api/jobs", data=b"kind=check", method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        OPENER.open(req, timeout=10)
    assert e.value.code == 415


def test_a_short_token_is_refused(agent_mod, tmp_path, monkeypatch):
    env = tmp_path / "mediatool.env"
    env.write_text("AGENT_TOKEN=short\n")
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="AGENT_TOKEN"):
        agent_mod.load_config(str(env))
    env.write_text(f"AGENT_TOKEN={TOKEN}\nAGENT_ROOTS=/data,/media\nPLEX_URL=http://plex\nPLEX_TOKEN=x\n")
    cfg = agent_mod.load_config(str(env))
    assert cfg["token"] == TOKEN and [str(r) for r in cfg["roots"]] == ["/data", "/media"] and cfg["plex"]


# ------------------------------------------------------- what may be started --

def test_job_options_are_built_from_a_fixed_list(agent_mod, cfg, library):
    film = str(library / "Movies" / "Film (2001)")
    argv = agent_mod.build_argv("convert", {"paths": [film], "profile": "live", "jobs": 2, "folders": 3,
                                            "dry_run": True}, cfg)
    assert argv == ["--profile", "live", "--quality", "standard", "--jobs", "2", "--progress-lines", "--folders", "3", "--dry-run",
                    "--", film]
    assert agent_mod.build_argv("convert", {"paths": [film], "profile": "auto"}, cfg)[:4] == \
        ["--quality", "standard", "--jobs", "1"]
    assert agent_mod.build_argv("convert", {"paths": [film], "profile": "live", "quality": "high"},
                                cfg)[:4] == ["--profile", "live", "--quality", "high"]
    with pytest.raises(agent_mod.BadRequest):
        agent_mod.set_profiles(cfg, {"profiles": {film: "cartoon"}})
    with pytest.raises(agent_mod.BadRequest):
        agent_mod.set_profiles(cfg, {"profiles": {"/etc": "live"}})
    assert agent_mod.build_argv("check", {"paths": [film]}, cfg) == ["--check", "--", film]
    assert agent_mod.build_argv("apply_plan", {"plan": "latest", "confirm": True}, cfg) == \
        ["--apply-plan", "latest", "--yes", "--env-file", cfg["env_file"]]
    assert agent_mod.build_argv("undo", {"log": "undo-20261115-220406.json", "confirm": True}, cfg)[1] == \
        str(cfg["engine_state"] / "undo" / "undo-20261115-220406.json")


@pytest.mark.parametrize("kind,args,message", [
    ("convert", {"paths": ["/etc"], "profile": "live"}, "outside the library"),
    ("convert", {"paths": ["{lib}/../.."], "profile": "live"}, "outside the library"),
    ("convert", {"paths": ["{lib}/Movies"], "profile": "fast"}, "profile must be"),
    ("convert", {"paths": ["{lib}/Movies"], "profile": "live", "quality": "ultra"}, "quality must be"),
    ("convert", {"paths": ["{lib}/Movies"], "profile": "live", "jobs": 9}, "between 1 and 4"),
    ("convert", {"paths": ["{lib}/nope"], "profile": "live"}, "does not exist"),
    ("replace", {"paths": ["{lib}/Movies"]}, "confirm=true"),
    ("apply_plan", {"plan": "../../etc/passwd", "confirm": True}, "file name"),
    ("organize", {"paths": ["{lib}/Movies"], "mode": "rename"}, "Plex is not set up"),
    ("rm", {}, "kind must be"),
])
def test_bad_requests_are_refused(agent_mod, cfg, library, kind, args, message):
    args = json.loads(json.dumps(args).replace("{lib}", str(library)))
    with pytest.raises(agent_mod.BadRequest, match=message):
        agent_mod.build_argv(kind, args, cfg)


def test_a_symlink_out_of_the_library_is_refused(agent_mod, cfg, library, tmp_path):
    (library / "escape").symlink_to(tmp_path)
    with pytest.raises(agent_mod.BadRequest, match="outside"):
        agent_mod.build_argv("check", {"paths": [str(library / "escape")]}, cfg)


# ----------------------------------------------------------------- jobs --

def test_a_job_runs_and_its_output_and_progress_can_be_followed(server, library, tmp_path):
    fake = tmp_path / "fake_engine.py"
    fake.write_text(FAKE_ENGINE)
    url, _ = server(engine=fake)
    code, job = call(f"{url}/api/jobs", {"kind": "check", "args": {"paths": [str(library)]}})
    assert code == 201 and job["state"] == "running"
    done = wait_done(url, job["id"])
    assert done["state"] == "finished" and done["returncode"] == 0
    assert done["progress"]["files_finished"] == 3
    assert done["lines"][0] == f"args: --check -- {library.resolve()}"
    assert done["lines"][-1] == "all done" and not any(l.startswith("@progress") for l in done["lines"])
    _, later = call(f"{url}/api/jobs/{job['id']}?since=3")
    assert later["first_line"] == 3 and later["lines"] == ["file 3 done", "all done"]
    _, listing = call(f"{url}/api/jobs")
    assert listing["jobs"][0]["id"] == job["id"]


def test_one_job_at_a_time_and_stop(server, library, tmp_path):
    fake = tmp_path / "fake_engine.py"
    fake.write_text(FAKE_ENGINE)
    slow = library / "Slow"
    slow.mkdir()
    url, _ = server(engine=fake)
    _, job = call(f"{url}/api/jobs", {"kind": "check", "args": {"paths": [str(slow)]}})
    code, busy = call(f"{url}/api/jobs", {"kind": "check", "args": {"paths": [str(library)]}})
    assert code == 409 and busy["job"]["id"] == job["id"]
    time.sleep(0.5)
    _, status = call(f"{url}/api/status")
    assert status["job"]["id"] == job["id"]
    call(f"{url}/api/jobs/{job['id']}/stop", {})
    done = wait_done(url, job["id"])
    assert done["state"] == "stopped" and "stopping cleanly" in done["lines"]


def test_jobs_survive_an_agent_restart(server, library, tmp_path, agent_mod, cfg):
    fake = tmp_path / "fake_engine.py"
    fake.write_text(FAKE_ENGINE)
    url, _ = server(engine=fake)
    _, job = call(f"{url}/api/jobs", {"kind": "check", "args": {"paths": [str(library)]}})
    wait_done(url, job["id"])
    url2, _ = server(engine=fake)
    _, again = call(f"{url2}/api/jobs/{job['id']}")
    assert again["state"] == "finished" and again["lines"][-1] == "all done"


def test_status(server):
    url, _ = server()
    _, s = call(f"{url}/api/status")
    assert s["name"] == "test-worker" and s["version"] and s["job"] is None
    assert s["disks"][0]["free"] > 0 and s["plex"] is False


def test_a_real_check_run_through_the_agent(server, library):
    url, _ = server()
    _, job = call(f"{url}/api/jobs", {"kind": "check", "args": {"paths": [str(library / "Movies")]}})
    done = wait_done(url, job["id"])
    assert done["state"] == "finished", done["lines"]
    assert "1 video(s) in these folders have no Clean copy yet." in done["lines"]


def test_plans_and_undo_logs_are_listed(server, cfg):
    url, _ = server()
    plans = cfg["engine_state"] / "plans"
    plans.mkdir(parents=True)
    (plans / "plan-20261115-220406.json").write_text(json.dumps(
        {"mode": "rename", "created": 1, "items": [{}, {}], "skipped": [], "unchanged": 4}))
    _, listing = call(f"{url}/api/plans")
    assert listing["plans"][0] == {"name": "plan-20261115-220406.json", "modified": listing["plans"][0]["modified"],
                                   "mode": "rename", "created": 1, "items": 2, "skipped": 0, "unchanged": 4}
    code, plan = call(f"{url}/api/plans/plan-20261115-220406.json")
    assert code == 200 and plan["mode"] == "rename"
    assert call(f"{url}/api/plans/..%2F..%2Fsecret.json")[0] in (400, 404)


def test_browse_stays_inside_the_library(server, library):
    url, _ = server()
    _, top = call(f"{url}/api/browse")
    _, at_root = call(f"{url}/api/browse?path={library}")
    assert at_root["parent"] is None
    assert top["dirs"] == [{"name": str(library.resolve()), "path": str(library.resolve())}]
    _, movies = call(f"{url}/api/browse?path={library}/Movies")
    assert [d["name"] for d in movies["dirs"]] == ["Film (2001)"] and movies["parent"] == str(library.resolve())
    _, film = call(f"{url}/api/browse?path={library}/Movies/Film%20(2001)")
    assert film["files"][0]["name"] == "Film.mkv" and film["parent"] == str((library / "Movies").resolve())
    assert call(f"{url}/api/browse?path=/etc")[0] == 400
    assert call(f"{url}/api/browse?path={library}/../..")[0] == 400


def test_audit_and_delete_jobs(agent_mod, cfg, library):
    film = library / "Movies" / "Film (2001)" / "Film.mkv"
    with pytest.raises(agent_mod.BadRequest, match="Plex is not set up"):
        agent_mod.build_argv("audit", {}, cfg)
    plex = {**cfg, "plex": True}
    assert agent_mod.build_argv("audit", {}, plex) == ["--audit", "--env-file", cfg["env_file"]]
    assert agent_mod.build_argv("delete", {"paths": [str(film)], "confirm": True}, cfg) == \
        ["--delete", "--yes", "--", str(film.resolve())]
    with pytest.raises(agent_mod.BadRequest, match="confirm"):
        agent_mod.build_argv("delete", {"paths": [str(film)]}, cfg)
    with pytest.raises(agent_mod.BadRequest, match="not a video"):
        agent_mod.build_argv("delete", {"paths": [str(film.parent)], "confirm": True}, cfg)
    assert "--two-episode" in agent_mod.build_argv("organize", {"paths": [str(library)], "mode": "rename",
                                                                "two_episode": "skip"}, plex)
    assert "--keep-leftovers" in agent_mod.build_argv("organize", {"paths": [str(library)], "mode": "rename",
                                                                   "cleanup": False}, plex)
    assert "--keep-leftovers" not in agent_mod.build_argv("organize", {"paths": [str(library)], "mode": "rename",
                                                                       "cleanup": True}, plex)


def test_update_is_only_a_request_for_the_root_service(server, tmp_path, monkeypatch, agent_mod):
    upd = tmp_path / "var-lib-mediatool"
    url, _ = server(update_dir=upd)
    code, err = call(f"{url}/api/update", {"tag": "latest"})
    assert code == 400 and "installer" in err["error"]
    upd.mkdir()
    assert call(f"{url}/api/update", {"tag": "v8;rm -rf /"})[0] == 400
    assert call(f"{url}/api/update", {"tag": "v8.4"})[0] == 202
    assert (upd / "update-request").read_text() == "v8.4\n"
    (upd / "update-result.json").write_text('{"state": "ok", "tag": "v8.4"}')
    _, s = call(f"{url}/api/status")
    assert s["update"]["pending"] and s["update"]["last"]["state"] == "ok"


def test_leave_as_is_hides_an_unmatched_folder_until_undone(server, cfg, library):
    url, _ = server()
    folder = str(library.resolve() / "Episodes" / "Fox Kids")
    cfg["engine_state"].mkdir(parents=True, exist_ok=True)
    (cfg["engine_state"] / "audit.json").write_text(json.dumps(
        {"created": 1, "unmatched": [{"folder": folder, "files": 3}], "counts": {"unmatched": 1, "duplicates": 0}}))
    assert call(f"{url}/api/leave", {"folder": "/etc"})[0] == 400
    assert call(f"{url}/api/leave", {"folder": folder})[0] == 200
    _, a = call(f"{url}/api/audit")
    assert a["unmatched"] == [] and a["left_alone"][0]["folder"] == folder and a["counts"]["unmatched"] == 0
    _, st = call(f"{url}/api/status")
    assert st["audit"]["counts"]["unmatched"] == 0
    assert call(f"{url}/api/leave", {"folder": folder, "leave": False})[0] == 200
    _, a = call(f"{url}/api/audit")
    assert a["unmatched"][0]["folder"] == folder


def test_leave_as_is_for_a_show_in_its_own_order(server, cfg, library):
    url, _ = server()
    show = str(library.resolve() / "Episodes" / "Scooby's All Star Laff-A-Lympics")
    cfg["engine_state"].mkdir(parents=True, exist_ok=True)
    (cfg["engine_state"] / "audit.json").write_text(json.dumps(
        {"created": 1, "unmatched": [], "order": [{"folder": show, "files": 21}], "counts": {"order": 1}}))
    assert call(f"{url}/api/leave", {"folder": show, "kind": "bogus"})[0] == 400
    assert call(f"{url}/api/leave", {"folder": show, "kind": "order"})[0] == 200
    _, a = call(f"{url}/api/audit")
    assert a["order"] == [] and a["order_left_alone"][0]["folder"] == show
    saved = json.loads((cfg["engine_state"] / "left-alone.json").read_text())
    assert saved["order"] == [show] and saved["unmatched"] == []
