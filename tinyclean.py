#!/usr/bin/env python3
"""
tinyclean - shrink and normalize a media library for TV playback.

Rules (in order of what matters most):
  Audio     One English track and one Japanese track, whichever exist, plus the
            release's default track when it is another language (the original
            language, e.g. Korean). Commentary is skipped.
            Each kept track follows the same rule. Stereo is kept as-is when it is already a
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
  Safety    Originals are never touched. Output goes to a "Clean" folder next to
            the source, written as a hidden temp file, checked (duration,
            streams), and only kept if it is smaller (or if the stereo rule
            required the change). Each Clean folder gets a README listing what
            happened to every file, plus .plexignore / .ignore so Plex and
            Jellyfin don't show the copies while you inspect them. A run stops
            before the NAS gets too full (--min-free-gb).
  Naming    <folder>/Clean/<original name>-Clean.mkv. Files already ending in
            -Clean, and files that already have a Clean copy, are skipped.

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
JAPANESE = {"jpn", "ja", "japanese"}
KEEP_AUDIO = [("English", ENGLISH), ("Japanese", JAPANESE)]   # kept in this order
CLEAN_DIR = "Clean"
UNTAGGED = {"", "und", "unk", "unknown", "none", "zxx"}
TEXT_SUB_CONVERT = {"mov_text": "srt"}          # codec -> target for MKV
DROP_SUB_CODECS = {"eia_608", "none", ""}       # not storable / not real subs
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
INTERLACED = {"tt", "bb", "tb", "bt"}
# Codecs the UHD 630 can decode on the GPU, and the pixel formats it accepts.
QSV_DECODERS = {"h264": "h264_qsv", "hevc": "hevc_qsv", "mpeg2video": "mpeg2_qsv",
                "vc1": "vc1_qsv", "vp9": "vp9_qsv"}
QSV_PIX_FMTS = {"h264": {"yuv420p", "yuvj420p"}, "mpeg2video": {"yuv420p"}, "vc1": {"yuv420p"},
                "hevc": {"yuv420p", "yuv420p10le"}, "vp9": {"yuv420p", "yuv420p10le"}}

# Encoding profiles, chosen per show (asked once, then remembered) or with --profile.
# quality: lower = better and bigger. preset: speed on the UHD 630 (faster ~12x, slow ~3x).
# Live action gets more bits (22) for fast motion; tested on Fast Five: slow gained nothing over faster.
PROFILES = {
    "animated": {"preset": "faster", "quality_hd": 24, "quality_sd": 23},
    "live":     {"preset": "faster", "quality_hd": 22, "quality_sd": 22},
}
DEFAULT_PROFILE = "live"     # used for unknown shows when nobody is there to answer
# A "show" is the first folder under one of these, whichever folder you point tinyclean at.
LIBRARY_ROOTS = ["/data/Episodes", "/data/Movies"]
SEASON_DIR = re.compile(r"^(season|series|s)[ ._-]*\d+|^specials$|^extras$", re.I)

DEFAULTS = {
    "max_height": 1080,
    "max_width": 1920,
    "copy_hevc_bpp": 0.06,   # H.265/AV1 below this bits-per-pixel is left alone
    "min_saving": 0.05,      # a re-encode must save at least 5% to be kept
    "audio_bitrate": "192k",
    "min_age_minutes": 60,   # skip files touched in the last hour (imports in progress)
    "min_free_gb": 500,      # never start a file that could push NAS free space below this
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
    """Choose the audio streams to keep: one per wanted language, else the main track."""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return []

    def main_tracks(group):
        mains = [s for s in group if "commentary" not in title_of(s) and not disp(s, "comment")]
        return mains or group

    def best(group):
        # An existing stereo/mono track beats a surround one; then the default-flagged one.
        group = sorted(group, key=lambda s: (int(s.get("channels", 2)) > 2, not disp(s, "default")))
        return group[0]

    kept = []
    for name, codes in KEEP_AUDIO:
        group = [s for s in audio if lang_of(s) in codes]
        if group:
            kept.append((best(main_tracks(group)), name))
    # Also keep the release's default track (almost always the original language),
    # e.g. Korean for Oldboy, unless that language is already kept or it is untagged.
    mains = main_tracks(audio)
    default = next((s for s in mains if disp(s, "default")), mains[0])
    lang = lang_of(default)
    covered = any(lang in codes for _, codes in KEEP_AUDIO)
    if not covered and not (is_untagged(default) and kept):
        same = [s for s in mains if lang_of(s) == lang]
        label = f"original language ({lang})" if lang and not is_untagged(default) else "main track"
        kept.insert(0, (best(same), label))
    return kept


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
    needs_scale = h > cfg["max_height"] or w > cfg["max_width"]
    if needs_scale:
        f = min(cfg["max_width"] / w, cfg["max_height"] / h)
        out_w, out_h = int(round(w * f / 2)) * 2, int(round(h * f / 2)) * 2
    else:
        out_w, out_h = w, h
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
            bits.append(f"{w}x{h}->{out_w}x{out_h}")
        if interlaced:
            bits.append("deinterlace")
        bits.append("10-bit" if ten_bit else "8-bit")
        q = cfg["quality_sd"] if h <= 576 else cfg["quality_hd"]
        bits.append(f"q{q}")
        bits.append(cfg["preset"] if cfg["encoder"] == "qsv" else cfg["x265_preset"])
        bits.append(f"{cfg.get('profile_name', '?')} profile")
        video_why = f"{v.get('codec_name')} {w}x{h} {bpp:.3f} bpp -> H.265 (" + ", ".join(bits) + ")"

    # ---- audio
    audio = []
    for a, a_why in pick_audio(streams):
        ch = int(a.get("channels", 2))
        if ch <= 2 and a.get("codec_name") in STEREO_COPY_CODECS:
            audio.append({"stream": a, "mode": "copy", "why": f"{a_why}: copy {a.get('codec_name')} {ch}ch"})
        elif ch <= 2:
            audio.append({"stream": a, "mode": "aac", "why": f"{a_why}: {a.get('codec_name')} {ch}ch -> AAC"})
        else:
            filt, what = nightmode_filter(a)
            audio.append({"stream": a, "mode": "downmix", "filter": filt,
                          "why": f"{a_why}: {a.get('codec_name')} {ch}ch -> AAC stereo, {what}"})
    # Default track: whichever kept track the source had as default, else the first.
    if audio:
        defaults = [i for i, a in enumerate(audio) if disp(a["stream"], "default")]
        for i, a in enumerate(audio):
            a["default"] = i == (defaults[0] if defaults else 0)

    # ---- subtitles
    subs, s_why = pick_subs(streams)
    all_subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio_count = sum(1 for s in streams if s.get("codec_type") == "audio")
    attachments = any(s.get("codec_type") == "attachment" for s in streams)

    # Does the file break a rule even if the video is fine?
    rule_change = (
        any(a["mode"] != "copy" for a in audio) or audio_count > len(audio)
        or len(subs) != len(all_subs) or any(s.get("codec_name") in TEXT_SUB_CONVERT for s in subs)
        or path.suffix.lower() != ".mkv")

    codec, pix = v.get("codec_name"), v.get("pix_fmt", "")
    gpu_decode = (codec in QSV_DECODERS and pix in QSV_PIX_FMTS.get(codec, set())
                  and not interlaced)  # scaling also runs on the GPU (vpp_qsv)

    return {
        "gpu_decode": gpu_decode, "fps": fps, "width": w, "out_w": out_w, "out_h": out_h,
        "path": path, "size": size, "duration": dur, "video": v, "video_mode": video_mode,
        "video_why": video_why, "hdr": hdr, "ten_bit": ten_bit, "needs_scale": needs_scale,
        "interlaced": interlaced, "height": h, "audio": audio, "subs": subs, "subs_why": s_why,
        "subs_dropped": len(all_subs) - len(subs), "attachments": attachments,
        "rule_change": rule_change, "audio_required": any(a["mode"] == "downmix" for a in audio),
    }


# -------------------------------------------------------------- ffmpeg cmd --


def build_cmd(plan, cfg, out_path, video_mode, rate_mode="icq", sample=None, decode="cpu", low_power=False):
    v = plan["video"]
    cmd = [cfg["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", "-fflags", "+genpts"]
    encode = video_mode == "encode"
    if encode and cfg["encoder"] == "qsv":
        # Same device setup Jellyfin uses: VA-API on the iGPU, Quick Sync on top.
        cmd += ["-init_hw_device", f"vaapi=va:{cfg['device']},driver=iHD",
                "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
    gpu = encode and cfg["encoder"] == "qsv" and decode == "gpu"
    if gpu:
        # Decode on the iGPU too: frames never leave the GPU (much faster at fast presets).
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
                "-c:v", QSV_DECODERS[plan["video"]["codec_name"]]]
    if sample:
        cmd += ["-ss", str(sample[0])]
    cmd += ["-i", str(plan["path"])]
    if sample:
        cmd += ["-t", str(sample[1])]

    cmd += ["-map", f"0:{v['index']}"]
    for a in plan["audio"]:
        cmd += ["-map", f"0:{a['stream']['index']}"]
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
            filters.append(f"scale={plan['out_w']}:{plan['out_h']}:flags=lanczos")
        q = cfg["quality_sd"] if plan["height"] <= 576 else cfg["quality_hd"]
        if cfg["encoder"] == "qsv":
            if not gpu:
                filters += ["format=p010le" if plan["ten_bit"] else "format=nv12",
                            "hwupload=extra_hw_frames=64"]
                cmd += ["-vf", ",".join(filters)]
            elif plan["needs_scale"]:
                # Resize on the iGPU too, so 4K never touches the CPU.
                cmd += ["-vf", f"vpp_qsv=w={plan['out_w']}:h={plan['out_h']}"]
            cmd += ["-c:v", "hevc_qsv", "-preset", cfg["preset"],
                    "-profile:v", "main10" if plan["ten_bit"] else "main"]
            if low_power:
                cmd += ["-low_power", "1"]
            # ICQ (constant quality) first; CQP if the driver refuses ICQ.
            cmd += ["-global_quality", str(q)] if rate_mode == "icq" else ["-q:v", str(q)]
        else:  # software x265, for testing or a no-GPU box
            filters.append("format=yuv420p10le" if plan["ten_bit"] else "format=yuv420p")
            cmd += ["-vf", ",".join(filters), "-c:v", "libx265", "-preset", cfg["x265_preset"],
                    "-crf", str(q), "-x265-params", "log-level=error"]
    else:
        cmd += ["-c:v", "copy"]

    # audio: each kept track gets its own settings
    for i, a in enumerate(plan["audio"]):
        if a["mode"] == "copy":
            cmd += [f"-c:a:{i}", "copy"]
        elif a["mode"] == "aac":
            cmd += [f"-c:a:{i}", "aac", f"-b:a:{i}", cfg["audio_bitrate"]]
        else:
            cmd += [f"-filter:a:{i}", a["filter"], f"-c:a:{i}", "aac",
                    f"-b:a:{i}", cfg["audio_bitrate"], f"-ac:a:{i}", "2"]
        cmd += [f"-disposition:a:{i}", "default" if a["default"] else "0"]

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


def run_ffmpeg(cmd, total_seconds, label, progress, src_fps=None):
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
                if done <= 0 and src_fps:
                    # Some files report no output time until the end; count frames instead.
                    try:
                        done = int(state.get("frame", 0)) / src_fps
                    except ValueError:
                        done = 0.0
                pct = min(100.0, done / total_seconds * 100) if total_seconds else 0.0
                speed = state.get("speed", "?").strip()
                if speed in ("N/A", "?", "") and done > 0 and now > started:
                    speed = f"{done / (now - started):.2f}x"
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
    want_audio = len(plan["audio"])
    got_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    if got_audio != want_audio:
        return False, f"expected {want_audio} audio track(s), got {got_audio}"
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


ALREADY_DONE = []


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
                dirnames[:] = sorted(d for d in dirnames
                                     if not skip_dir.match(d) and d.lower() != CLEAN_DIR.lower())
                for name in filenames:
                    candidates.append(Path(dirpath) / name)
        for p in candidates:
            if p.name.startswith(TEMP_PREFIX) or p.suffix.lower() not in VIDEO_EXTS:
                continue
            if p.stem.lower().endswith(SUFFIX.lower()):
                continue
            if (p.parent / CLEAN_DIR / f"{p.stem}{SUFFIX}.mkv").exists() or \
                    p.name in read_map(p.parent / CLEAN_DIR).values():
                ALREADY_DONE.append(p)        # already has a Clean copy
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


def find_unfinished(roots):
    """Hidden temp files: a conversion in progress elsewhere, or left over from a crash."""
    found = []
    for root in roots:
        root = Path(root)
        if root.is_file():
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.startswith(TEMP_PREFIX):
                    found.append(Path(dirpath) / name)
    return found


def original_for_temp(temp):
    """The original video a Clean/.tinyclean.<stem>-Clean.mkv temp file belongs to."""
    stem = temp.name[len(TEMP_PREFIX):]
    if stem.endswith(SUFFIX + ".mkv"):
        stem = stem[:-len(SUFFIX + ".mkv")]
    folder = temp.parent.parent if temp.parent.name == CLEAN_DIR else temp.parent
    for p in folder.iterdir() if folder.is_dir() else []:
        if p.is_file() and p.stem == stem and p.suffix.lower() in VIDEO_EXTS:
            return p
    return None


def clean_dir_for(path):
    """Create <folder>/Clean with ignore files so Plex/Jellyfin skip the copies."""
    d = path.parent / CLEAN_DIR
    if d.exists():
        return False
    d.mkdir()
    (d / ".plexignore").write_text("*\n")
    (d / ".ignore").write_text("")
    return True


def write_readme(folder, state):
    """Rewrite <folder>/Clean/README-tinyclean.txt from the state of every file in folder."""
    d = folder / CLEAN_DIR
    if not d.is_dir():
        return
    lines = ["tinyclean results for this folder. Originals are untouched.",
             "converted   = a smaller -Clean copy is in this folder",
             "fine        = original already meets the rules and could not shrink; no copy made",
             "failed      = left alone, see reason", ""]
    rows = sorted((Path(k).name, v) for k, v in state.items() if Path(k).parent == folder)
    for name, v in rows:
        st = v.get("status")
        if st == "done":
            lines.append(f"converted  {human(v['orig'])} -> {human(v['new'])}  {name}")
        elif st == "fine":
            lines.append(f"fine       {human(v.get('orig', 0))}  {name}")
        else:
            lines.append(f"failed     {name}  ({v.get('reason', '')[:150]})")
    tmp = d / ".README.tmp"
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(d / "README-tinyclean.txt")


# ------------------------------------------------------------------ main --


def show_key(path, roots):
    """The show a file belongs to: the first folder under a library root (or under the
    folder you pointed at). Season folders never count as a show of their own."""
    for lib in LIBRARY_ROOTS:
        try:
            rel = path.relative_to(lib)
        except ValueError:
            continue
        if len(rel.parts) > 1:
            return str(Path(lib) / rel.parts[0])
    for root in roots:
        root = Path(root)
        if root.is_file():
            root = root.parent
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > 1 and not SEASON_DIR.match(rel.parts[0]):
            return str(root / rel.parts[0])
        return str(root)
    return str(path.parent)


def ask_profiles(shows, profiles, interactive):
    """Ask animated/live for each show not seen before. Returns True if anything was asked."""
    unknown = sorted((s for s in shows if s not in profiles), key=lambda s: Path(s).name.lower())
    if not unknown:
        return False
    if not interactive:
        print(f"[profiles] {len(unknown)} show(s) have no profile yet; using '{DEFAULT_PROFILE}' for them this run.")
        return False
    print(f"{len(unknown)} show(s) need a profile. a = animated, l = live action, "
          f"A / L = same for all remaining, Enter = {DEFAULT_PROFILE}.")
    all_answer = None
    for show in unknown:
        if all_answer:
            profiles[show] = all_answer
            continue
        while True:
            ans = input(f"  {Path(show).name}: ").strip()
            if ans in ("A", "L"):
                all_answer = "animated" if ans == "A" else "live"
                profiles[show] = all_answer
                break
            if ans.lower() in ("a", "l", ""):
                profiles[show] = {"a": "animated", "l": "live", "": DEFAULT_PROFILE}[ans.lower()]
                break
            print("    please type a, l, A, L or press Enter")
    print()
    return True


RES_TOKENS = re.compile(r"(?i)(?<![0-9a-z])(4320p|2160p|1440p)(?![0-9a-z])")
RES_DIMS = re.compile(r"(?<!\d)(\d{4})x(\d{3,4})(?!\d)")
UHD_TAGS = re.compile(r"(?i)[ ._-]*(?<![0-9a-z])(4k|uhd)(?![0-9a-z])")


def scaled_name(stem, out_w, out_h):
    """Fix resolution words in a file name after scaling down (2160p -> 1080p etc.)."""
    new = RES_TOKENS.sub("1080p", stem)
    new = RES_DIMS.sub(lambda m: f"{out_w}x{out_h}" if int(m.group(1)) > 1920 or int(m.group(2)) > 1080
                       else m.group(0), new)
    new = UHD_TAGS.sub("", new)
    new = re.sub(r"\[\s*\]|\(\s*\)", "", new)
    new = re.sub(r"\s{2,}", " ", new).strip(" ._-")
    return new or stem


MAP_NAME = ".tinyclean-map.json"   # in each Clean folder: clean file name -> original file name


def read_map(clean):
    try:
        return json.loads((clean / MAP_NAME).read_text())
    except (OSError, ValueError):
        return {}


def write_map(clean, mapping):
    tmp = clean / ".tinyclean-map.tmp"
    tmp.write_text(json.dumps(mapping, indent=1, sort_keys=True))
    tmp.replace(clean / MAP_NAME)


def pairs_in(folder):
    """(original, clean copy) pairs in one folder's Clean subfolder."""
    clean = folder / CLEAN_DIR
    mapping = read_map(clean)
    pairs = []
    for c in sorted(clean.glob(f"*{SUFFIX}.mkv")):
        if c.name.startswith("."):
            continue
        orig_name = mapping.get(c.name)
        if orig_name and (folder / orig_name).is_file():
            pairs.append((folder / orig_name, c))
            continue
        stem = c.name[:-len(SUFFIX + ".mkv")]
        for o in folder.iterdir():
            if o.is_file() and o.stem == stem and o.suffix.lower() in VIDEO_EXTS:
                pairs.append((o, c))
                break
    return pairs


def replace_originals(roots, cfg, yes):
    """Swap each original for its verified Clean copy, after showing the list and asking."""
    pairs = []
    for root in roots:
        for dirpath, dirnames, _ in os.walk(root):
            if CLEAN_DIR in dirnames and (Path(dirpath) / CLEAN_DIR).is_dir():
                pairs += pairs_in(Path(dirpath))
            dirnames[:] = [d for d in dirnames if d != CLEAN_DIR and not re.match(r"^[._@#]", d)]
    if not pairs:
        print("No Clean copies with a matching original were found.")
        return
    orig_total = sum(o.stat().st_size for o, _ in pairs)
    clean_total = sum(c.stat().st_size for _, c in pairs)
    folders = sorted({str(o.parent) for o, _ in pairs})
    print(f"{len(pairs)} original(s) in {len(folders)} folder(s) have a Clean copy.")
    print("Every original below will be DELETED and replaced by the copy shown:\n")
    for f in folders:
        print(f"  {f}")
        for o, c in sorted((x for x in pairs if str(x[0].parent) == f), key=lambda x: x[0].name):
            print(f"    DELETE {human(o.stat().st_size):>9}  {o.name}")
            print(f"    KEEP   {human(c.stat().st_size):>9}  {c.name}")
    print()
    print(f"Originals: {human(orig_total)}  ->  Clean copies: {human(clean_total)}  "
          f"(frees {human(orig_total - clean_total)})")
    print("Each original will be DELETED and its Clean copy moved up into its place.")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not running interactively; add --yes to confirm.")
        if input("Type 'replace' to go ahead: ").strip() != "replace":
            print("Nothing changed.")
            return
    done = 0
    for o, c in pairs:
        name = c.name
        # Copies made before names were fixed: correct 2160p etc. from the real size.
        try:
            info = ffprobe(cfg["ffprobe"], c)
            v = next(s for s in info["streams"] if s.get("codec_type") == "video")
            w, h = int(v.get("width", 0)), int(v.get("height", 0))
            if w <= 1920 and h <= 1080:
                name = scaled_name(c.name[:-len(SUFFIX + ".mkv")], w, h) + SUFFIX + ".mkv"
        except Exception:  # noqa: BLE001
            pass
        target = o.parent / name
        if target.exists():
            print(f"  [skip] {target.name} already exists next to the original")
            continue
        o.unlink()
        os.replace(c, target)
        done += 1
        print(f"  replaced  {o.name}  ->  {target.name}")
    for f in folders:
        clean = Path(f) / CLEAN_DIR
        # Remove the emptied Clean folder, but never while a conversion is in progress in it.
        if clean.is_dir() and not any((p.suffix == ".mkv" and not p.name.startswith(".")) or p.name.startswith(TEMP_PREFIX)
                                      for p in clean.iterdir()):
            shutil.rmtree(clean, ignore_errors=True)
    print(f"\nReplaced {done} file(s). Rescan the library in Plex/Jellyfin, and Refresh in Sonarr/Radarr.")


WORKED = []   # the (rate, low_power) combination that last succeeded, tried first next time


def variants(plan, cfg, mode):
    """Ways to try an encode, best first: GPU decode then CPU decode; ICQ then CQP;
    normal then low-power encoder (newer Intel chips such as the N100 may need it)."""
    if mode != "encode" or cfg["encoder"] != "qsv":
        return [("cpu", "icq", False)]
    decodes = ["gpu", "cpu"] if plan["gpu_decode"] else ["cpu"]
    combos = [("icq", False), ("cqp", False), ("icq", True), ("cqp", True)]
    if WORKED:
        combos.remove(WORKED[0])
        combos.insert(0, WORKED[0])
    return [(d, r, lp) for d in decodes for r, lp in combos]


def describe(plan):
    lines = [f"  video : {plan['video_mode']} - {plan['video_why']}"]
    if not plan["audio"]:
        lines.append("  audio : none in source")
    for a in plan["audio"]:
        lines.append(f"  audio : {a['why']}{' (default)' if a['default'] and len(plan['audio']) > 1 else ''}")
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
    ap.add_argument("--min-free-gb", type=int, default=DEFAULTS["min_free_gb"], help="stop when free space would drop below this")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.tinyclean"))
    ap.add_argument("--encoder", choices=["qsv", "x265"], default="qsv")
    ap.add_argument("--device", default="/dev/dri/renderD128")
    ap.add_argument("--profile", choices=sorted(PROFILES), help="use this profile for every file (no questions)")
    ap.add_argument("--quality-hd", type=int, help="override the profile's HD quality")
    ap.add_argument("--quality-sd", type=int, help="override the profile's SD quality")
    ap.add_argument("--preset",
                    choices=["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                    help="override the profile's Quick Sync speed preset")
    ap.add_argument("--x265-preset", default="medium")
    ap.add_argument("--min-age", type=int, default=DEFAULTS["min_age_minutes"], help="minutes since last change before a file is touched")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="swap originals for their Clean copies in the given folders (asks first)")
    ap.add_argument("--yes", action="store_true", help="with --replace: don't ask (for scripts)")
    args = ap.parse_args()

    jf = "/usr/lib/jellyfin-ffmpeg/"
    cfg = dict(DEFAULTS)
    cfg.update({
        "ffmpeg": jf + "ffmpeg" if os.path.exists(jf + "ffmpeg") else "ffmpeg",
        "ffprobe": jf + "ffprobe" if os.path.exists(jf + "ffprobe") else "ffprobe",
        "encoder": args.encoder, "device": args.device,
        "overrides": {k: v for k, v in (("quality_hd", args.quality_hd), ("quality_sd", args.quality_sd),
                                         ("preset", args.preset)) if v is not None},
        "x265_preset": args.x265_preset, "min_age_minutes": args.min_age,
        "retry_failed": args.retry_failed, "min_free_gb": args.min_free_gb,
    })

    if args.replace:
        replace_originals(args.paths, cfg, args.yes)
        return

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

    in_progress = set()
    if not args.dry_run and not args.sample:
        unfinished = find_unfinished(args.paths)
        if unfinished:
            print(f"Found {len(unfinished)} unfinished conversion(s): another worker is busy with them, "
                  f"or a run stopped without cleaning up:")
            for t in unfinished:
                print(f"  {t.parent}/{t.name[len(TEMP_PREFIX):]}")
            redo = False
            if sys.stdin.isatty():
                redo = input("Delete these temp files and convert those videos again? [y/N] ").strip().lower() == "y"
            if redo:
                for t in unfinished:
                    t.unlink(missing_ok=True)
                print("Removed. They will be converted again.\n")
            else:
                in_progress = {o for o in map(original_for_temp, unfinished) if o}
                print("Leaving them alone and skipping those videos this run.\n")
    files = find_files(args.paths, cfg, {} if (args.dry_run or args.sample) else state)
    if not args.dry_run and not args.sample:
        free = shutil.disk_usage(args.paths[0] if Path(args.paths[0]).is_dir() else Path(args.paths[0]).parent).free
        print(f"NAS free space: {human(free)} (tinyclean keeps at least {cfg['min_free_gb']} GB free).")
        if free < cfg["min_free_gb"] * 1024 ** 3:
            sys.exit(f"Not starting: free space is already below {cfg['min_free_gb']} GB. "
                     f"Replace some originals or clear space first.")
    if in_progress:
        files = [(p, sz) for p, sz in files if p not in in_progress]
    if args.sample:
        files = files[:args.sample]
    elif args.limit:
        files = files[:args.limit]
    total_bytes = sum(s for _, s in files)
    if ALREADY_DONE:
        print(f"{len(ALREADY_DONE)} file(s) already have a Clean copy and are skipped.")
    print(f"{len(files)} file(s) to look at, {human(total_bytes)} in total.\n")

    profiles_file = state_dir / "profiles.json"
    profiles = json.loads(profiles_file.read_text()) if profiles_file.exists() else {}
    shows = list(dict.fromkeys(show_key(p, args.paths) for p, _ in files))
    if not args.profile and ask_profiles(shows, profiles, sys.stdin.isatty()):
        tmp = profiles_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, indent=1, sort_keys=True))
        tmp.replace(profiles_file)

    def file_cfg(path):
        name = args.profile or profiles.get(show_key(path, args.paths), DEFAULT_PROFILE)
        return {**cfg, **PROFILES[name], **cfg["overrides"], "profile_name": name}

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
            fcfg = file_cfg(path)
            plan = plan_file(path, info, fcfg)
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

        clean = path.parent / CLEAN_DIR
        out_path = clean / f"{TEMP_PREFIX}{path.stem}{SUFFIX}.mkv"
        final_path = clean / f"{path.stem}{SUFFIX}.mkv"

        if args.sample:
            start = max(0.0, plan["duration"] / 2 - 30)
            length = min(60.0, plan["duration"])
            sample_out = state_dir / "sample.mkv"
            mode = plan["video_mode"]
            rc, err = 1, []
            for decode, rate, lp in variants(plan, fcfg, mode):
                cmd = build_cmd(plan, fcfg, sample_out, mode, rate_mode=rate, sample=(start, length),
                                decode=decode, low_power=lp)
                rc, err = run_ffmpeg(cmd, length, f"  sample ({decode} decode{', low-power' if lp else ''})", progress, plan.get("fps"))
                if rc == 0 or STOP.now:
                    if rc == 0 and mode == "encode":
                        WORKED[:] = [(rate, lp)]
                    break
            if tty:
                print()
            if rc != 0:
                print("  sample failed: " + (err[-1] if err else f"exit {rc}") + "\n")
                continue
            ratio = sample_out.stat().st_size / (size * length / plan["duration"]) if plan["duration"] else 0
            print(f"  projected size: {ratio * 100:.0f}% of original (~{human(size * ratio)}, saves ~{human(size * (1 - ratio))})\n")
            sample_out.unlink(missing_ok=True)
            continue

        free = shutil.disk_usage(path.parent).free
        if free - size < cfg["min_free_gb"] * 1024 ** 3:
            print(f"\n[stop] Only {human(free)} free on the NAS; a copy of this file could push it under "
                  f"{cfg['min_free_gb']} GB. Clear some space (or lower --min-free-gb) and run again.")
            break
        created_clean = False
        if plan["video_mode"] == "encode" or plan["rule_change"]:
            created_clean = clean_dir_for(path)

        attempts = [plan["video_mode"]]
        if plan["video_mode"] == "encode":
            attempts.append("copy")            # fallback if the re-encode is not smaller
        result, notes, new_size = None, [], 0
        for mode in attempts:
            if mode == "copy" and not plan["rule_change"]:
                break                          # nothing to gain from a copy pass
            rc, err = 1, []
            for decode, rate, lp in variants(plan, fcfg, mode):
                cmd = build_cmd(plan, fcfg, out_path, mode, rate_mode=rate, decode=decode, low_power=lp)
                label = f"  {'encoding' if mode == 'encode' else 'remuxing'}"
                if mode == "encode" and cfg["encoder"] == "qsv":
                    label += f" ({decode} decode{', low-power' if lp else ''})"
                rc, err = run_ffmpeg(cmd, plan["duration"], label, progress, plan.get("fps"))
                if tty:
                    print()
                if rc == 0 or STOP.now:
                    if rc == 0 and mode == "encode" and cfg["encoder"] == "qsv":
                        WORKED[:] = [(rate, lp)]
                    break
                notes.append(f"retry: {decode}/{rate}{'/lp' if lp else ''} attempt failed ({(err[-1] if err else 'exit ' + str(rc))[:120]})")
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

        mtime = int(path.stat().st_mtime)
        if result == "done" and mode == "encode" and plan["needs_scale"]:
            final_path = clean / f"{scaled_name(path.stem, plan['out_w'], plan['out_h'])}{SUFFIX}.mkv"
        if result == "done":
            if final_path.exists():
                print(f"  [skip] {final_path.name} already exists in Clean, leaving it\n")
                out_path.unlink(missing_ok=True)
                counts["failed"] += 1
                continue
            os.replace(out_path, final_path)
            mapping = read_map(clean)
            mapping[final_path.name] = path.name
            write_map(clean, mapping)
            saved += size - new_size
            counts["done"] += 1
            state[str(path)] = {"status": "done", "mtime": mtime, "orig": size, "new": new_size}
            print(f"  [done] {human(size)} -> {human(new_size)} "
                  f"({(1 - new_size / size) * 100:.0f}% smaller), saved as Clean/{final_path.name}")
        elif all("not worth it" in x or x.startswith("retry:") for x in notes):
            result, new_size = "fine", size
            counts["kept"] += 1
            state[str(path)] = {"status": "fine", "mtime": mtime, "orig": size}
            print("  [fine] Already meets the rules and could not be shrunk; no copy made.")
        else:
            result = "failed"
            counts["failed"] += 1
            state[str(path)] = {"status": "failed", "mtime": mtime, "reason": "; ".join(notes)}
            print("  [FAILED] " + "; ".join(notes))
        save_state()
        write_readme(path.parent, state)
        # Remove a Clean folder this file's run created but left with nothing in it.
        if created_clean and result != "done" and clean.is_dir() and not any(p.suffix == ".mkv" and not p.name.startswith(".") for p in clean.iterdir()) \
                and not any(v.get("status") == "done" for k, v in state.items() if Path(k).parent == path.parent):
            shutil.rmtree(clean, ignore_errors=True)

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
        print(f"Space the Clean copies would save once originals are removed: {human(saved)}")
        print(f"Ledger: {ledger}")
        if counts["done"]:
            print("Originals are untouched. Check the Clean folders, then replace originals when you're happy.")


if __name__ == "__main__":
    main()
