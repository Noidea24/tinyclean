"""Shared test helpers: load tinyclean fresh for each test and make small real videos with ffmpeg."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tinyclean.py"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg and ffprobe are needed")


@pytest.fixture
def tc():
    """A fresh copy of the tinyclean module, so module-level state never leaks between tests."""
    spec = importlib.util.spec_from_file_location("tinyclean_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg(tc):
    """The settings a run uses, with the plain ffmpeg on PATH and the CPU encoder."""
    c = dict(tc.DEFAULTS)
    c.update({"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "encoder": "x265", "device": "",
              "overrides": {}, "x265_preset": "ultrafast", "retry_failed": False, "hdr_encode": True,
              "min_age_minutes": 0, "min_free_gb": 0, **tc.PROFILES["live"]})
    return c


def make_video(path, seconds=4, size="320x240", vcodec="libx264", channels=2, audio=True,
               subs=(), fonts=(), metadata=(), extra=()):
    """Write a small real video. subs: (language, title) pairs, written as SRT tracks.
    fonts: file names attached as TrueType fonts. metadata: (key, value) pairs on the file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}"]
    if audio:
        layout = {1: "mono", 2: "stereo", 6: "5.1"}[channels]
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                "-filter_complex", f"[1:a]aformat=channel_layouts={layout}[a]"]
    srt_inputs = []
    for i, _ in enumerate(subs):
        srt = path.parent / f".sub{i}.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n")
        srt_inputs.append(srt)
        cmd += ["-i", str(srt)]
    cmd += ["-map", "0:v"]
    if audio:
        cmd += ["-map", "[a]"]
    first_sub = 2 if audio else 1
    for i in range(len(subs)):
        cmd += ["-map", f"{first_sub + i}:s"]
    cmd += ["-c:v", vcodec]
    if vcodec == "libx265":
        cmd += ["-x265-params", "log-level=error"]
    if audio:
        cmd += ["-c:a", "aac", "-metadata:s:a:0", "language=eng"]
    for i, (lang, title) in enumerate(subs):
        cmd += [f"-c:s:{i}", "srt", f"-metadata:s:s:{i}", f"language={lang}", f"-metadata:s:s:{i}", f"title={title}"]
    for j, name in enumerate(fonts):
        font = path.parent / name
        font.write_bytes(b"\0\1\0\0" + b"font" * 64)
        cmd += ["-attach", str(font), f"-metadata:s:t:{j}", "mimetype=application/x-truetype-font"]
    for key, value in metadata:
        cmd += ["-metadata", f"{key}={value}"]
    cmd += [*extra, str(path)]
    subprocess.run(cmd, check=True)
    for srt in srt_inputs:
        srt.unlink()
    for name in fonts:
        (path.parent / name).unlink()
    return path


def run_cli(*args, home, check=True):
    """Run tinyclean as a command, the way the workers do, with its state kept under ``home``."""
    cmd = [sys.executable, str(SCRIPT), *map(str, args)]
    env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if check and result.returncode != 0:
        raise AssertionError(f"tinyclean exited {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def probe(path):
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)
