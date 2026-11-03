#!/usr/bin/env python3
"""
tinyclean - shrink and normalize a media library for TV playback.

Rules (in order of what matters most):
  Audio     ONE track. English if there is one (commentary skipped), otherwise
            the file's main track. Stereo is kept as-is when it is already a
            TV-friendly codec (AAC, AC3, E-AC3, MP3). Surround is replaced by a
            "night mode" stereo downmix: centre (dialogue) weighted up, LFE
            dropped, then loudness-levelled so quiet talk and loud action end up
            closer together. Output AAC 192k.
  Subtitles Keep every English track (forced, full, SDH; SRT, ASS, PGS, VobSub).
            If no track is tagged English, keep untagged ones (a lone unnamed
            track is almost always English). Drop everything else.
            MP4 text subs are converted to SRT. Font attachments are kept so
            styled ASS subs still render.
  Video     H.265 on Intel Quick Sync. 4K is scaled to 1080p. Interlaced DVD
            material is deinterlaced. 10-bit sources stay 10-bit. HDR video and
            already-efficient H.265/AV1 are copied, not re-encoded.
  Safety    Output is written next to the source as a hidden temp file, checked
            (duration, streams), and only kept if it is smaller (or if the
            stereo rule required the change). Originals are moved to a
            quarantine folder on the same share, not deleted, unless asked.
  Naming    <original name>-Clean.mkv. Files already ending in -Clean are done.

Runs as a normal user that can write to the library. State, log and a CSV
ledger live in --state-dir. Ctrl+C stops cleanly (temp file removed). Touch
<state-dir>/STOP to finish the current file and then stop.
"""

import argparse
import csv
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------- defaults --

SUFFIX = "-Clean"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts",
              ".mpg", ".mpeg", ".flv", ".webm", ".divx", ".ogm"}
TEMP_PREFIX = ".tinyclean."
STEREO_COPY_CODECS = {"aac", "ac3", "eac3", "mp3"}
ENGLISH = {"eng", "en", "english"}
UNTAGGED = {"", "und", "unk", "unknown", "none", "zxx"}
TEXT_SUB_CONVERT = {"mov_text": "srt"}          # codec -> target for MKV
DROP_SUB_CODECS = {"eia_608", "none", ""}       # not storable / not real subs
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
INTERLACED = {"tt", "bb", "tb", "bt"}

DEFAULTS = {
    "quality_hd": 24,        # Quick Sync ICQ / CQP value for > 576p (lower = better, bigger)
    "quality_sd": 23,        # value for SD (DVD) material
    "max_height": 1080,
    "copy_hevc_bpp": 0.06,   # H.265/AV1 below this bits-per-pixel is left alone
    "min_saving": 0.05,      # a re-encode must save at least 5% to be kept
    "audio_bitrate": "192k",
    "min_age_minutes": 60,   # skip files touched in the last hour (imports in progress)
}

# ------------------------------------------------------------- small helpers --


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def hms(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def lang_of(stream):
    return (stream.get("tags", {}).get("language") or "").strip().lower()


def title_of(stream):
    return (stream.get("tags", {}).get("title") or "").strip().lower()


def is_english(stream):
    return lang_of(stream) in ENGLISH


def is_untagged(stream):
    return lang_of(stream) in UNTAGGED


def disp(stream, key):
    return int(stream.get("disposition", {}).get(key, 0)) == 1


def parse_rate(value):
    try:
        num, den = value.split("/")
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except (ValueError, AttributeError):
        return 0.0


def stream_bitrate(stream):
    """Bitrate from the stream, or from MKV statistics tags (BPS / BPS-eng)."""
    if stream.get("bit_rate", "").isdigit():
        return int(stream["bit_rate"])
    for key, val in stream.get("tags", {}).items():
        if key.upper().startswith("BPS") and str(val).isdigit():
            return int(val)
    return 0


# --------------------------------------------------------------- probing --


def ffprobe(ffprobe_bin, path):
    out = subprocess.run(
        [ffprobe_bin, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout)


def duration_of(info):
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        best = 0.0
        for s in info.get("streams", []):
            try:
                best = max(best, float(s.get("duration", 0)))
            except ValueError:
                pass
        return best


# --------------------------------------------------------------- planning --

# Channel order for common layouts, so the night-mode downmix can name them.
LAYOUTS = {
    "mono": ["FC"], "stereo": ["FL", "FR"], "2.1": ["FL", "FR", "LFE"],
    "3.0": ["FL", "FR", "FC"], "3.1": ["FL", "FR", "FC", "LFE"],
    "4.0": ["FL", "FR", "FC", "BC"], "quad": ["FL", "FR", "BL", "BR"],
    "quad(side)": ["FL", "FR", "SL", "SR"],
    "5.0": ["FL", "FR", "FC", "BL", "BR"], "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "4.1": ["FL", "FR", "FC", "LFE", "BC"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "6.0": ["FL", "FR", "FC", "BC", "SL", "SR"],
    "6.1": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    "7.0": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"],
    "7.1(wide-side)": ["FL", "FR", "FC", "LFE", "FLC", "FRC", "SL", "SR"],
}
DEFAULT_LAYOUT_FOR = {3: "3.0", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def nightmode_filter(stream):
    """Dialogue-forward stereo downmix + loudness levelling for surround tracks."""
    layout = (stream.get("channel_layout") or "").strip()
    chans = LAYOUTS.get(layout) or LAYOUTS.get(DEFAULT_LAYOUT_FOR.get(int(stream.get("channels", 0)), ""))
    level = "loudnorm=I=-16:LRA=7:TP=-1.5,aresample=48000"
    if not chans:
        # Unknown layout: level it, and let "-ac 2" do a standard downmix.
        return level, "standard downmix (unknown layout)"
    weights = {"FC": 1.0, "FL": 0.7, "FR": 0.7, "FLC": 0.6, "FRC": 0.6}
    left, right = [], []
    if "FC" in chans:
        left.append("1.0*FC")
        right.append("1.0*FC")
    for side, out in (("L", left), ("R", right)):
        front = "F" + side
        if front in chans:
            out.append(f"{weights[front]}*{front}")
        wide = "F" + side + "C"
        if wide in chans:
            out.append(f"{weights[wide]}*{wide}")
        rears = [c for c in ("S" + side, "B" + side) if c in chans]
        for c in rears:
            out.append(f"{0.5 / len(rears):.2f}*{c}")
    if "BC" in chans:
        left.append("0.35*BC")
        right.append("0.35*BC")
    pan = f"pan=stereo|FL<{'+'.join(left)}|FR<{'+'.join(right)}"
    return f"{pan},{level}", f"night-mode downmix from {layout or str(stream.get('channels')) + 'ch'}"


def pick_audio(streams):
    """Choose the single audio stream to keep."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return None, "no audio"

    def main_tracks(group):
        mains = [s for s in group if "commentary" not in title_of(s) and not disp(s, "comment")]
        return mains or group

    eng = [s for s in audio if is_english(s)]
    if eng:
        pool, why = main_tracks(eng), "English"
    else:
        pool = main_tracks(audio)
        default = [s for s in pool if disp(s, "default")]
        pool, why = (default or pool), "no English track, using main track"
    # Within the pool: an existing stereo/mono track beats a surround one.
    small = [s for s in pool if int(s.get("channels", 2)) <= 2]
    return (small[0] if small else pool[0]), why


def pick_subs(streams):
    subs = [s for s in streams if s.get("codec_type") == "subtitle"
            and s.get("codec_name", "") not in DROP_SUB_CODECS]
    english = [s for s in subs if is_english(s)]
    if english:
        return english, "English"
    untagged = [s for s in subs if is_untagged(s)]
    if untagged:
        return untagged, "untagged (assumed English)"
    return [], "none kept"


def plan_file(path, info, cfg):
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and not disp(s, "attached_pic")]
    if not videos:
        raise RuntimeError("no video stream")
    v = videos[0]
    dur = duration_of(info)
    size = path.stat().st_size

    # ---- video
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    fps = parse_rate(v.get("avg_frame_rate")) or parse_rate(v.get("r_frame_rate")) or 24.0
    vbr = stream_bitrate(v)
    if not vbr and dur > 0:
        other = sum(stream_bitrate(s) for s in streams if s is not v)
        vbr = max(0, int(size * 8 / dur) - other)
    bpp = vbr / (w * h * fps) if w and h and fps else 0.0
    hdr = v.get("color_transfer", "") in HDR_TRANSFERS
    ten_bit = "10" in v.get("pix_fmt", "") or "12" in v.get("pix_fmt", "")
    needs_scale = h > cfg["max_height"]
    interlaced = v.get("field_order", "") in INTERLACED
    efficient = v.get("codec_name") in ("hevc", "av1") and bpp and bpp < cfg["copy_hevc_bpp"]

    if hdr:
        video_mode, video_why = "copy", "HDR (kept as-is)"
    elif efficient and not needs_scale:
        video_mode, video_why = "copy", f"already efficient {v.get('codec_name')} ({bpp:.3f} bpp)"
    else:
        video_mode = "encode"
        bits = []
        if needs_scale:
            bits.append(f"{h}p->{cfg['max_height']}p")
        if interlaced:
            bits.append("deinterlace")
        bits.append("10-bit" if ten_bit else "8-bit")
        q = cfg["quality_sd"] if h <= 576 else cfg["quality_hd"]
        bits.append(f"q{q}")
        video_why = f"{v.get('codec_name')} {w}x{h} {bpp:.3f} bpp -> H.265 (" + ", ".join(bits) + ")"

    # ---- audio
    a, a_why = pick_audio(streams)
    audio = None
    if a is not None:
        ch = int(a.get("channels", 2))
        if ch <= 2 and a.get("codec_name") in STEREO_COPY_CODECS:
            audio = {"stream": a, "mode": "copy", "why": f"{a_why}: copy {a.get('codec_name')} {ch}ch"}
        elif ch <= 2:
            audio = {"stream": a, "mode": "aac", "why": f"{a_why}: {a.get('codec_name')} {ch}ch -> AAC"}
        else:
            filt, what = nightmode_filter(a)
            audio = {"stream": a, "mode": "downmix", "filter": filt,
                     "why": f"{a_why}: {a.get('codec_name')} {ch}ch -> AAC stereo, {what}"}

    # ---- subtitles
    subs, s_why = pick_subs(streams)
    all_subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio_count = sum(1 for s in streams if s.get("codec_type") == "audio")
    attachments = any(s.get("codec_type") == "attachment" for s in streams)

    # Does the file break a rule even if the video is fine?
    rule_change = (
        (audio and audio["mode"] != "copy") or audio_count > 1
        or len(subs) != len(all_subs) or any(s.get("codec_name") in TEXT_SUB_CONVERT for s in subs)
        or path.suffix.lower() != ".mkv")

    return {
        "path": path, "size": size, "duration": dur, "video": v, "video_mode": video_mode,
        "video_why": video_why, "hdr": hdr, "ten_bit": ten_bit, "needs_scale": needs_scale,
        "interlaced": interlaced, "height": h, "audio": audio, "subs": subs, "subs_why": s_why,
        "subs_dropped": len(all_subs) - len(subs), "attachments": attachments,
        "rule_change": rule_change, "audio_required": bool(audio and audio["mode"] == "downmix"),
    }


# -------------------------------------------------------------- ffmpeg cmd --


def build_cmd(plan, cfg, out_path, video_mode, rate_mode="icq", sample=None):
    v = plan["video"]
    cmd = [cfg["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", "-fflags", "+genpts"]
    encode = video_mode == "encode"
    if encode and cfg["encoder"] == "qsv":
        # Same device setup Jellyfin uses: VA-API on the iGPU, Quick Sync on top.
        cmd += ["-init_hw_device", f"vaapi=va:{cfg['device']},driver=iHD",
                "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
    if sample:
        cmd += ["-ss", str(sample[0])]
    cmd += ["-i", str(plan["path"])]
    if sample:
        cmd += ["-t", str(sample[1])]

    cmd += ["-map", f"0:{v['index']}"]
    if plan["audio"]:
        cmd += ["-map", f"0:{plan['audio']['stream']['index']}"]
    for s in plan["subs"]:
        cmd += ["-map", f"0:{s['index']}"]
    if plan["attachments"]:
        cmd += ["-map", "0:t?"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-max_muxing_queue_size", "9999"]

    # video
    if encode:
        filters = []
        if plan["interlaced"]:
            filters.append("bwdif=mode=send_frame")
        if plan["needs_scale"]:
            filters.append(f"scale=-2:{cfg['max_height']}:flags=lanczos")
        q = cfg["quality_sd"] if plan["height"] <= 576 else cfg["quality_hd"]
        if cfg["encoder"] == "qsv":
            filters += ["format=p010le" if plan["ten_bit"] else "format=nv12",
                        "hwupload=extra_hw_frames=64"]
            cmd += ["-vf", ",".join(filters), "-c:v", "hevc_qsv", "-preset", "slow",
                    "-profile:v", "main10" if plan["ten_bit"] else "main"]
            # ICQ (constant quality) first; CQP if the driver refuses ICQ.
            cmd += ["-global_quality", str(q)] if rate_mode == "icq" else ["-q:v", str(q)]
        else:  # software x265, for testing or a no-GPU box
            filters.append("format=yuv420p10le" if plan["ten_bit"] else "format=yuv420p")
            cmd += ["-vf", ",".join(filters), "-c:v", "libx265", "-preset", cfg["x265_preset"],
                    "-crf", str(q), "-x265-params", "log-level=error"]
    else:
        cmd += ["-c:v", "copy"]

    # audio
    a = plan["audio"]
    if a:
        if a["mode"] == "copy":
            cmd += ["-c:a", "copy"]
        elif a["mode"] == "aac":
            cmd += ["-c:a", "aac", "-b:a", cfg["audio_bitrate"]]
        else:
            cmd += ["-filter:a", a["filter"], "-c:a", "aac", "-b:a", cfg["audio_bitrate"], "-ac", "2"]
        cmd += ["-disposition:a:0", "default"]

    # subtitles: copy, converting MP4 text subs. Only forced tracks are
    # marked default, so full subtitles never switch themselves on.
    cmd += ["-c:s", "copy"]
    for i, s in enumerate(plan["subs"]):
        target = TEXT_SUB_CONVERT.get(s.get("codec_name"))
        if target:
            cmd += [f"-c:s:{i}", target]
        forced = disp(s, "forced") or "forced" in title_of(s)
        cmd += [f"-disposition:s:{i}", "default+forced" if forced else "0"]
    cmd += ["-c:t", "copy"] if plan["attachments"] else []

    cmd += ["-f", "matroska", str(out_path)]
    return cmd


# --------------------------------------------------------------- running --

class Stopper:
    def __init__(self):
        self.now = False
        self.proc = None

    def handle(self, signum, frame):
        if self.now:
            sys.exit(130)
        self.now = True
        print("\n[stop] Stopping: cleaning up the current file. Press Ctrl+C again to force.", flush=True)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


STOP = Stopper()


def run_ffmpeg(cmd, total_seconds, label, progress):
    """Run ffmpeg, drawing a progress line. Returns (returncode, last stderr lines)."""
    err = deque(maxlen=40)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    STOP.proc = proc

    def pump_err():
        for line in proc.stderr:
            err.append(line.rstrip())

    t = threading.Thread(target=pump_err, daemon=True)
    t.start()
    state, started, last_draw = {}, time.time(), 0.0
    for line in proc.stdout:
        k, _, val = line.strip().partition("=")
        state[k] = val
        if k == "progress":
            now = time.time()
            if now - last_draw >= 1.0 or val == "end":
                last_draw = now
                try:
                    done = int(state.get("out_time_us") or state.get("out_time_ms") or 0) / 1e6
                except ValueError:
                    done = 0.0
                pct = min(100.0, done / total_seconds * 100) if total_seconds else 0.0
                speed = state.get("speed", "?").strip()
                fps = state.get("fps", "?")
                elapsed = now - started
                eta = (elapsed / pct * (100 - pct)) if pct > 0.5 else 0
                progress(f"{label} {pct:5.1f}%  fps {fps}  speed {speed}  ETA {hms(eta) if eta else '--'}")
    proc.wait()
    t.join(timeout=2)
    STOP.proc = None
    return proc.returncode, list(err)


def verify(cfg, plan, out_path, sample_len=None):
    try:
        info = ffprobe(cfg["ffprobe"], out_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"output unreadable: {exc}"
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        return False, "no video in output"
    want_audio = 1 if plan["audio"] else 0
    got_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    if got_audio != want_audio:
        return False, f"expected {want_audio} audio track, got {got_audio}"
    got_subs = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    if got_subs != len(plan["subs"]):
        return False, f"expected {len(plan['subs'])} subtitle tracks, got {got_subs}"
    src = sample_len or plan["duration"]
    out = duration_of(info)
    if src > 0 and abs(out - src) > max(2.0, src * 0.005):
        return False, f"duration mismatch: source {src:.1f}s, output {out:.1f}s"
    if out_path.stat().st_size < 1024 * 1024 and src > 60:
        return False, "output suspiciously small"
    return True, "ok"


# ---------------------------------------------------------------- library --


def find_files(roots, cfg, state):
    skip_dir = re.compile(r"^[._@#]")
    min_age = time.time() - cfg["min_age_minutes"] * 60
    items = []
    for root in roots:
        root = Path(root)
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not skip_dir.match(d))
                for name in filenames:
                    candidates.append(Path(dirpath) / name)
        for p in candidates:
            if p.name.startswith(TEMP_PREFIX) or p.suffix.lower() not in VIDEO_EXTS:
                continue
            if p.stem.lower().endswith(SUFFIX.lower()):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime > min_age:
                continue
            prev = state.get(str(p))
            if prev and prev.get("mtime") == int(st.st_mtime) and not cfg["retry_failed"]:
                continue
            items.append((p, st.st_size))
    items.sort(key=lambda x: x[1], reverse=True)
    return items


def clean_stale_temps(roots):
    for root in roots:
        root = Path(root)
        if root.is_file():
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.startswith(TEMP_PREFIX):
                    try:
                        os.remove(os.path.join(dirpath, name))
                        print(f"[cleanup] removed stale temp file {name}")
                    except OSError:
                        pass


def quarantine_target(cfg, src):
    base = Path(cfg["library_base"])
    try:
        rel = src.relative_to(base)
    except ValueError:
        rel = Path(src.name)
    return Path(cfg["originals"]) / rel


# ------------------------------------------------------------------ main --


def describe(plan):
    lines = [f"  video : {plan['video_mode']} - {plan['video_why']}"]
    a = plan["audio"]
    lines.append(f"  audio : {a['why'] if a else 'none in source'}")
    kept = ", ".join(f"{lang_of(s) or 'untagged'}/{s.get('codec_name')}{' forced' if disp(s, 'forced') else ''}"
                     for s in plan["subs"]) or "-"
    lines.append(f"  subs  : keep {len(plan['subs'])} [{kept}], drop {plan['subs_dropped']} ({plan['subs_why']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Shrink and normalize video files (see header for rules).")
    ap.add_argument("paths", nargs="+", help="folders or files to process")
    ap.add_argument("--dry-run", action="store_true", help="show the plan for each file, change nothing")
    ap.add_argument("--sample", type=int, metavar="N", help="encode a 60 s clip of the N largest files and report projected savings")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--delete-originals", action="store_true", help="delete originals instead of moving them to quarantine")
    ap.add_argument("--originals", default="/data/_originals", help="quarantine folder (same share as the library)")
    ap.add_argument("--library-base", default="/data", help="base used to mirror folders inside the quarantine")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.tinyclean"))
    ap.add_argument("--encoder", choices=["qsv", "x265"], default="qsv")
    ap.add_argument("--device", default="/dev/dri/renderD128")
    ap.add_argument("--quality-hd", type=int, default=DEFAULTS["quality_hd"])
    ap.add_argument("--quality-sd", type=int, default=DEFAULTS["quality_sd"])
    ap.add_argument("--x265-preset", default="medium")
    ap.add_argument("--min-age", type=int, default=DEFAULTS["min_age_minutes"], help="minutes since last change before a file is touched")
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    jf = "/usr/lib/jellyfin-ffmpeg/"
    cfg = dict(DEFAULTS)
    cfg.update({
        "ffmpeg": jf + "ffmpeg" if os.path.exists(jf + "ffmpeg") else "ffmpeg",
        "ffprobe": jf + "ffprobe" if os.path.exists(jf + "ffprobe") else "ffprobe",
        "encoder": args.encoder, "device": args.device,
        "quality_hd": args.quality_hd, "quality_sd": args.quality_sd,
        "x265_preset": args.x265_preset, "min_age_minutes": args.min_age,
        "retry_failed": args.retry_failed, "originals": args.originals,
        "library_base": args.library_base,
    })

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(state_dir / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another tinyclean run is already going.")
    state_file = state_dir / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    ledger = state_dir / "ledger.csv"
    new_ledger = not ledger.exists()
    stop_flag = state_dir / "STOP"
    if stop_flag.exists():
        stop_flag.unlink()

    signal.signal(signal.SIGINT, STOP.handle)
    signal.signal(signal.SIGTERM, STOP.handle)
    tty = sys.stdout.isatty()

    def progress(text):
        if tty:
            cols = shutil.get_terminal_size((120, 20)).columns
            print("\r" + text[:cols - 1].ljust(cols - 1), end="", flush=True)

    def save_state():
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(state_file)

    if not args.dry_run and not args.sample:
        clean_stale_temps(args.paths)
    files = find_files(args.paths, cfg, {} if (args.dry_run or args.sample) else state)
    if args.sample:
        files = files[:args.sample]
    elif args.limit:
        files = files[:args.limit]
    total_bytes = sum(s for _, s in files)
    print(f"{len(files)} file(s) to look at, {human(total_bytes)} in total.\n")

    counts = {"done": 0, "kept": 0, "failed": 0}
    saved = 0
    run_start = time.time()
    lf = open(ledger, "a", newline="")
    writer = csv.writer(lf)
    if new_ledger:
        writer.writerow(["time", "file", "result", "orig_bytes", "new_bytes", "seconds", "notes"])

    for n, (path, size) in enumerate(files, 1):
        if STOP.now or stop_flag.exists():
            print("\n[stop] Stop requested, finishing here.")
            break
        head = f"[{n}/{len(files)}]"
        print(f"{head} {path}  ({human(size)})")
        t0 = time.time()
        try:
            info = ffprobe(cfg["ffprobe"], path)
            plan = plan_file(path, info, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] cannot read: {exc}\n")
            counts["failed"] += 1
            if not args.dry_run and not args.sample:
                state[str(path)] = {"status": "unreadable", "mtime": int(path.stat().st_mtime), "reason": str(exc)}
                save_state()
            continue
        print(describe(plan))

        if args.dry_run:
            print()
            continue

        out_path = path.with_name(f"{TEMP_PREFIX}{path.stem}{SUFFIX}.mkv")
        final_path = path.with_name(f"{path.stem}{SUFFIX}.mkv")

        if args.sample:
            start = max(0.0, plan["duration"] / 2 - 30)
            length = min(60.0, plan["duration"])
            sample_out = state_dir / "sample.mkv"
            mode = plan["video_mode"]
            cmd = build_cmd(plan, cfg, sample_out, mode, sample=(start, length))
            rc, err = run_ffmpeg(cmd, length, f"  sample", progress)
            if rc != 0 and mode == "encode" and cfg["encoder"] == "qsv":
                cmd = build_cmd(plan, cfg, sample_out, mode, rate_mode="cqp", sample=(start, length))
                rc, err = run_ffmpeg(cmd, length, f"  sample (CQP)", progress)
            if tty:
                print()
            if rc != 0:
                print("  sample failed: " + (err[-1] if err else f"exit {rc}") + "\n")
                continue
            ratio = sample_out.stat().st_size / (size * length / plan["duration"]) if plan["duration"] else 0
            print(f"  projected size: {ratio * 100:.0f}% of original (~{human(size * ratio)}, saves ~{human(size * (1 - ratio))})\n")
            sample_out.unlink(missing_ok=True)
            continue

        attempts = [plan["video_mode"]]
        if plan["video_mode"] == "encode":
            attempts.append("copy")            # fallback if the re-encode is not smaller
        result, notes, new_size = None, [], 0
        for mode in attempts:
            if mode == "copy" and not plan["rule_change"]:
                break                          # nothing to gain from a copy pass
            rate_modes = ["icq", "cqp"] if (mode == "encode" and cfg["encoder"] == "qsv") else ["icq"]
            rc, err = 1, []
            for rate in rate_modes:
                cmd = build_cmd(plan, cfg, out_path, mode, rate_mode=rate)
                label = f"  {'encoding' if mode == 'encode' else 'remuxing'}"
                rc, err = run_ffmpeg(cmd, plan["duration"], label, progress)
                if tty:
                    print()
                if rc == 0 or STOP.now:
                    break
                if rate == "icq":
                    notes.append("ICQ refused, retried with CQP")
            if STOP.now:
                out_path.unlink(missing_ok=True)
                break
            if rc != 0:
                notes.append(f"{mode} failed: {(err[-1] if err else 'exit ' + str(rc))[:200]}")
                out_path.unlink(missing_ok=True)
                continue
            ok, why = verify(cfg, plan, out_path)
            if not ok:
                notes.append(f"{mode} check failed: {why}")
                out_path.unlink(missing_ok=True)
                continue
            new_size = out_path.stat().st_size
            smaller = new_size <= size * (1 - cfg["min_saving"])
            if smaller or (mode == "copy" and (new_size < size or plan["audio_required"])):
                result = "done"
                break
            notes.append(f"{mode} gave {human(new_size)} vs {human(size)}, not worth it")
            out_path.unlink(missing_ok=True)

        if STOP.now:
            break

        if result == "done":
            if final_path.exists():
                print(f"  [skip] {final_path.name} already exists, leaving both alone\n")
                out_path.unlink(missing_ok=True)
                counts["failed"] += 1
                continue
            os.replace(out_path, final_path)
            if args.delete_originals:
                path.unlink()
                where = "deleted"
            else:
                target = quarantine_target(cfg, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                where = f"moved to {target}"
            saved += size - new_size
            counts["done"] += 1
            print(f"  [done] {human(size)} -> {human(new_size)} "
                  f"({(1 - new_size / size) * 100:.0f}% smaller). Original {where}.")
        elif not notes or all("not worth it" in x for x in notes):
            # Already meets every rule and cannot be shrunk: just mark it done.
            if final_path.exists():
                print(f"  [skip] {final_path.name} already exists\n")
                counts["failed"] += 1
                continue
            os.replace(path, final_path)
            result, new_size = "kept", size
            counts["kept"] += 1
            print("  [kept] Already fine and could not be shrunk; renamed to -Clean.")
        else:
            result = "failed"
            counts["failed"] += 1
            print("  [FAILED] " + "; ".join(notes))
            state[str(path)] = {"status": "failed", "mtime": int(path.stat().st_mtime), "reason": "; ".join(notes)}
            save_state()

        secs = time.time() - t0
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), str(path), result, size, new_size,
                         int(secs), "; ".join(notes)])
        lf.flush()
        done_bytes = sum(s for _, s in files[:n])
        elapsed = time.time() - run_start
        eta = elapsed / done_bytes * (total_bytes - done_bytes) if done_bytes else 0
        print(f"  overall: {n}/{len(files)} files, saved {human(saved)} so far, "
              f"est. {hms(eta)} left for the rest\n")

    lf.close()
    if not args.dry_run and not args.sample:
        print("=" * 60)
        print(f"Converted {counts['done']}, already fine {counts['kept']}, failed/skipped {counts['failed']}.")
        print(f"Space saved this run: {human(saved)}   Ledger: {ledger}")
        if not args.delete_originals and counts["done"]:
            print(f"Originals are in {cfg['originals']} until you delete them.")


if __name__ == "__main__":
    main()
