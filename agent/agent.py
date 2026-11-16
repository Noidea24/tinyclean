#!/usr/bin/env python3
"""
mediatool agent - a small web service on each transcoder that runs tinyclean for the dashboard.

It runs as the media user and only ever starts tinyclean with options it builds itself from a
fixed list of job kinds, on paths inside the configured library folders. Every request needs the
agent's token, as a Bearer header (the dashboard) or as the password of the browser's login box
(any user name). One job runs at a time; its output and progress are kept so a page can follow it
live or come back to it later.

Settings come from /etc/mediatool/mediatool.env (or MEDIATOOL_ENV), then the environment:
  AGENT_TOKEN   required, at least 24 characters
  AGENT_PORT    default 8770
  AGENT_BIND    default 0.0.0.0
  AGENT_ROOTS   library folders jobs may touch, comma separated (default /data)
  AGENT_NAME    shown on the dashboard (default: the host name)
"""
import base64
import collections
import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine" / "tinyclean.py"
ENV_FILE = "/etc/mediatool/mediatool.env"
PROGRESS_PREFIX = "@progress "
KEEP_JOBS = 200
MEMORY_LINES = 20000
PROFILES = ("animated", "live")
ORGANIZE_MODES = ("rename", "restructure", "newtree")
TEMPLATE_NAMES = ("movie_folder", "movie_file", "show_folder", "season_folder", "episode_file")
NAME_OK = re.compile(r"^(latest|(plan|undo)-[\w.-]+\.json)$")
REPOS = ("Noidea24/mediatool", "Noidea24/tinyclean")   # the second until the repo is renamed (GitHub redirects)
TAG_OK = re.compile(r"^v[0-9][0-9A-Za-z.-]*$")
VIDEO_EXTS_AGENT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".flv",
                    ".webm", ".divx", ".ogm"}


class BadRequest(Exception):
    pass


# ------------------------------------------------------------------ settings --

def load_env(path):
    values = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_file=None):
    env_file = env_file or os.environ.get("MEDIATOOL_ENV", ENV_FILE)
    values = {**load_env(env_file), **{k: v for k, v in os.environ.items()
                                       if k.startswith(("AGENT_", "PLEX_"))}}
    token = values.get("AGENT_TOKEN", "")
    if len(token) < 24:
        sys.exit(f"AGENT_TOKEN is missing or shorter than 24 characters in {env_file}.")
    roots = [Path(r).resolve() for r in values.get("AGENT_ROOTS", "/data").split(",") if r.strip()]
    home = Path(os.path.expanduser("~"))
    return {
        "token": token,
        "port": int(values.get("AGENT_PORT", 8770)),
        "bind": values.get("AGENT_BIND", "0.0.0.0"),
        "roots": roots,
        "name": values.get("AGENT_NAME") or socket.gethostname(),
        "engine": Path(values.get("AGENT_ENGINE") or ENGINE),
        "env_file": env_file,
        "plex": bool(values.get("PLEX_URL") and values.get("PLEX_TOKEN")),
        "plex_ignore": [t.strip() for t in values.get("PLEX_IGNORE_LIBRARIES", "").split(",") if t.strip()],
        "plex_refresh": values.get("PLEX_REFRESH", "").strip().lower() in ("1", "yes", "true", "on"),
        "update_dir": Path(values.get("AGENT_UPDATE_DIR") or "/var/lib/mediatool"),
        "state": Path(values.get("AGENT_STATE") or home / ".mediatool"),
        "engine_state": Path(values.get("AGENT_ENGINE_STATE") or home / ".tinyclean"),
    }


def engine_version(engine):
    try:
        m = re.search(r'^VERSION = "([^"]+)"', Path(engine).read_text(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


# -------------------------------------------------------------- job requests --

def _bool(a, key):
    v = a.get(key, False)
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def _int(a, key, lo, hi, default=0):
    v = a.get(key, default)
    if v in (None, ""):
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a whole number")
    if not lo <= v <= hi:
        raise BadRequest(f"{key} must be between {lo} and {hi}")
    return v


def _choice(a, key, choices, required=True):
    v = a.get(key)
    if v in (None, "") and not required:
        return None
    if v not in choices:
        raise BadRequest(f"{key} must be one of: {', '.join(choices)}")
    return v


def _paths(a, roots):
    raw = a.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not raw or not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise BadRequest("paths: give one or more folders or files")
    out = []
    for p in raw:
        real = Path(os.path.realpath(p.strip()))
        if not any(real == r or r in real.parents for r in roots):
            raise BadRequest(f"{p} is outside the library folders ({', '.join(map(str, roots))})")
        if not real.exists():
            raise BadRequest(f"{p} does not exist")
        out.append(str(real))
    return out


def _confirmed(a, what):
    if not _bool(a, "confirm"):
        raise BadRequest(f"{what} changes files: look at the dry run first, then send confirm=true")


def _name(a, key):
    v = a.get(key, "latest")
    if not isinstance(v, str) or not NAME_OK.match(v):
        raise BadRequest(f"{key} must be 'latest' or a file name from the list")
    return v


def build_argv(kind, a, cfg):
    """The engine's options for a job request. Only what is listed here can ever be passed."""
    if not isinstance(a, dict):
        raise BadRequest("args must be an object")
    if kind not in ("convert", "check", "status", "replace", "rename_suffix", "organize", "apply_plan", "undo",
                    "audit", "delete"):
        raise BadRequest("kind must be one of: convert, check, replace, rename_suffix, status, organize, "
                         "apply_plan, undo, audit, delete")
    plans, undo = cfg["engine_state"] / "plans", cfg["engine_state"] / "undo"
    if kind == "convert":
        argv = ["--profile", _choice(a, "profile", PROFILES), "--jobs", str(_int(a, "jobs", 1, 4, 1)),
                "--progress-lines"]
        for key, flag, hi in (("folders", "--folders", 100000), ("limit", "--limit", 100000)):
            n = _int(a, key, 0, hi)
            if n:
                argv += [flag, str(n)]
        sample = _int(a, "sample", 0, 20)
        if sample:
            argv += ["--sample", str(sample)]
        if _bool(a, "dry_run"):
            argv.append("--dry-run")
        if _bool(a, "retry_failed"):
            argv.append("--retry-failed")
        return argv + ["--"] + _paths(a, cfg["roots"])
    if kind in ("check", "status"):
        return ["--" + kind, "--"] + _paths(a, cfg["roots"])
    if kind in ("replace", "rename_suffix"):
        _confirmed(a, kind.replace("_", " "))
        return ["--" + kind.replace("_", "-"), "--yes", "--"] + _paths(a, cfg["roots"])
    env = ["--env-file", str(cfg["env_file"])]
    if kind in ("organize", "audit") and not cfg["plex"]:
        raise BadRequest(f"Plex is not set up on this worker: add PLEX_URL and PLEX_TOKEN to {cfg['env_file']}")
    if kind == "audit":
        return ["--audit"] + env + (["--"] + _paths(a, cfg["roots"]) if a.get("paths") else [])
    if kind == "delete":
        _confirmed(a, "deleting files")
        files = _paths(a, cfg["roots"])
        for f in files:
            if not Path(f).is_file() or Path(f).suffix.lower() not in VIDEO_EXTS_AGENT:
                raise BadRequest(f"{f} is not a video file")
        return ["--delete", "--yes", "--"] + files
    if kind == "organize":
        argv = ["--organize", _choice(a, "mode", ORGANIZE_MODES)] + env
        if a.get("two_episode") not in (None, ""):
            argv += ["--two-episode", _choice(a, "two_episode", ("name", "skip"))]
        n = _int(a, "folders", 0, 100000)
        if n:
            argv += ["--folders", str(n)]
        for t in TEMPLATE_NAMES:
            v = a.get(t)
            if v not in (None, ""):
                if not isinstance(v, str) or len(v) > 200 or "\n" in v:
                    raise BadRequest(f"{t}: a template of at most 200 characters")
                argv += ["--" + t.replace("_", "-"), v]
        return argv + ["--"] + _paths(a, cfg["roots"])
    if kind == "apply_plan":
        _confirmed(a, "applying a plan")
        name = _name(a, "plan")
        return ["--apply-plan", name if name == "latest" else str(plans / name), "--yes"] + env
    if kind == "undo":
        _confirmed(a, "undo")
        name = _name(a, "log")
        return ["--undo", name if name == "latest" else str(undo / name), "--yes"] + env
    raise BadRequest("kind must be one of: convert, check, replace, rename_suffix, status, organize, "
                     "apply_plan, undo, audit, delete")


# ---------------------------------------------------------------------- jobs --

class Jobs:
    """Starts the engine, one job at a time, and keeps each job's output and progress."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg["state"] / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.jobs = collections.OrderedDict()
        self.lines = {}
        self.procs = {}
        for f in sorted(self.dir.glob("*.json"), key=lambda f: f.stat().st_mtime):
            try:
                job = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if job.get("state") in ("running", "stopping"):
                job["state"] = "interrupted"
                job["ended"] = job.get("ended") or f.stat().st_mtime
                self._save(job)
            self.jobs[job["id"]] = job

    def _save(self, job):
        tmp = self.dir / f"{job['id']}.tmp"
        tmp.write_text(json.dumps(job))
        tmp.replace(self.dir / f"{job['id']}.json")

    def running(self):
        return next((j for j in self.jobs.values() if j["state"] in ("running", "stopping")), None)

    def start(self, kind, args):
        argv = build_argv(kind, args, self.cfg)
        with self.lock:
            busy = self.running()
            if busy:
                return None, busy
            job = {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "kind": kind,
                   "args": args, "argv": argv, "state": "running", "created": time.time(),
                   "ended": None, "returncode": None, "progress": None, "line_count": 0}
            self.jobs[job["id"]] = job
            self.lines[job["id"]] = []
            self._trim()
            log = open(self.dir / f"{job['id']}.log", "w", encoding="utf-8")
            try:
                proc = subprocess.Popen([sys.executable, str(self.cfg["engine"]), *argv],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
                                        start_new_session=True, cwd=os.path.expanduser("~"),
                                        env={**os.environ, "PYTHONUNBUFFERED": "1"})
            except OSError as exc:
                log.close()
                job.update(state="failed", ended=time.time())
                self._add_line(job, f"could not start the engine: {exc}", None)
                self._save(job)
                return job, None
            self.procs[job["id"]] = proc
            self._save(job)
        threading.Thread(target=self._follow, args=(job, proc, log), daemon=True).start()
        return job, None

    def _add_line(self, job, line, log):
        lines = self.lines.setdefault(job["id"], [])
        lines.append(line)
        if len(lines) > MEMORY_LINES:
            del lines[:len(lines) - MEMORY_LINES]
        job["line_count"] += 1
        if log:
            log.write(line + "\n")
            log.flush()

    def _follow(self, job, proc, log):
        last_save = time.time()
        for raw in proc.stdout:
            line = raw.rstrip("\n").split("\r")[-1].rstrip()
            with self.lock:
                if line.startswith(PROGRESS_PREFIX):
                    try:
                        job["progress"] = json.loads(line[len(PROGRESS_PREFIX):])
                    except ValueError:
                        pass
                else:
                    self._add_line(job, line, log)
                if time.time() - last_save > 10:
                    self._save(job)
                    last_save = time.time()
        rc = proc.wait()
        log.close()
        with self.lock:
            stopped = job["state"] == "stopping"
            job.update(returncode=rc, ended=time.time(),
                       state="stopped" if stopped else ("finished" if rc == 0 else "failed"))
            self.procs.pop(job["id"], None)
            self._save(job)

    def stop(self, job_id, after_current=False):
        with self.lock:
            job = self.jobs.get(job_id)
            proc = self.procs.get(job_id)
            if not job or not proc:
                return job
            if after_current and job["kind"] == "convert":
                (self.cfg["engine_state"] / "STOP").touch()
                self._add_line(job, "[agent] stop requested: finishing the file(s) in progress first", None)
                return job
            first = job["state"] == "running"
            job["state"] = "stopping"
            self._save(job)
        try:
            proc.send_signal(signal.SIGINT if first else signal.SIGTERM)
        except OSError:
            pass
        return job

    def stop_all(self, wait=60):
        for job_id in list(self.procs):
            self.stop(job_id)
        end = time.time() + wait
        while self.procs and time.time() < end:
            time.sleep(0.5)

    def output(self, job_id, since=0):
        job = self.jobs.get(job_id)
        if not job:
            return None
        lines = self.lines.get(job_id)
        if lines is None:
            try:
                lines = (self.dir / f"{job_id}.log").read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
        first = job["line_count"] - len(lines)
        start = max(since, first)
        return {**job, "first_line": start, "lines": lines[start - first:]}

    def _trim(self):
        while len(self.jobs) > KEEP_JOBS:
            old_id, old = next(iter(self.jobs.items()))
            if old["state"] in ("running", "stopping"):
                break
            self.jobs.pop(old_id)
            self.lines.pop(old_id, None)
            for ext in (".json", ".log"):
                (self.dir / f"{old_id}{ext}").unlink(missing_ok=True)


# -------------------------------------------------------------------- status --

def meminfo():
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {"total": out.get("MemTotal"), "available": out.get("MemAvailable")}


def status(cfg, jobs, started):
    disks = []
    for r in cfg["roots"]:
        try:
            u = shutil.disk_usage(r)
            disks.append({"path": str(r), "total": u.total, "free": u.free})
        except OSError:
            disks.append({"path": str(r), "total": None, "free": None, "missing": True})
    running = jobs.running()
    return {"name": cfg["name"], "host": socket.gethostname(), "version": engine_version(cfg["engine"]),
            "uptime": int(time.time() - started), "cpus": os.cpu_count(), "load": os.getloadavg(),
            "memory": meminfo(), "disks": disks, "roots": [str(r) for r in cfg["roots"]],
            "gpu": os.path.exists("/dev/dri/renderD128"),
            "ffmpeg": "jellyfin" if os.path.exists("/usr/lib/jellyfin-ffmpeg/ffmpeg")
            else ("system" if shutil.which("ffmpeg") else None),
            "plex": cfg["plex"], "plex_ignore": cfg.get("plex_ignore", []),
            "plex_refresh": cfg.get("plex_refresh", False),
            "audit": audit_summary(cfg), "update": update_state(cfg),
            "job": summary(running) if running else None}


def audit_summary(cfg):
    try:
        a = json.loads((cfg["engine_state"] / "audit.json").read_text())
        return {"created": a.get("created"), "counts": a.get("counts", {})}
    except (OSError, ValueError):
        return None


# -------------------------------------------------------------------- update --
# The agent never installs anything itself: it leaves a request file, and a root service on this
# machine (mediatool-update, set up by the installer) installs that release from GitHub.

_latest = {"tag": None, "checked": 0, "error": None}


def latest_release(force=False):
    if not force and time.time() - _latest["checked"] < 3600:
        return _latest
    import urllib.request
    _latest.update(checked=time.time(), error=None)
    for repo in REPOS:
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                         headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "mediatool-agent"})
            with urllib.request.urlopen(req, timeout=10) as r:
                tag = json.load(r).get("tag_name")
            if tag and TAG_OK.match(tag):
                _latest["tag"] = tag
                return _latest
        except (OSError, ValueError) as exc:
            _latest["error"] = str(getattr(exc, "reason", exc))
    return _latest


def update_state(cfg):
    d = cfg.get("update_dir")
    if not d:
        return {"available": False, "pending": False, "last": None}
    try:
        result = json.loads((d / "update-result.json").read_text())
    except (OSError, ValueError):
        result = None
    return {"available": d.is_dir(), "pending": (d / "update-request").exists(), "last": result}


def request_update(cfg, jobs, tag):
    if tag not in (None, "", "latest") and not (isinstance(tag, str) and TAG_OK.match(tag)):
        raise BadRequest("tag must be 'latest' or a release such as v8.4")
    if jobs.running():
        raise BadRequest("a job is running: update when it is done")
    d = cfg.get("update_dir")
    if not d or not d.is_dir():
        raise BadRequest("updates from the page are not set up here: run the installer once as root")
    tmp = d / "update-request.tmp"
    tmp.write_text((tag or "latest") + "\n")
    tmp.replace(d / "update-request")


def summary(job):
    return {k: job.get(k) for k in ("id", "kind", "args", "state", "created", "ended", "returncode",
                                     "progress", "line_count")}


def list_saved(folder, pattern):
    out = []
    for f in sorted(folder.glob(pattern), reverse=True)[:100]:
        item = {"name": f.name, "modified": f.stat().st_mtime}
        try:
            data = json.loads(f.read_text())
            if "items" in data:
                item.update(mode=data.get("mode"), created=data.get("created"), items=len(data["items"]),
                            skipped=len(data.get("skipped", [])), unchanged=data.get("unchanged"))
            elif "done" in data:
                item.update(mode=data.get("mode"), changes=len(data["done"]), plan=Path(data.get("plan", "")).name)
        except (OSError, ValueError):
            item["unreadable"] = True
        out.append(item)
    return out


VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".flv",
              ".webm", ".divx", ".ogm"}


def browse(cfg, path):
    """One folder's sub-folders and videos, inside the library folders only. No path: the library folders."""
    if not path:
        return {"path": None, "parent": None, "dirs": [{"name": str(r), "path": str(r)} for r in cfg["roots"]],
                "files": []}
    real = Path(os.path.realpath(path))
    root = next((r for r in cfg["roots"] if real == r or r in real.parents), None)
    if root is None:
        raise BadRequest("outside the library folders")
    if not real.is_dir():
        raise BadRequest("not a folder")
    dirs, files = [], []
    with os.scandir(real) as it:
        for e in it:
            if e.name.startswith((".", "@", "#")):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": str(real / e.name), "clean": e.name == "Clean"})
                elif os.path.splitext(e.name)[1].lower() in VIDEO_EXTS:
                    files.append({"name": e.name, "path": str(real / e.name), "size": e.stat().st_size})
            except OSError:
                continue
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return {"path": str(real), "parent": None if real == root else str(real.parent), "dirs": dirs, "files": files}


def ledger_rows(path, after=0):
    import csv
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        rows = []
    return {"total": len(rows), "rows": rows[after:after + 5000]}


# ---------------------------------------------------------------------- HTTP --

def token_ok(header, token):
    if not header:
        return False
    kind, _, value = header.partition(" ")
    if kind.lower() == "bearer":
        given = value.strip()
    elif kind.lower() == "basic":
        try:
            given = base64.b64decode(value.strip()).decode("utf-8", "replace").partition(":")[2]
        except ValueError:
            return False
    else:
        return False
    return hmac.compare_digest(given.encode(), token.encode())


def make_handler(cfg, jobs, started):
    page = (HERE / "page.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "mediatool-agent"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if token_ok(self.headers.get("Authorization"), cfg["token"]):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="mediatool agent", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            if not self.authorized():
                return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = [p for p in u.path.split("/") if p]
            try:
                if u.path == "/":
                    return self.send(200, page, "text/html")
                if parts == ["api", "status"]:
                    return self.send(200, status(cfg, jobs, started))
                if parts == ["api", "jobs"]:
                    with jobs.lock:
                        items = [summary(j) for j in reversed(jobs.jobs.values())]
                    return self.send(200, {"jobs": items[:int(q.get("limit", 50))]})
                if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    with jobs.lock:
                        out = jobs.output(parts[2], int(q.get("since", 0)))
                    return self.send(200, out) if out else self.send(404, {"error": "no such job"})
                if parts == ["api", "plans"]:
                    return self.send(200, {"plans": list_saved(cfg["engine_state"] / "plans", "plan-*.json")})
                if parts == ["api", "undo"]:
                    return self.send(200, {"logs": list_saved(cfg["engine_state"] / "undo", "undo-*.json")})
                if len(parts) == 3 and parts[:2] in (["api", "plans"], ["api", "undo"]):
                    if not NAME_OK.match(parts[2]) or parts[2] == "latest":
                        return self.send(400, {"error": "bad name"})
                    f = cfg["engine_state"] / parts[1] / parts[2]
                    return self.send(200, f.read_bytes()) if f.is_file() else self.send(404, {"error": "not found"})
                if parts == ["api", "audit"]:
                    f = cfg["engine_state"] / "audit.json"
                    return self.send(200, f.read_bytes()) if f.is_file() else self.send(404, {"error": "no audit yet"})
                if parts == ["api", "update"]:
                    lr = latest_release(q.get("check") == "1")
                    return self.send(200, {"current": engine_version(cfg["engine"]), "latest": lr["tag"],
                                           "latest_error": lr["error"], **update_state(cfg)})
                if parts == ["api", "browse"]:
                    try:
                        return self.send(200, browse(cfg, q.get("path")))
                    except BadRequest as exc:
                        return self.send(400, {"error": str(exc)})
                    except OSError as exc:
                        return self.send(500, {"error": f"could not read the folder: {exc.strerror}"})
                if parts == ["api", "ledger"]:
                    return self.send(200, ledger_rows(cfg["engine_state"] / "ledger.csv", int(q.get("after", 0))))
            except ValueError:
                return self.send(400, {"error": "bad number in the query"})
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            # Browsers cannot send JSON to another site without asking first, and this agent never says
            # yes, so a page elsewhere cannot start jobs with the login the browser remembers.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.send(403, {"error": "requests from other sites are refused"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send(415, {"error": "send JSON"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad JSON"})
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts == ["api", "jobs"]:
                try:
                    job, busy = jobs.start(body.get("kind"), body.get("args", {}))
                except BadRequest as exc:
                    return self.send(400, {"error": str(exc)})
                if busy:
                    return self.send(409, {"error": "a job is already running", "job": summary(busy)})
                return self.send(201, summary(job))
            if parts == ["api", "update"]:
                try:
                    request_update(cfg, jobs, body.get("tag"))
                except BadRequest as exc:
                    return self.send(409 if "running" in str(exc) else 400, {"error": str(exc)})
                return self.send(202, {"ok": True, "message": "update requested; the agent restarts in a minute"})
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop":
                job = jobs.stop(parts[2], after_current=bool(body.get("after_current")))
                return self.send(200, summary(job)) if job else self.send(404, {"error": "no such job"})
            return self.send(404, {"error": "not found"})

    return Handler


def serve(cfg):
    jobs = Jobs(cfg)
    server = ThreadingHTTPServer((cfg["bind"], cfg["port"]), make_handler(cfg, jobs, time.time()))
    server.daemon_threads = True

    def shutdown(signum, frame):
        threading.Thread(target=lambda: (jobs.stop_all(), server.shutdown()), daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"mediatool agent {engine_version(cfg['engine'])} '{cfg['name']}' listening on "
          f"{cfg['bind']}:{server.server_address[1]}, libraries {', '.join(map(str, cfg['roots']))}", flush=True)
    server.serve_forever()
    return jobs, server


if __name__ == "__main__":
    serve(load_config())
