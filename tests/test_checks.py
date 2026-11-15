"""The checks that decide whether a copy can replace its original, and the memory guard."""
import os
import subprocess
import sys

from conftest import make_video, needs_ffmpeg


def plan_for(tc, cfg, src):
    info = tc.ffprobe(cfg["ffprobe"], src)
    return {"path": src, "audio": [{}], "subs": [], "hdr": False,
            "duration": tc.duration_of(info), "video_seconds": tc.video_seconds(info)}


def remux(src, dst, *extra):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *extra, "-map", "0", "-c", "copy", str(dst)],
                   check=True)
    return dst


def zero_padded(src, dst):
    """The video's data ends halfway; the rest of the file is zeros, the header still says full length."""
    data = src.read_bytes()
    dst.write_bytes(data[:len(data) // 2] + b"\0" * (len(data) - len(data) // 2))
    return dst


@needs_ffmpeg
def test_a_full_copy_passes(tc, cfg, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=20)
    assert tc.verify(cfg, plan_for(tc, cfg, src), remux(src, tmp_path / "copy.mkv")) == (True, "ok")


@needs_ffmpeg
def test_a_short_copy_fails(tc, cfg, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=20)
    ok, why = tc.verify(cfg, plan_for(tc, cfg, src), remux(src, tmp_path / "copy.mkv", "-t", "5"))
    assert not ok and "duration mismatch" in why


@needs_ffmpeg
def test_a_damaged_original_is_named_and_its_short_copy_fails(tc, cfg, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=40)
    broken = zero_padded(src, tmp_path / "broken.mkv")
    ok, why = tc.verify(cfg, plan_for(tc, cfg, broken), remux(broken, tmp_path / "copy.mkv"))
    assert not ok and "damaged or incomplete" in why and "re-download" in why


@needs_ffmpeg
def test_replace_check_refuses_a_short_copy(tc, cfg, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=20)
    problems, _ = tc.check_pair(cfg, src, remux(src, tmp_path / "copy.mkv", "-t", "5"))
    assert any("length differs" in p for p in problems)
    broken = zero_padded(src, tmp_path / "broken.mkv")
    problems, _ = tc.check_pair(cfg, broken, remux(broken, tmp_path / "copy2.mkv"))
    assert any("length differs" in p for p in problems)


@needs_ffmpeg
def test_replace_check_passes_a_good_copy(tc, cfg, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=10)
    problems, facts = tc.check_pair(cfg, src, remux(src, tmp_path / "copy.mkv"))
    assert problems == [] and "0:10 = 0:10" in facts


@needs_ffmpeg
def test_video_continues(tc, tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=20)
    assert tc.video_continues("ffprobe", src, 5)
    assert not tc.video_continues("ffprobe", src, 19.9)


def test_lengths_compare_video_tracks_not_the_header(tc, tmp_path, monkeypatch):
    """Project Hail Mary: a dropped commentary made the header 4 minutes longer than the film."""
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setattr(tc, "ffprobe", lambda probe, path: {
        "format": {"duration": "9391.8"},
        "streams": [{"codec_type": "video", "tags": {"DURATION": "02:36:31.750000000"}}, {"codec_type": "audio"}]})
    plan = {"path": tmp_path / "src.mkv", "audio": [{}], "subs": [], "hdr": False,
            "duration": 9654.0, "video_seconds": 9391.716}
    assert tc.verify({"ffprobe": "ffprobe"}, plan, out) == (True, "ok")


def fake_ffmpeg():
    return [sys.executable, "-c", "import time\nfor i in range(100):\n print('progress=continue', flush=True); time.sleep(0.1)"]


def test_memory_guard_stops_ffmpeg_when_memory_stays_low(tc, monkeypatch):
    monkeypatch.setattr(tc, "mem_available_mb", lambda: 100)
    rc, err = tc.run_ffmpeg(fake_ffmpeg(), 10, "x", lambda s: None)
    assert rc == tc.LOW_MEMORY and "memory ran low" in err[-1]


def test_memory_guard_ignores_a_single_dip(tc, monkeypatch):
    readings = iter([4000, 100] + [4000] * 1000)
    monkeypatch.setattr(tc, "mem_available_mb", lambda: next(readings))
    rc, _ = tc.run_ffmpeg(fake_ffmpeg(), 10, "x", lambda s: None)
    assert rc == 0


def test_memory_guard_can_be_turned_off(tc, monkeypatch):
    monkeypatch.setattr(tc, "mem_available_mb", lambda: 1)
    tc.MEMORY["floor_mb"] = 0
    rc, _ = tc.run_ffmpeg(fake_ffmpeg(), 10, "x", lambda s: None)
    assert rc == 0


def test_mem_available_reads_meminfo(tc):
    if os.path.exists("/proc/meminfo"):
        assert tc.mem_available_mb() > 0
