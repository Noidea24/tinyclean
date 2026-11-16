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
  Video     H.265 on Intel Quick Sync. 4K is scaled to fit 1920x1080 (decode and
            resize on the iGPU). Interlaced DVD material is deinterlaced. 10-bit
            sources stay 10-bit. HDR10/HLG is re-encoded at 1080p and stays HDR
            (checked afterwards; if HDR is lost, the 4K video is copied instead).
            Dolby Vision with no HDR10 base layer is copied untouched. Already
            efficient H.265 is copied (remuxed), everything else becomes H.265.
  Safety    Originals are never touched. Every file gets a Clean copy so the
            whole library ends up in one consistent format. Output is written
            as a hidden temp file in a "Clean" folder next to the source,
            checked (duration, streams, HDR), and kept; a re-encode may be up
            to 20% bigger, beyond that the original video is remuxed instead.
            Each Clean folder gets a README listing what
            happened to every file, plus .plexignore / .ignore so Plex and
            Jellyfin don't show the copies while you inspect them. A run stops
            before the NAS gets too full (--min-free-gb).
  Naming    <folder>/Clean/<original name>-noidea.mkv, and every file carries a
            hidden TINYCLEAN tag. Files ending in -noidea (or the old -Clean),
            files with the tag (even if renamed later by Sonarr/Radarr), and
            files that already have a Clean copy are skipped.
  Organize  --organize names files (and optionally folders) from Plex's matches,
            as a dry run that saves a plan; --apply-plan carries it out and
            --undo puts it back. See the README.

Runs as a normal user that can write to the library. State, log and a CSV
ledger live in --state-dir. Ctrl+C stops cleanly (temp file removed). Touch
<state-dir>/STOP to finish the current file and then stop.
"""

import argparse
import csv
import fcntl
import json
import queue
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

VERSION = "8.5.2"
SUFFIX = "-noidea"                  # added to every finished file's name
OLD_SUFFIXES = ("-Clean",)          # names used by earlier versions; still count as done
ALL_SUFFIXES = (SUFFIX,) + OLD_SUFFIXES
TAG = "TINYCLEAN"                   # hidden tag inside every file tinyclean makes
PROGRESS_PREFIX = "@progress "      # --progress-lines: start of each JSON progress line


def done_suffix(stem):
    """The tinyclean suffix a file name (without extension) ends in, or None."""
    low = stem.lower()
    return next((x for x in ALL_SUFFIXES if low.endswith(x.lower())), None)


def strip_suffix(name):
    """'<stem><suffix>.mkv' -> '<stem>'."""
    stem = name[:-4] if name.lower().endswith(".mkv") else name
    sfx = done_suffix(stem)
    return stem[:-len(sfx)] if sfx else stem
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
HDR_NAMES = {"smpte2084": "HDR10", "arib-std-b67": "HLG"}
DV_TOKENS = re.compile(r"(?i)[ ._-]*(?<![0-9a-z])(dv|dovi|dolby[ ._-]?vision)(?![0-9a-z])")
INTERLACED = {"tt", "bb", "tb", "bt"}
# Codecs the UHD 630 can decode on the GPU, and the pixel formats it accepts.
QSV_DECODERS = {"h264": "h264_qsv", "hevc": "hevc_qsv", "mpeg2video": "mpeg2_qsv",
                "vc1": "vc1_qsv", "vp9": "vp9_qsv", "av1": "av1_qsv"}
# AV1 decodes on newer iGPUs (N100 and later). On the UHD 630 the GPU attempt
# fails quickly and tinyclean falls back to CPU decoding by itself.
QSV_PIX_FMTS = {"h264": {"yuv420p", "yuvj420p"}, "mpeg2video": {"yuv420p"}, "vc1": {"yuv420p"},
                "hevc": {"yuv420p", "yuv420p10le"}, "vp9": {"yuv420p", "yuv420p10le"},
                "av1": {"yuv420p", "yuv420p10le"}}

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
    "max_growth": 1.20,      # a re-encode may come out up to 20% bigger (consistency first)
    "audio_bitrate": "192k",
    "min_age_minutes": 60,   # skip files touched in the last hour (imports in progress)
    "min_free_gb": 500,      # never start a file that could push NAS free space below this
    "min_free_ram_mb": 256,  # stop ffmpeg before the container runs out of memory and locks up
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


def dovi_of(v):
    """Dolby Vision (profile, base-layer compatibility id) from a video stream, or None."""
    for sd in v.get("side_data_list", []) or []:
        if "dovi" in str(sd.get("side_data_type", "")).lower():
            return int(sd.get("dv_profile", -1)), int(sd.get("dv_bl_signal_compatibility_id", -1))
    return None


def first_frame_has_mastering(ffprobe_bin, path):
    """True if the first video frame carries HDR mastering-display data."""
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
             "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        frames = json.loads(out.stdout or "{}").get("frames", [])
    except Exception:  # noqa: BLE001
        return False
    return any("mastering" in str(sd.get("side_data_type", "")).lower()
               for f in frames for sd in f.get("side_data_list", []) or [])


def hdr_report(cfg, plan, out_path):
    """After an HDR encode: (ok, text). ok = the HDR signalling survived."""
    try:
        ov = next(s for s in ffprobe(cfg["ffprobe"], out_path)["streams"] if s.get("codec_type") == "video")
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read output ({exc})"
    v = plan["video"]
    if ov.get("color_transfer") != v.get("color_transfer") or \
            (v.get("color_primaries") and ov.get("color_primaries") != v.get("color_primaries")):
        return False, (f"HDR lost: output is {ov.get('color_transfer')}/{ov.get('color_primaries')}, "
                       f"source {v.get('color_transfer')}/{v.get('color_primaries')}")
    text = f"{HDR_NAMES.get(v.get('color_transfer'), 'HDR')} kept ({ov.get('pix_fmt')})"
    if v.get("color_transfer") == "smpte2084" and first_frame_has_mastering(cfg["ffprobe"], plan["path"]):
        if first_frame_has_mastering(cfg["ffprobe"], out_path):
            text += ", brightness metadata kept"
        else:
            text += ", brightness metadata missing (TVs use their defaults)"
    return True, text


def tag_of(info):
    tags = (info.get("format") or {}).get("tags") or {}
    return next((v for k, v in tags.items() if k.upper() == TAG), "")


def has_tag(info):
    return bool(tag_of(info))


def stream_seconds(stream):
    """A stream's own length in seconds, from ffprobe or Matroska's DURATION tag, or 0 if unknown."""
    try:
        value = float(stream.get("duration", 0) or 0)
    except ValueError:
        value = 0.0
    if value > 0:
        return value
    tags = stream.get("tags") or {}
    for key, text in tags.items():
        if key.upper().startswith("DURATION"):
            try:
                h, m, sec = str(text).split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            except ValueError:
                continue
    return 0.0


def video_seconds(info):
    """How long the main video track runs, or 0 if the file does not say.

    The file's header length can come from a commentary or subtitle track that runs past the
    film; tinyclean drops such tracks, so lengths are compared on the video, which it always keeps.
    """
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not disp(s, "attached_pic"):
            return stream_seconds(s)
    return 0.0


def video_continues(ffprobe_bin, path, after):
    """True if ``path`` has video after ``after`` seconds (more than 2 s past it).

    Some releases end their video before the length their header claims, often at the start of
    the credits, so a copy that stops there has everything the original has. This reads only the
    stretch just past ``after``. If ffprobe fails, the answer is True, so a mismatch still counts.
    """
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", "-read_intervals", f"{max(0.0, after - 1):.3f}%+120", str(path)],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return True
    if out.returncode != 0 and not out.stdout.strip():
        return True
    for line in out.stdout.split():
        try:
            if float(line.strip(",")) > after + 2:
                return True
        except ValueError:
            continue
    return False


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
    flagged = [s for s in mains if disp(s, "default")]
    if len(flagged) > 1 and kept:
        # Several tracks claim to be the default (e.g. Russian dub + English on a
        # web release): the signal is useless, so don't add an extra language.
        return kept
    default = flagged[0] if flagged else mains[0]
    lang = lang_of(default)
    covered = any(lang in codes for _, codes in KEEP_AUDIO)
    dub = re.search(r"(?i)\b(dub|dubbed|dubbing|mvo|dvo|avo|voice[- ]?over)\b", title_of(default))
    if dub and kept:
        return kept
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
    dovi = dovi_of(v)
    # Dolby Vision with no HDR10/HLG/SDR base layer (profile 5): only DV players
    # can show it right; re-encoding gives a green/purple picture. Never touch it.
    dv_only = dovi is not None and (dovi[1] == 0 or (dovi[0] == 5 and dovi[1] < 0))
    ten_bit = "10" in v.get("pix_fmt", "") or "12" in v.get("pix_fmt", "") or hdr
    needs_scale = h > cfg["max_height"] or w > cfg["max_width"]
    if needs_scale:
        f = min(cfg["max_width"] / w, cfg["max_height"] / h)
        out_w, out_h = int(round(w * f / 2)) * 2, int(round(h * f / 2)) * 2
    else:
        out_w, out_h = w, h
    interlaced = v.get("field_order", "") in INTERLACED
    efficient = v.get("codec_name") == "hevc" and bpp and bpp < cfg["copy_hevc_bpp"]

    if dv_only:
        video_mode, video_why = "copy", f"Dolby Vision profile {dovi[0]} with no HDR10 base (kept as-is)"
    elif hdr and not cfg.get("hdr_encode", True):
        video_mode, video_why = "copy", "HDR (kept as-is, --keep-hdr)"
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
        if hdr:
            bits.append(f"{HDR_NAMES.get(v.get('color_transfer'), 'HDR')} kept")
        if dovi:
            bits.append("Dolby Vision layer dropped")
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
        if len(defaults) > 1:
            # More than one flagged default: prefer English, then the first.
            eng = [i for i in defaults if lang_of(audio[i]["stream"]) in ENGLISH]
            defaults = eng or defaults
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
        "path": path, "size": size, "duration": dur, "video_seconds": video_seconds(info),
        "video": v, "video_mode": video_mode,
        "video_why": video_why, "hdr": hdr, "dovi": dovi, "ten_bit": ten_bit, "needs_scale": needs_scale,
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
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-max_muxing_queue_size", "9999",
            "-metadata", f"{TAG}=v{VERSION} {time.strftime('%Y-%m-%d')}"]

    # video
    if encode:
        filters = []
        if plan["interlaced"]:
            filters.append("bwdif=mode=send_frame")
        if plan["needs_scale"]:
            filters.append(f"scale={plan['out_w']}:{plan['out_h']}:flags=lanczos")
        q = cfg["quality_sd"] if plan["height"] <= 576 else cfg["quality_hd"]
        if plan["hdr"]:
            # Carry the HDR colour signalling into the new stream.
            cmd += ["-color_primaries", v.get("color_primaries") or "bt2020",
                    "-color_trc", v.get("color_transfer"),
                    "-colorspace", v.get("color_space") or "bt2020nc",
                    "-color_range", v.get("color_range") or "tv"]
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
                    "-crf", str(q), "-x265-params",
                    "log-level=error" + (":hdr10=1:hdr10-opt=1" if plan["hdr"] and v.get("color_transfer") == "smpte2084" else "")]
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
        self.procs = set()

    def handle(self, signum, frame):
        if self.now:
            os._exit(130)
        self.now = True
        print("\n[stop] Stopping: cleaning up the file(s) in progress. Press Ctrl+C again to force.", flush=True)
        for proc in list(self.procs):
            if proc.poll() is None:
                proc.terminate()


STOP = Stopper()


LOW_MEMORY = -99          # run_ffmpeg's return code when it stopped ffmpeg for memory
MEMORY = {"floor_mb": DEFAULTS["min_free_ram_mb"]}


def mem_available_mb():
    """Memory the container can still use, in MB, or None where /proc/meminfo has no figure.

    In an LXC container LXCFS reports the container's own limit here, with the page cache from
    reading videos off the NAS counted as available, so this is what matters for a lock-up.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def run_ffmpeg(cmd, total_seconds, label, progress, src_fps=None):
    """Run ffmpeg, drawing a progress line. Returns (returncode, last stderr lines).

    If the container's free memory stays under the floor for two checks in a row (2 s apart),
    ffmpeg is stopped and the return code is LOW_MEMORY. A container that runs out of memory and
    swap stops answering its own console until it is restarted, so a stopped file is the lesser harm.
    """
    err = deque(maxlen=40)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    STOP.procs.add(proc)
    low = {}

    def pump_err():
        for line in proc.stderr:
            err.append(line.rstrip())

    def watch_memory():
        strikes = 0
        while proc.poll() is None:
            free = mem_available_mb()
            strikes = strikes + 1 if free is not None and free < MEMORY["floor_mb"] else 0
            if strikes >= 2:
                low["free"] = free
                proc.terminate()
                return
            time.sleep(2)

    t = threading.Thread(target=pump_err, daemon=True)
    t.start()
    if MEMORY["floor_mb"] > 0 and mem_available_mb() is not None:
        threading.Thread(target=watch_memory, daemon=True).start()
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
    STOP.procs.discard(proc)
    if low:
        err.append(f"stopped: memory ran low ({low['free']} MB free, floor {MEMORY['floor_mb']} MB)")
        return LOW_MEMORY, list(err)
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
    src, out = sample_len or plan["duration"], duration_of(info)
    if not sample_len and plan.get("video_seconds") and video_seconds(info):
        # Compare the video tracks: a dropped commentary can make the header longer than the film.
        src, out = plan["video_seconds"], video_seconds(info)
    if src > 0 and abs(out - src) > max(2.0, src * 0.005):
        if not sample_len and out < src and not video_continues(cfg["ffprobe"], plan["path"], out):
            # A short copy is never accepted: the original may carry on after a gap of bad data,
            # and replacing it would lose that. Say what was found so it can be re-downloaded.
            return False, (f"the original looks damaged or incomplete: its video stops at {mmss(out)} "
                           f"but should run to {mmss(src)}; re-download it")
        return False, f"duration mismatch: source {src:.1f}s, output {out:.1f}s"
    if out_path.stat().st_size < 1024 * 1024 and src > 60:
        return False, "output suspiciously small"
    if plan["hdr"]:
        ov = next(s for s in streams if s.get("codec_type") == "video")
        if ov.get("color_transfer") != plan["video"].get("color_transfer"):
            return False, (f"HDR lost (output transfer {ov.get('color_transfer')}, "
                           f"source {plan['video'].get('color_transfer')})")
    return True, "ok"


# ---------------------------------------------------------------- library --


ALREADY_DONE = []


def has_clean_copy(p):
    """True only if a Clean copy of original p actually exists on disk."""
    clean = p.parent / CLEAN_DIR
    if any((clean / f"{p.stem}{x}.mkv").exists() for x in ALL_SUFFIXES):
        return True
    return any(orig == p.name and (clean / copy).exists() for copy, orig in read_map(clean).items())


def find_files(roots, cfg, state, include_done=False):
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
            if done_suffix(p.stem):
                continue
            if not include_done and has_clean_copy(p):
                ALREADY_DONE.append(p)        # already has a Clean copy
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime > min_age:
                continue
            prev = state.get(str(p))
            if prev and prev.get("mtime") == int(st.st_mtime) and prev.get("status") != "fine" \
                    and not cfg["retry_failed"]:
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
    stem = strip_suffix(stem)
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
             f"converted   = a {SUFFIX} copy is in this folder",
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


def ask_batch(upcoming, profiles, info):
    """Ask animated/live for the next few shows with one line of letters.
    upcoming: show keys in run order. info: show -> (files, bytes). Nothing is saved
    until you confirm, so a typo just means typing the line again."""
    width = max(len(Path(s).name) for s in upcoming)
    while True:
        print(f"\nNext {len(upcoming)} show(s) need a profile:")
        for i, show in enumerate(upcoming, 1):
            n, b = info.get(show, (0, 0))
            print(f"  {i:2d}. {Path(show).name:<{width}}  {n:4d} file(s), {human(b)}")
        ans = input(f"Type {len(upcoming)} letters in order, a = animated, l = live "
                    f"(spaces ok, e.g. {'al' * 2}...): ")
        letters = ans.replace(" ", "").replace(",", "").lower()
        if len(letters) != len(upcoming) or set(letters) - {"a", "l"}:
            bad = "".join(sorted(set(letters) - {"a", "l"}))
            why = f"'{bad}' is not a or l" if bad else f"got {len(letters)} letters"
            print(f"  Need exactly {len(upcoming)} letters, each a or l ({why}). Try again.")
            continue
        print()
        for i, (show, c) in enumerate(zip(upcoming, letters), 1):
            print(f"  {i:2d}. {Path(show).name:<{width}}  {'animated' if c == 'a' else 'live'}")
        ok = input("Correct? Enter = yes and start, r = type them again: ").strip().lower()
        if ok in ("", "y", "yes"):
            for show, c in zip(upcoming, letters):
                profiles[show] = "animated" if c == "a" else "live"
            print()
            return


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
# Every file tinyclean itself writes into a Clean folder (besides the copies): a folder holding
# only these is empty and can go; anything else in it means someone put it there, so it stays.
CLEAN_OWN_FILES = {MAP_NAME, ".tinyclean-map.tmp", "README-tinyclean.txt", ".plexignore", ".ignore"}


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
    for c in sorted(x for x in clean.glob("*.mkv") if done_suffix(x.stem)):
        if c.name.startswith("."):
            continue
        orig_name = mapping.get(c.name)
        if orig_name and (folder / orig_name).is_file():
            pairs.append((folder / orig_name, c))
            continue
        stem = strip_suffix(c.name)
        for o in folder.iterdir():
            if o.is_file() and o.stem == stem and o.suffix.lower() in VIDEO_EXTS:
                pairs.append((o, c))
                break
    return pairs


EPISODE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})|(?<![0-9])(\d{1,2})x(\d{2,3})(?![0-9])")


def episode_key(name):
    m = EPISODE.search(name)
    if not m:
        return None
    season, ep = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return f"S{int(season):02d}E{int(ep):02d}"


def summarize(cfg, path):
    """Short facts about a video for the replace check."""
    info = ffprobe(cfg["ffprobe"], path)
    st = info.get("streams", [])
    v = next((x for x in st if x.get("codec_type") == "video" and not disp(x, "attached_pic")), {})
    audio = [x for x in st if x.get("codec_type") == "audio"]
    return {
        "dur": video_seconds(info) or duration_of(info),
        "video": f"{v.get('codec_name', '?')} {v.get('width', '?')}x{v.get('height', '?')}",
        "w": int(v.get("width", 0) or 0), "h": int(v.get("height", 0) or 0),
        "hdr": v.get("color_transfer", "") in HDR_TRANSFERS,
        "audio": ", ".join(f"{lang_of(a) or 'und'} {a.get('channels', '?')}ch" for a in audio) or "none",
        "audio_n": len(audio), "max_ch": max([int(a.get("channels", 0) or 0) for a in audio] or [0]),
        "subs": sum(1 for x in st if x.get("codec_type") == "subtitle"),
    }


def mmss(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def check_pair(cfg, o, c):
    """(problems, one-line facts) comparing an original with its Clean copy."""
    try:
        a, b = summarize(cfg, o), summarize(cfg, c)
    except Exception as exc:  # noqa: BLE001
        return [f"cannot read: {exc}"], ""
    problems = []
    if a["dur"] > 0 and abs(a["dur"] - b["dur"]) > max(2.0, a["dur"] * 0.005):
        problems.append(f"length differs ({mmss(a['dur'])} vs {mmss(b['dur'])})")
    if not b["w"]:
        problems.append("no video in copy")
    if b["w"] > 1920 or b["h"] > 1080:
        problems.append("copy is bigger than 1080p (made by an old version?)")
    if a["audio_n"] and not b["audio_n"]:
        problems.append("copy has no audio")
    if b["max_ch"] > 2:
        problems.append("copy still has surround audio")
    if a["hdr"] and not b["hdr"]:
        problems.append("HDR lost")
    facts = (f"{mmss(a['dur'])} = {mmss(b['dur'])}  |  {a['video']} -> {b['video']}{'  HDR' if b['hdr'] else ''}"
             f"  |  audio {a['audio']} -> {b['audio']}  |  subs {a['subs']} -> {b['subs']}")
    return problems, facts


def remove_empty_clean(clean):
    """Remove a Clean folder that holds nothing but tinyclean's own map file.
    Anything else in it (a video, a conversion in progress, a file someone put there) keeps it."""
    if not clean.is_dir():
        return False
    own = CLEAN_OWN_FILES
    entries = list(clean.iterdir())
    if any(e.name not in own or not e.is_file() for e in entries):
        return False
    for e in entries:
        e.unlink(missing_ok=True)
    try:
        clean.rmdir()
    except OSError:
        return False
    return True


def replace_originals(roots, cfg, yes, check_only=False):
    """Check every original against its Clean copy, show the list, ask, then swap.
    Copies that fail a check are never used. check_only: report, change nothing."""
    pairs, leftovers, all_videos = [], [], []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            folder = Path(dirpath)
            paired = pairs_in(folder) if (folder / CLEAN_DIR).is_dir() else []
            pairs += paired
            have = {o.name for o, _ in paired}
            for name in filenames:
                f = folder / name
                if f.suffix.lower() in VIDEO_EXTS and not name.startswith("."):
                    all_videos.append(f)
                    if name not in have and not done_suffix(f.stem):
                        leftovers.append(f)
            dirnames[:] = [d for d in dirnames if d != CLEAN_DIR and not re.match(r"^[._@#]", d)]
    if pairs:
        print(f"Checking {len(pairs)} original(s) against their Clean copies (length, video, audio, subtitles)...")
    results = {}
    for i, (o, c) in enumerate(pairs, 1):
        if sys.stdout.isatty():
            print(f"\r  {i}/{len(pairs)}", end="", flush=True)
        results[o] = check_pair(cfg, o, c)
    if sys.stdout.isatty():
        print("\r" + " " * 20 + "\r", end="")

    # Same episode more than once in a show (two copies of S01E03 in different files or folders).
    by_show = {}
    for f in all_videos:
        k = episode_key(f.name)
        if k:
            # Group per show: the first folder under the library (or under the folder given).
            base = next((Path(r) for r in roots if str(f).startswith(str(r))), f.parent)
            lib = library_root(base)
            try:
                top = lib / f.relative_to(lib).parts[0]
            except (ValueError, IndexError):
                top = base
            by_show.setdefault((str(top), k), []).append(f)
    dupes = {k: v for k, v in by_show.items() if len(v) > 1}

    def print_dupes():
        if not dupes:
            return
        print(f"WARNING - the same episode appears more than once ({len(dupes)} episode(s)):")
        for (top, k), files_ in sorted(dupes.items()):
            print(f"  {Path(top).name} {k}:")
            for f in files_:
                print(f"    {f.relative_to(top) if str(f).startswith(top) else f}")
        print(f"  Sort these out first (keep one copy of each), or you will end up with duplicate {SUFFIX} files.\n")

    if not pairs:
        print_dupes()
        print("No Clean copies with a matching original were found.")
        if leftovers:
            print(f"{len(leftovers)} video(s) in these folders have no Clean copy yet.")
        return

    good = [(o, c) for o, c in pairs if not results[o][0]]
    bad = [(o, c) for o, c in pairs if results[o][0]]
    folders = sorted({str(o.parent) for o, _ in pairs})
    print(f"{len(pairs)} original(s) in {len(folders)} folder(s) have a Clean copy: "
          f"{len(good)} passed the checks, {len(bad)} did not.")
    print("Each original that passed will be DELETED and replaced by the copy shown:\n")
    for f in folders:
        print(f"  {f}")
        for o, c in sorted((x for x in pairs if str(x[0].parent) == f), key=lambda x: x[0].name):
            problems, facts = results[o]
            print(f"    {'SKIP  ' if problems else 'DELETE'} {human(o.stat().st_size):>9}  {o.name}")
            print(f"    {'COPY  ' if problems else 'KEEP  '} {human(c.stat().st_size):>9}  {c.name}")
            if problems:
                print(f"      PROBLEM  {'; '.join(problems)}  -> this one is left alone")
            print(f"      ok       {facts}" if not problems and facts else f"               {facts}" if facts else "")
    print()
    print_dupes()
    if leftovers:
        print(f"Not touched - {len(leftovers)} video(s) in these folders have no Clean copy yet:")
        for f in leftovers[:20]:
            print(f"    {f}")
        if len(leftovers) > 20:
            print(f"    ... and {len(leftovers) - 20} more")
        print()
    orig_total = sum(o.stat().st_size for o, _ in good)
    clean_total = sum(c.stat().st_size for _, c in good)
    print(f"Will replace {len(good)} file(s): originals {human(orig_total)} -> Clean copies {human(clean_total)} "
          f"(frees {human(orig_total - clean_total)})")
    if bad:
        print(f"Will NOT touch {len(bad)} file(s) whose copy failed a check (see PROBLEM lines).")
    if check_only:
        print("Check only: nothing changed.")
        return
    if not good:
        print("Nothing to replace.")
        return
    print("Deleted originals do not go to the Synology recycle bin.")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not running interactively; add --yes to confirm.")
        if input("Type 'replace' to go ahead: ").strip() != "replace":
            print("Nothing changed.")
            return
    pairs = good
    done = 0
    for o, c in pairs:
        name = strip_suffix(c.name) + SUFFIX + ".mkv"
        # Copies made before names were fixed: correct 2160p etc. from the real size.
        try:
            info = ffprobe(cfg["ffprobe"], c)
            v = next(s for s in info["streams"] if s.get("codec_type") == "video")
            w, h = int(v.get("width", 0)), int(v.get("height", 0))
            if w <= 1920 and h <= 1080:
                name = scaled_name(strip_suffix(c.name), w, h) + SUFFIX + ".mkv"
        except Exception:  # noqa: BLE001
            pass
        target = o.parent / name
        if target.exists():
            print(f"  [skip] {target.name} already exists next to the original")
            continue
        # Copy into place first, original deleted second: if either step fails, no video is lost.
        try:
            os.replace(c, target)
        except OSError as exc:
            print(f"  [skip] could not move {c.name} into place ({exc}); original kept")
            continue
        try:
            o.unlink()
        except OSError as exc:
            print(f"  [note] {target.name} is in place, but {o.name} could not be deleted ({exc}); remove it by hand")
        done += 1
        print(f"  replaced  {o.name}  ->  {target.name}")
    for f in folders:
        remove_empty_clean(Path(f) / CLEAN_DIR)
    print(f"\nReplaced {done} file(s). Rescan the library in Plex/Jellyfin, and Refresh in Sonarr/Radarr.")
    for lib in {library_root(r) for r in roots}:
        write_status(lib)


def write_tag(cfg, path):
    """Add the hidden TINYCLEAN tag to an existing MKV in place (needs mkvpropedit).
    Keeps the file's other global tags. Returns '' on success, else the reason."""
    tool = shutil.which("mkvpropedit")
    if not tool:
        return "mkvpropedit not installed"
    try:
        info = ffprobe(cfg["ffprobe"], path)
    except Exception as exc:  # noqa: BLE001
        return f"cannot read: {exc}"
    if has_tag(info):
        return ""
    keep = {k: v for k, v in ((info.get("format") or {}).get("tags") or {}).items()
            if k.upper() not in ("ENCODER", "CREATION_TIME", "DURATION", "TITLE")}
    keep[TAG] = f"v{VERSION} {time.strftime('%Y-%m-%d')} (tagged later)"
    from xml.sax.saxutils import escape
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<Tags><Tag><Targets/>"]
    xml += [f"<Simple><Name>{escape(k)}</Name><String>{escape(str(v))}</String></Simple>" for k, v in keep.items()]
    xml.append("</Tag></Tags>")
    xf = path.parent / f".tinyclean-tags.{os.getpid()}.xml"
    try:
        xf.write_text("\n".join(xml))
        r = subprocess.run([tool, str(path), "--tags", f"global:{xf}"], capture_output=True, text=True, timeout=300)
        return "" if r.returncode in (0, 1) else (r.stdout + r.stderr).strip()[-200:]
    finally:
        xf.unlink(missing_ok=True)


def rename_suffix(roots, cfg, yes):
    """Give every finished file the current suffix (-noidea). Files ending in an old suffix
    (-Clean) are renamed and tagged. Files tinyclean made that lost the suffix, for example
    when Sonarr or Radarr renamed them, are found by their hidden tag and get it back.
    Lists everything and asks before changing anything."""
    plan, retag = [], set()
    looked = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not re.match(r"^[._@#]", d) and d.lower() != CLEAN_DIR.lower()]
            for name in filenames:
                f = Path(dirpath) / name
                if name.startswith(".") or f.suffix.lower() != ".mkv":
                    continue
                sfx = done_suffix(f.stem)
                if sfx and sfx != SUFFIX:
                    plan.append((f, f.with_name(f.stem[:-len(sfx)] + SUFFIX + ".mkv")))
                    retag.add(f)
                elif not sfx:
                    looked += 1
                    if sys.stdout.isatty():
                        print(f"\r  checking files for tinyclean's tag: {looked}", end="", flush=True)
                    try:
                        made_here = has_tag(ffprobe(cfg["ffprobe"], f))
                    except Exception:  # noqa: BLE001
                        made_here = False
                    if made_here:
                        plan.append((f, f.with_name(f.stem + SUFFIX + ".mkv")))
    if looked and sys.stdout.isatty():
        print()
    if not plan:
        print(f"Nothing to rename: no files ending in {', '.join(OLD_SUFFIXES)}, and no tinyclean "
              f"files missing {SUFFIX}.")
        return
    tagging = bool(shutil.which("mkvpropedit"))
    folders = sorted({str(a.parent) for a, _ in plan})
    print(f"{len(plan)} file(s) in {len(folders)} folder(s) will be renamed to end in {SUFFIX}"
          + (" and tagged as tinyclean output." if tagging else "."))
    for fo in folders:
        print(f"  {fo}")
        for a, b in sorted((x for x in plan if str(x[0].parent) == fo), key=lambda x: x[0].name):
            print(f"    {a.name}\n      -> {b.name}")
    print()
    if not tagging:
        print("Note: mkvpropedit is not installed, so files are renamed but not tagged.\n"
              "      (apt install mkvtoolnix, then run this again to tag them.)")
    print("Nothing is deleted; files are only renamed (and tagged).")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not running interactively; add --yes to confirm.")
        if input("Type 'rename' to go ahead: ").strip() != "rename":
            print("Nothing changed.")
            return
    done = tagged = 0
    for i, (a, b) in enumerate(plan, 1):
        if b.exists():
            print(f"  [skip] {b.name} already exists")
            continue
        if tagging and a in retag:
            why = write_tag(cfg, a)
            if why:
                print(f"  [tag failed] {a.name}: {why}")
            else:
                tagged += 1
        os.replace(a, b)
        done += 1
        if a.parent.name == CLEAN_DIR:
            m = read_map(a.parent)
            if a.name in m:
                m[b.name] = m.pop(a.name)
                write_map(a.parent, m)
        if sys.stdout.isatty():
            print(f"\r  {i}/{len(plan)}", end="", flush=True)
    print(f"\nRenamed {done} file(s), tagged {tagged}. Rescan Plex/Jellyfin; Refresh in Sonarr/Radarr.")
    for lib in {library_root(r) for r in roots}:
        write_status(lib)


STATUS_NAME = "_tinyclean-status.txt"   # at the top of each library folder


def library_root(path):
    """The library folder a path belongs to (/data/Episodes, /data/Movies), else the path."""
    path = Path(path)
    for lib in LIBRARY_ROOTS:
        try:
            path.relative_to(lib)
            return Path(lib)
        except ValueError:
            continue
    return path if path.is_dir() else path.parent


def scan_status(root):
    """Look at the files on disk (shared by every worker) and sum up each show/movie."""
    root = Path(root)
    skip = re.compile(r"^[._@#]")
    rows = {}
    try:
        top = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return rows
    for item in top:
        if skip.match(item.name):
            continue
        r = {"total": 0, "todo": 0, "ready": 0, "replaced": 0, "busy": 0, "failed": 0,
             "orig_bytes": 0, "clean_bytes": 0}
        walk = [(str(root), [], [item.name])] if item.is_file() else os.walk(item)
        for dirpath, dirnames, filenames in walk:
            dirnames[:] = [d for d in dirnames if not skip.match(d) and d != CLEAN_DIR]
            folder = Path(dirpath)
            clean = folder / CLEAN_DIR
            have_clean = clean.is_dir()
            paired = {o.name: c for o, c in pairs_in(folder)} if have_clean else {}
            if have_clean:
                for t in clean.iterdir():
                    if t.name.startswith(TEMP_PREFIX):
                        r["busy"] += 1
                readme = clean / "README-tinyclean.txt"
                if readme.is_file():
                    try:
                        r["failed"] += sum(1 for ln in readme.read_text().splitlines()
                                           if re.match(r"failed\s+[^=\s]", ln))
                    except OSError:
                        pass
            for name in filenames:
                f = folder / name
                if f.suffix.lower() not in VIDEO_EXTS or name.startswith("."):
                    continue
                r["total"] += 1
                if done_suffix(f.stem):
                    r["replaced"] += 1
                elif name in paired:
                    r["ready"] += 1
                    try:
                        r["orig_bytes"] += f.stat().st_size
                        r["clean_bytes"] += paired[name].stat().st_size
                    except OSError:
                        pass
                else:
                    r["todo"] += 1
        if r["total"]:
            rows[item.name] = r
    return rows


def status_text(root, rows):
    groups = {"busy": [], "review": [], "partial": [], "attention": [], "replaced": [], "new": []}
    for name, r in rows.items():
        if r["busy"]:
            groups["busy"].append(name)
        elif r["todo"] == 0 and r["ready"] == 0:
            groups["replaced"].append(name)
        elif r["todo"] == 0:
            groups["review"].append(name)
        elif r["failed"] and r["todo"] <= r["failed"]:
            groups["attention"].append(name)
        elif r["ready"] or r["replaced"]:
            groups["partial"].append(name)
        else:
            groups["new"].append(name)
    width = min(48, max([len(n) for n in rows] + [10]))
    ready_o = sum(r["orig_bytes"] for r in rows.values())
    ready_c = sum(r["clean_bytes"] for r in rows.values())

    def line(n, extra=""):
        r = rows[n]
        done = r["ready"] + r["replaced"]
        sizes = (f"  {human(r['orig_bytes']):>9} -> {human(r['clean_bytes']):>9}  frees {human(r['orig_bytes'] - r['clean_bytes'])}"
                 if r["ready"] else "")
        return f"  {n[:width]:<{width}}  {done:4d}/{r['total']:<4d}{sizes}{extra}"

    out = [f"tinyclean status for {root}", f"Updated {time.strftime('%Y-%m-%d %H:%M')}"
           " (refreshed during runs; tinyclean --status \"<folder>\" refreshes it by hand)", "",
           f"{len(rows)} folders: {len(groups['review'])} ready to review, {len(groups['busy'])} converting now, "
           f"{len(groups['partial'])} partly done, {len(groups['attention'])} need attention, "
           f"{len(groups['replaced'])} replaced, {len(groups['new'])} not started.",
           f"Clean copies waiting for review: {human(ready_o)} of originals -> {human(ready_c)} "
           f"(replacing them would free {human(ready_o - ready_c)}).", "",
           "Columns: done/total files, then originals -> Clean copies for files waiting for review.", ""]
    sections = [
        ("review", "READY TO REVIEW - every file has a Clean copy. Check a few, then:\n"
                   f"  tinyclean --replace \"{root}/<folder name>\"", ""),
        ("busy", "CONVERTING NOW", "  (converting)"),
        ("partial", "PARTLY DONE - some files converted or replaced, more to go", ""),
        ("attention", "NEEDS ATTENTION - only failed files left, see Clean/README-tinyclean.txt", ""),
        ("replaced", f"REPLACED - finished, every file is a {SUFFIX} file", ""),
        ("new", "NOT STARTED", ""),
    ]
    for key, title, extra in sections:
        if not groups[key]:
            continue
        out.append(title)
        for n in groups[key]:
            fail = f"  ({rows[n]['failed']} failed)" if rows[n]["failed"] else ""
            out.append(line(n, extra + fail))
        out.append("")
    return "\n".join(out) + "\n"


def write_status(root, echo=False, working_on=None):
    """working_on: the file this worker is converting right now (counts as converting)."""
    root = Path(root)
    try:
        rows = scan_status(root)
        for w in ([working_on] if isinstance(working_on, (str, Path)) else (working_on or [])):
            try:
                top = Path(w).relative_to(root).parts[0]
                if top in rows:
                    rows[top]["busy"] += 1
            except ValueError:
                pass
        text = status_text(root, rows)
        tmp = root / f".{STATUS_NAME}.{os.getpid()}.tmp"
        tmp.write_text(text)
        tmp.replace(root / STATUS_NAME)
    except OSError as exc:
        text = f"could not write status for {root}: {exc}"
    if echo:
        print(text)


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


# ---------------------------------------------------------------- organize --
# Rename, restructure or rebuild the library in a standard layout, from Plex's matches.
# Every run is a dry run that saves a plan; --apply-plan carries out exactly that plan,
# and every applied run leaves an undo log.

ORGANIZE_MODES = ("rename", "restructure", "newtree")
TEMPLATES = {
    "movie_folder": "{title} ({year}) {tmdb-{tmdb}}",
    "movie_file": "{title} ({year}) {edition-{edition}}",
    "show_folder": "{show} ({year}) {tvdb-{tvdb}}",
    "season_folder": "Season {season}",
    "episode_file": "{show} ({year}) - S{season}E{episode} - {episode_title}",
}
TEMPLATE_TOKENS = {"title", "year", "imdb", "tmdb", "tvdb", "show", "season", "episode",
                   "episode_title", "resolution", "edition"}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".nfo"}
PLAN_MAX_AGE_HOURS = 24
ENV_FILE = "/etc/mediatool/mediatool.env"
_EMPTY = "\x00"
_TOKEN = re.compile(r"\{(\w+)(?::(\d))?\}")


def template_problems(template):
    """Tokens in ``template`` that tinyclean does not know, as a list of names."""
    return [m.group(1) for m in _TOKEN.finditer(template) if m.group(1) not in TEMPLATE_TOKENS]


def render(template, values):
    """Fill a name template. A token without a value disappears with the brackets around it,
    characters not allowed in names are replaced, and spacing and separators are tidied."""
    def token(m):
        name, width = m.group(1), m.group(2)
        if name not in TEMPLATE_TOKENS:
            return m.group(0)
        value = values.get(name)
        if value in (None, ""):
            return _EMPTY
        if name in ("season", "episode") and str(value).isdigit():
            return str(int(value)).zfill(int(width) if width else 2)
        return str(value)
    out = _TOKEN.sub(token, template)
    # A bracket group holding an empty token goes entirely: "({imdb})" or "{tmdb-{tmdb}}".
    out = re.sub(r"[(\[{][^()\[\]{}" + _EMPTY + r"]*" + _EMPTY + r"[^()\[\]{}]*[)\]}]", "", out)
    out = out.replace(_EMPTY, "")
    out = out.replace(": ", " - ").replace(":", "-")
    out = re.sub(r"[\\/]", "-", out)
    out = re.sub(r'[*?"<>|]', "", out)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"( - )+", " - ", out)
    out = re.sub(r"^\s*-\s*|\s*-\s*$", "", out)
    return out.strip(" .")


def load_env(path):
    """KEY=VALUE lines from a file only root and the media group can read. Missing file: {}."""
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


class Plex:
    """Reads Plex's library over its web API. Only reads; the token is sent as a header, never shown."""

    def __init__(self, url, token, timeout=60):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path, **params):
        import urllib.parse
        import urllib.request
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}{path}" + (f"?{query}" if query else ""),
                                     headers={"Accept": "application/json", "X-Plex-Token": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r).get("MediaContainer", {})

    def all(self, path, **params):
        """Every item, fetched 500 at a time."""
        items, start = [], 0
        while True:
            c = self.get(path, **params, **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 500})
            batch = c.get("Metadata", [])
            items += batch
            start += len(batch)
            if not batch or start >= int(c.get("totalSize", c.get("size", start))):
                return items

    def sections(self):
        return [d for d in self.get("/library/sections").get("Directory", []) if d.get("type") in ("movie", "show")]


def plex_ids(meta):
    """{'imdb': 'tt..', 'tmdb': '..', 'tvdb': '..'} from an item's Guid list; {} when unmatched."""
    ids = {}
    for g in meta.get("Guid", []) or []:
        source, _, value = str(g.get("id", "")).partition("://")
        if source in ("imdb", "tmdb", "tvdb") and value:
            ids[source] = value
    return ids


def resolution_label(media):
    r = str(media.get("videoResolution", "")).lower()
    return {"4k": "2160p", "sd": "SD"}.get(r, f"{r}p" if r.isdigit() else "")


def path_mapper(pairs):
    """Turn Plex's paths into this machine's, from 'plex=local' pairs, longest prefix first."""
    pairs = sorted(pairs, key=lambda p: -len(p[0]))

    def mapped(path):
        for plex_prefix, local_prefix in pairs:
            if path == plex_prefix or path.startswith(plex_prefix.rstrip("/") + "/"):
                return local_prefix.rstrip("/") + path[len(plex_prefix.rstrip("/")):]
        return path
    return mapped


def read_library(plex, mapped, ignore=(), info=None):
    """Every video Plex knows, as entries: file, section root, kind, folder group and template values.
    Also returns files Plex knows but that cannot be named cleanly, with the reason.
    ``ignore``: library titles to leave out. ``info``: filled with {file: {title, key, section}} for every
    file, for links back to Plex."""
    entries, skipped = [], []
    info = {} if info is None else info
    ignore = {t.strip().lower() for t in ignore if t.strip()}
    for section in plex.sections():
        if section.get("title", "").lower() in ignore:
            continue
        roots = [Path(mapped(loc["path"])) for loc in section.get("Location", [])]
        if roots and not any(r.is_dir() for r in roots):
            continue                    # not mounted on this machine (a library kept elsewhere)
        key = section["key"]

        def root_of(f):
            return next((r for r in roots if f == r or r in f.parents), None)

        if section["type"] == "movie":
            for meta in plex.all(f"/library/sections/{key}/all", type=1, includeGuids=1):
                parts = [(m, p) for m in meta.get("Media", []) for p in m.get("Part", [])]
                # Plex keeps a moved or deleted file as an "unavailable" copy until its trash is emptied.
                # Those are not real second versions: drop them while another part is still on disk.
                present = [(m, p) for m, p in parts if Path(mapped(p["file"])).exists()]
                if present and len(present) < len(parts):
                    parts = present
                ids = plex_ids(meta)
                for media, part in parts:
                    f = Path(mapped(part["file"]))
                    info[str(f)] = {"title": meta.get("title"), "year": meta.get("year"),
                                    "key": meta.get("ratingKey"), "section": key, "kind": "movie"}
                    if len(parts) > 1:
                        skipped.append((f, "several files or versions for one movie in Plex"))
                        continue
                    if not ids:
                        skipped.append((f, "not matched in Plex"))
                        continue
                    root = root_of(f)
                    if root is None:
                        skipped.append((f, "outside the Plex library folders (check PLEX_PATH_MAP)"))
                        continue
                    entries.append({"file": f, "root": root, "kind": "movie", "group": f.relative_to(root).parts[0],
                                    "section": key,
                                    "values": {"title": meta.get("title"), "year": meta.get("year"),
                                               "edition": meta.get("editionTitle"),
                                               "resolution": resolution_label(media), **ids}})
        else:
            shows = {str(s["ratingKey"]): s for s in plex.all(f"/library/sections/{key}/all", type=2, includeGuids=1)}
            episodes = plex.all(f"/library/sections/{key}/all", type=4)
            per_file = {}
            for ep in episodes:
                files = [(media, part) for media in ep.get("Media", []) for part in media.get("Part", [])]
                present = [(m, p) for m, p in files if Path(mapped(p["file"])).exists()]
                if present and len(present) < len(files):
                    files = present            # the rest are unavailable leftovers in Plex, see above
                for media, part in files:
                    per_file.setdefault(part["file"], []).append((ep, media))
            for plex_file, uses in per_file.items():
                f = Path(mapped(plex_file))
                ep, media = uses[0]
                show = shows.get(str(ep.get("grandparentRatingKey")), {})
                ids = plex_ids(show)
                info[str(f)] = {"title": show.get("title") or ep.get("grandparentTitle"), "year": show.get("year"),
                                "key": ep.get("grandparentRatingKey"), "section": key, "kind": "episode",
                                "episode": f"S{int(ep.get('parentIndex') or 0):02d}E{int(ep.get('index') or 0):02d}"}
                multi = None
                if len(uses) > 1:
                    seasons = {u[0].get("parentIndex") for u in uses}
                    numbers = sorted(int(u[0].get("index") or 0) for u in uses)
                    if len(seasons) != 1 or numbers[-1] - numbers[0] != len(numbers) - 1:
                        skipped.append((f, "file holds several episodes from different seasons or out of order"))
                        continue
                    multi = (int(seasons.pop() or 0), numbers[0], numbers[-1])
                if not ids:
                    skipped.append((f, "show not matched in Plex"))
                    continue
                root = root_of(f)
                if root is None:
                    skipped.append((f, "outside the Plex library folders (check PLEX_PATH_MAP)"))
                    continue
                entries.append({"file": f, "root": root, "kind": "episode", "group": f.relative_to(root).parts[0],
                                "section": key, "multi": multi,
                                "values": {"show": show.get("title") or ep.get("grandparentTitle"),
                                           "title": show.get("title") or ep.get("grandparentTitle"),
                                           "year": show.get("year"), "season": ep.get("parentIndex"),
                                           "episode": ep.get("index"), "episode_title": ep.get("title"),
                                           "resolution": resolution_label(media), **ids}})
    return entries, skipped


ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".tbn", ".webp"}
ARTWORK_TAGS = ("thumb", "poster", "fanart", "banner", "landscape", "clearart", "logo")


# ----------------------------------------------------------------- cleanup --
# Download leftovers the organizer deletes (each one listed in the plan): the ads release groups put in
# their folders, and subtitles in other languages than English. English subtitles in a Subs folder get
# their video's name, so Plex finds them.

JUNK_NAME = re.compile(r"^www\..+\.(jpe?g|png|gif|txt|url|html?)$"
                       r"|(yts|yify|rarbg|eztv|ettv|etrg|torrentgalaxy).*\.(txt|url|html?|exe)$"
                       r"|\.url$", re.I)
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
TEXT_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
SUBS_DIRS = {"subs", "sub", "subtitles"}
ENGLISH = {"en", "eng", "english"}
LANGUAGES = {
    "arabic": "ar ara", "bengali": "bn ben", "bulgarian": "bg bul", "catalan": "ca cat",
    "chinese": "zh chi zho chs cht", "croatian": "hr hrv", "czech": "cs cze ces", "danish": "da dan",
    "dutch": "nl dut nld", "estonian": "et est", "filipino": "fil tl tgl tagalog", "finnish": "fi fin",
    "french": "fr fre fra", "german": "de ger deu", "greek": "el gre ell", "hebrew": "he heb",
    "hindi": "hi hin", "hungarian": "hu hun", "icelandic": "is ice isl", "indonesian": "id ind",
    "italian": "it ita", "japanese": "ja jpn", "korean": "ko kor", "latvian": "lv lav",
    "lithuanian": "lt lit", "malay": "ms may msa", "norwegian": "no nor nob nno", "persian": "fa per fas farsi",
    "polish": "pl pol", "portuguese": "pt por pob", "romanian": "ro rum ron", "russian": "ru rus",
    "serbian": "sr srp scc", "slovak": "sk slo slk", "slovenian": "sl slv", "spanish": "es spa",
    "swedish": "sv swe", "tamil": "ta tam", "telugu": "te tel", "thai": "th tha", "turkish": "tr tur",
    "ukrainian": "uk ukr", "vietnamese": "vi vie", "basque": "eu baq eus", "galician": "gl glg",
    "cantonese": "yue", "mandarin": "cmn",
}
FOREIGN = {n for k, v in LANGUAGES.items() for n in [k, *v.split()]}
SUB_TAGS = {"sdh", "hi", "cc", "forced", "full", "default", "sub", "subs", "subtitle", "subtitles", "latin",
            "america", "american", "brazil", "brazilian", "spain", "portugal", "european", "canada",
            "canadian", "traditional", "simplified", "hans", "hant", "br", "mx", "la", "lat"}


def sub_language(label):
    """'en', a language name, or None when the label is not purely a language (keep the file).
    The label is the part of a subtitle's name that is not the video's: '2_English', 'French.fre',
    'English (SDH).eng', '.fr.forced'."""
    words = [w for w in re.split(r"[\s._\-\[\]()]+", re.sub(r"^\d+[_ .-]*", "", label).lower()) if w]
    if not words or not all(w in ENGLISH | FOREIGN | SUB_TAGS or w.isdigit() for w in words):
        return None
    if any(w in ENGLISH for w in words):
        return "en"
    foreign = [w for w in words if w in FOREIGN]
    if not foreign:
        return None
    w = foreign[0]
    return w if w in LANGUAGES else next(k for k, v in LANGUAGES.items() if w in v.split())


def sub_kind(label):
    """'sdh', 'forced' or '' for an English subtitle's label."""
    words = set(re.split(r"[\s._\-\[\]()]+", label.lower()))
    return "forced" if "forced" in words else "sdh" if words & {"sdh", "hi", "cc"} else ""


def sidecar_parts(video):
    """Files belonging to ``video``, as (file, the rest of its name after the video's name).
    Subtitles and .nfo: '<video name>.en.srt'. Artwork: '<video name>.jpg' or '<video name>-thumb.jpg'.
    The video's name is taken with or without its -noidea suffix (downloads name artwork after the
    original file)."""
    bases = sorted({video.stem, strip_suffix(video.name) if video.suffix.lower() == ".mkv" else video.stem},
                   key=len, reverse=True)
    found = []
    try:
        for f in video.parent.iterdir():
            if f == video or not f.is_file() or f.suffix.lower() in VIDEO_EXTS:
                continue
            for base in bases:
                if not f.name.startswith(base):
                    continue
                rest = f.name[len(base):]
                ext = f.suffix.lower()
                if rest.startswith(".") and ext in SIDECAR_EXTS | ARTWORK_EXTS:
                    found.append((f, rest))
                    break
                if ext in ARTWORK_EXTS and re.fullmatch(r"-(%s)\.\w+" % "|".join(ARTWORK_TAGS), rest, re.I):
                    found.append((f, rest))
                    break
    except OSError:
        pass
    return sorted(found)


def sidecars(video):
    """Subtitle, info and artwork files belonging to ``video`` (see sidecar_parts)."""
    return [f for f, _ in sidecar_parts(video)]


def target_for(entry, mode, templates, new_root_suffix="-new"):
    """Where ``entry`` goes under ``mode``."""
    f, v = entry["file"], entry["values"]
    kind = entry["kind"]
    stem = render(templates["movie_file" if kind == "movie" else "episode_file"], v)
    if done_suffix(f.stem):
        stem += SUFFIX
    name = stem + f.suffix
    if mode == "rename":
        return f.parent / name
    root = entry["root"]
    if mode == "newtree":
        root = root.with_name(root.name + new_root_suffix)
    if kind == "movie":
        return root / render(templates["movie_folder"], v) / name
    return root / render(templates["show_folder"], v) / render(templates["season_folder"], v) / name


MULTI_EPISODE = [
    re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._]*e(\d{1,3})(?:-?e|-|-s\d{1,2}e|[ ._]*&[ ._]*s\d{1,2}e|[ ._]*&[ ._]*e)(\d{1,3})(?!\d)"),
    re.compile(r"(?i)(?<![0-9])(\d{1,2})x(\d{2,3})(?:-|[ ._]*&[ ._]*\d{1,2}x)(\d{2,3})(?!\d)"),
]


def multi_episode_range(name):
    """(season, first, last) from 'S01E01-02', 'S01E01E02', 'S01E01-E02', '3x03 & 3x04'; None for one episode."""
    for rx in MULTI_EPISODE:
        m = rx.search(name)
        if m:
            season, first, last = (int(g) for g in m.groups())
            if first < last <= first + 10:
                return season, first, last
    return None


def multi_episode_name(name):
    r = multi_episode_range(name)
    return f"S{r[0]:02d}E{r[1]:02d}-E{r[2]:02d}" if r else None


PLACEHOLDER_TITLE = re.compile(r"(?i)^\s*(episode|ep\.?|chapter|part)\s*#?\d+\s*$")
CODE_IN_NAME = re.compile(r"(?i)(?<![a-z0-9])s\d{1,4}[ ._-]*e\d{1,4}(?:-?e?\d{1,4})?|(?<!\d)\d{1,2}x\d{2,3}(?!\d)")


def is_placeholder_title(title):
    """Plex's stand-in when it has no real title: 'Episode 5'."""
    return not title or bool(PLACEHOLDER_TITLE.match(str(title)))


def title_from_name(name):
    """The title written after the episode code in a file name, or None:
    's2014e05 - Too Many Cooks-noidea.mkv' -> 'Too Many Cooks'."""
    stem = strip_suffix(name) if name.lower().endswith(".mkv") else Path(name).stem
    m = CODE_IN_NAME.search(stem)
    if not m:
        return None
    rest = re.sub(r"\[[^\]]*\]", " ", stem[m.end():])
    rest = re.sub(r"(?i)[ ._(-]*\b(hevc|x26[45]|h\.?26[45]|xvid|divx|web-?dl|web-?rip|web|amzn|nf|hulu|dsnp|hmax|"
                  r"pcok|bluray|blu-ray|brrip|bdrip|dvdrip|dvd|hdtv|sdtv|remux|remastered|proper|repack|nordic|"
                  r"r\dj?dvd|\d{3,4}p|\d{3,4}x\d{3,4}|10bit|ddp?\d?)\b.*$", "", rest)
    rest = re.sub(r"\s+-[A-Za-z0-9]+(-[A-Za-z0-9]+)*$", "", rest)   # a release group after ' -'
    if rest.count("(") > rest.count(")"):
        rest += ")"
    rest = re.sub(r"\s+-\s*[A-Za-z0-9]+$", "", rest) if re.search(r"\s{2,}-\s*\w+$", rest) else rest
    rest = rest.replace(".", " ") if " " not in rest.strip(" .") else rest
    rest = re.sub(r"\s+", " ", rest).strip(" .-_()")
    return rest if re.search(r"[A-Za-z]{2}", rest or "") else None


def norm_title(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"(?i)\((incomplete|partial|uncut|extended)\)", " ", t)
    t = re.sub(r"['\u2019`]", "", t.lower())
    t = re.sub(r"\s+&\s+", " and ", t)
    t = re.sub(r"\bpart\s+(one|i|1)\b|\(1\)", " 1 ", t)
    t = re.sub(r"\bpart\s+(two|ii|2)\b|\(2\)", " 2 ", t)
    t = re.sub(r"\bpart\s+(three|iii|3)\b|\(3\)", " 3 ", t)
    t = re.sub(r"\bno\.\s*(\d)", r"number \1", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def titles_agree(a, b):
    """Loose: the file's title and Plex's title are plausibly the same episode."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return True
    import difflib
    return na in nb or nb in na or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def titles_same(a, b):
    """Strict: the file's title is that episode's title (used to renumber a file)."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    import difflib
    short, long_ = sorted((na, nb), key=len)
    return na == nb or (len(short) >= 5 and short in long_) or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9


def as_multi_episode(values, rng):
    """Template values for a file holding episodes first..last: S01E01-E02, no episode title
    (one title would be wrong for the other episode)."""
    season, first, last = rng
    return {**values, "season": season, "episode": f"{first:02d}-E{last:02d}", "episode_title": None}


def make_plan(entries, skipped, mode, templates, scope, folders=0, new_root_suffix="-new", two_episode="name",
              cleanup=True):
    """Work out every change, skipping clashes and anything outside ``scope``. Returns the plan."""
    scope = [Path(p).resolve() for p in scope]

    def in_scope(f):
        return not scope or any(f == s or s in f.parents for s in scope)

    items, skips, unchanged = [], [(str(f), why) for f, why in skipped if in_scope(f)], 0
    drops = []

    def season_key(e):
        v = e["values"]
        return (e.get("section"), v.get("tvdb") or v.get("show"), v.get("season"))
    seasons = {}
    for e in entries:
        if e["kind"] == "episode" and not e.get("multi") and not is_placeholder_title(e["values"].get("episode_title")):
            seasons.setdefault(season_key(e), []).append((e["values"].get("episode"), e["values"].get("episode_title")))

    for e in sorted(entries, key=lambda e: str(e["file"])):
        note = None
        f = e["file"]
        if not in_scope(f) or CLEAN_DIR in f.relative_to(e["root"]).parts:
            continue
        if not f.exists():
            skips.append((str(f), "not found here (check PLEX_PATH_MAP)"))
            continue
        if has_clean_copy(f):
            skips.append((str(f), "its Clean copy is waiting: run --replace first"))
            continue
        if e["kind"] == "episode":
            rng = e.get("multi") or multi_episode_range(f.name)
            if rng and two_episode != "name":
                skips.append((str(f), f"holds several episodes (S{rng[0]:02d}E{rng[1]:02d}-E{rng[2]:02d}); "
                                      "two-episode naming is off"))
                continue
            if rng:
                e = {**e, "values": as_multi_episode(e["values"], rng)}
            elif is_placeholder_title(e["values"].get("episode_title")):
                e = {**e, "values": {**e["values"], "episode_title": title_from_name(f.name)}}
            else:
                mine, plex_title = title_from_name(f.name), e["values"].get("episode_title")
                if mine and not titles_agree(mine, plex_title):
                    v = e["values"]
                    code = f"S{int(v.get('season') or 0):02d}E{int(v.get('episode') or 0):02d}"
                    hits = [(n, t) for n, t in seasons.get(season_key(e), []) if titles_same(mine, t)]
                    if len(hits) == 1 and hits[0][0] != v.get("episode"):
                        n, t = hits[0]
                        note = (f"renumbered: the file's title \u201c{mine}\u201d is Plex's "
                                f"S{int(v.get('season') or 0):02d}E{int(n):02d}, not {code} \u201c{plex_title}\u201d")
                        e = {**e, "values": {**v, "episode": n, "episode_title": t}}
                    else:
                        skips.append((str(f), "the titles in the file names don't match Plex's titles for those "
                                              "episode numbers, so Plex is showing the wrong episodes for them now: "
                                              "check the show's episode order in Plex (Edit, Advanced, Episode "
                                              "ordering) or the file names"))
                        continue
        new = target_for(e, mode, templates, new_root_suffix)
        if new == f:
            unchanged += 1
            continue
        st = f.stat()
        sides = []
        for s, rest in sidecar_parts(f):
            lang = sub_language(rest[:-len(s.suffix)]) if s.suffix.lower() in SUB_EXTS else None
            if cleanup and lang and lang != "en":
                drops.append({**drop(s, e, f"{lang.capitalize()} subtitles"), "video": str(f)})
                continue
            sides.append([str(s), str(new.with_name(
                (new.stem if not rest.startswith("-") else strip_suffix(new.name)) + rest))])
        items.append({"old": str(f), "new": str(new), "root": str(e["root"]), "group": e["group"],
                      "section": e.get("section"), "note": note,
                      "size": st.st_size, "mtime": st.st_mtime_ns, "sidecars": sides})
    # Two changes aiming at one name, or a name already in use by another file: leave both alone.
    targets = {}
    for it in items:
        targets.setdefault(it["new"], []).append(it)
    kept = []
    for it in items:
        new = Path(it["new"])
        if len(targets[it["new"]]) > 1:
            skips.append((it["old"], f"another file would get the same name: {new.name}"))
        elif new.exists() and not (mode != "newtree" and os.path.samefile(new, it["old"])):
            skips.append((it["old"], f"name already in use: {new}"))
        else:
            kept.append(it)
    if folders:
        groups = list(dict.fromkeys((it["root"], it["group"]) for it in kept))[:folders]
        kept = [it for it in kept if (it["root"], it["group"]) in groups]
    leftovers = []
    extras = plan_extras(kept, mode, new_root_suffix, skips, leftovers, drops if cleanup else None)
    groups = {(it["root"], it["group"]) for it in kept}
    kept_olds = {it["old"] for it in kept}
    deletes = [] if mode == "newtree" else sorted(
        {d["path"]: d for d in drops
         if (d["root"], d["group"]) in groups and (d.get("video") is None or d["video"] in kept_olds)}.values(),
        key=lambda d: d["path"])
    return {"version": 1, "created": time.time(), "mode": mode, "templates": templates,
            "new_root_suffix": new_root_suffix, "roots": sorted({str(e["root"]) for e in entries}),
            "items": kept, "extras": extras, "clean_leftovers": leftovers, "deletes": deletes,
            "skipped": sorted(skips), "unchanged": unchanged}


SYNO_DIR = "@eaDir"


def only_synology_files(d):
    """A Synology @eaDir folder holding nothing but the NAS's own index files (it rebuilds them)."""
    try:
        for dirpath, _, filenames in os.walk(d):
            for name in filenames:
                if "@Syno" not in name and not name.startswith("SYNO") and name != "Thumbs.db":
                    return False
        return True
    except OSError:
        return False


def only_own_files(clean):
    """A Clean folder holding nothing but tinyclean's own bookkeeping files (no copies)."""
    try:
        return all(e.is_file() and e.name in CLEAN_OWN_FILES for e in clean.iterdir())
    except OSError:
        return False


def plan_extras(items, mode, new_root_suffix, skips, leftovers, drops=None):
    """Everything else in a movie's or show's old folder (posters, .nfo files, extras, subtitle folders)
    goes along with its videos, so the old folder ends up empty and is removed. Only when that folder
    belongs to this one movie or show alone: a movie folder with another movie directly in it, or a show
    folder with an episode that is not moving, keeps everything that is not a planned video.
    With ``drops`` (a list to add to), download ads and other-language subtitles found there are listed
    for deleting instead, and English subtitles in a Subs folder get their video's name next to it."""
    by_group = {}
    for it in items:
        by_group.setdefault((it["root"], it["group"]), []).append(it)
    extras = []
    for (root, group), its in by_group.items():
        root = Path(root)
        gdir = root / group
        if not gdir.is_dir():
            continue
        new_root = root.with_name(root.name + new_root_suffix) if mode == "newtree" else root
        tops = {Path(it["new"]).relative_to(new_root).parts[0] for it in its}
        if len(tops) != 1:
            continue
        top = new_root / tops.pop()
        # The folder keeps its name: its other files are already in place, only the cleanup applies.
        stay = mode != "newtree" and top == gdir
        if stay and drops is None:
            continue
        planned = {it["old"] for it in its} | {s_old for it in its for s_old, _ in it["sidecars"]}
        planned |= {d["path"] for d in drops or ()}     # other-language subtitles beside a video
        taken = {s_new for it in its for _, s_new in it["sidecars"]}
        movie = Path(its[0]["new"]).parent == top and len(its) == 1
        parents = {}
        for it in its:
            parents.setdefault(str(Path(it["old"]).parent), set()).add(str(Path(it["new"]).parent))
        mine, blocked = [], None
        syno = []
        english = {}                    # video item -> [(file, kind)]
        for dirpath, dirnames, filenames in os.walk(gdir):
            syno += [Path(dirpath) / d for d in dirnames if d == SYNO_DIR]
            dirnames[:] = [d for d in dirnames if d not in (CLEAN_DIR, SYNO_DIR)]
            for name in filenames:
                f = Path(dirpath) / name
                if str(f) in planned:
                    continue
                if f.suffix.lower() in VIDEO_EXTS and (not movie or f.parent == gdir):
                    blocked = blocked or f
                    continue
                if drops is not None and JUNK_NAME.search(f.name):
                    drops.append(drop(f, {"root": root, "group": group}, "download ad"))
                    continue
                if drops is not None and f.suffix.lower() in SUB_EXTS:
                    owner, label = subs_owner(f, its)
                    lang = sub_language(label) if label is not None else None
                    if lang and lang != "en":
                        drops.append(drop(f, {"root": root, "group": group}, f"{lang.capitalize()} subtitles"))
                        continue
                    if lang == "en" and owner is not None and f.suffix.lower() in TEXT_SUB_EXTS:
                        english.setdefault(id(owner), (owner, []))[1].append((f, sub_kind(label)))
                        continue
                mine.append(f)
        # English subtitles from a Subs folder: one plain, one SDH and one forced per video, named after it.
        for owner, subs in english.values():
            new_video = Path(owner["new"])
            for kind in ("", "sdh", "forced"):
                pick = sorted((f for f, k in subs if k == kind), key=lambda f: f.stat().st_size, reverse=True)
                for f in pick:
                    new = new_video.with_name(new_video.stem + ".en" + (f".{kind}" if kind else "") + f.suffix.lower())
                    if f is pick[0] and str(new) not in taken and not new.exists():
                        taken.add(str(new))
                        extras.append({"old": str(f), "new": str(new), "root": str(root), "group": group,
                                       "note": "English subtitles, named after their video"})
                    else:
                        mine.append(f)
        if stay:
            continue
        if blocked:
            skips.append((str(gdir), f"other files stay in the old folder: it also holds {blocked.name}, "
                                     "which is not part of this change"))
            continue
        cleans = [Path(dirpath) / CLEAN_DIR for dirpath, _, _ in os.walk(gdir) if (Path(dirpath) / CLEAN_DIR).is_dir()]
        if any(not only_own_files(c) for c in cleans):
            continue                    # Clean copies still waiting somewhere in it: leave the rest alone
        leftovers += [{"dir": str(c), "root": str(root), "group": group, "kind": "clean"} for c in cleans]
        leftovers += [{"dir": str(d), "root": str(root), "group": group, "kind": "synology"}
                      for d in syno if only_synology_files(d)]
        for f in mine:
            # Follow the nearest folder whose videos all went to one place: Season 1/Subs/x.srt -> Season 01/Subs/x.srt
            d, new = f.parent, None
            while True:
                dest = parents.get(str(d), set())
                if len(dest) == 1:
                    new = Path(next(iter(dest))) / f.relative_to(d)
                    break
                if d == gdir:
                    break
                d = d.parent
            new = new or top / f.relative_to(gdir)
            if new.exists():
                skips.append((str(f), f"stays in the old folder: name already in use: {new}"))
                continue
            extras.append({"old": str(f), "new": str(new), "root": str(root), "group": group})
    return extras


def drop(f, e, why):
    """A file the plan deletes: an ad from the download, or subtitles in another language."""
    try:
        size = f.stat().st_size
    except OSError:
        size = None
    return {"path": str(f), "root": str(e["root"]), "group": e["group"], "size": size, "why": why}


def subs_owner(f, its):
    """For a subtitle file in a movie's or show's folder: (the planned video it belongs to or None, its
    language label or None). Subs/2_English.srt belongs to the one video beside the Subs folder;
    Subs/<episode file name>/2_English.srt to that episode."""
    d = f.parent
    if d.name.lower() in SUBS_DIRS:
        beside = [it for it in its if Path(it["old"]).parent == d.parent]
        return (beside[0] if len(beside) == 1 else None), f.stem
    if d.parent.name.lower() in SUBS_DIRS:
        for it in its:
            v = Path(it["old"])
            if v.parent == d.parent.parent and d.name in (v.stem, strip_suffix(v.name)):
                return it, f.stem
        return None, f.stem
    return None, None


def group_skipped(plan):
    """Skipped files with the same reason in the same show or movie folder, as one line each:
    [(folder, reason, [paths])]. Reasons naming another file stay one line per file."""
    roots = sorted(plan.get("roots", []), key=len, reverse=True)
    groups = {}
    for path, why in plan["skipped"]:
        root = next((r for r in roots if path.startswith(r.rstrip("/") + "/")), None)
        folder = str(Path(root) / Path(path).relative_to(root).parts[0]) if root else str(Path(path).parent)
        personal = why.startswith(("another file", "name already", "other files stay", "stays in", "the file name"))
        key = (folder, why, path if personal else None)
        groups.setdefault(key, []).append(path)
    return [(folder, why, paths) for (folder, why, _), paths in groups.items()]


def show_plan(plan):
    by_root = {}
    for it in plan["items"]:
        by_root.setdefault(it["root"], []).append(it)
    for root, items in by_root.items():
        print(f"\n{root}")
        for it in items:
            old = os.path.relpath(it["old"], root)
            new = os.path.relpath(it["new"], root)
            print(f"  {old}\n    -> {new}")
            if it.get("note"):
                print(f"       ({it['note']})")
            for s_old, s_new in it["sidecars"]:
                print(f"       + {Path(s_old).name}  ->  {Path(s_new).name}")
    if plan["skipped"]:
        print(f"\nSkipped ({len(plan['skipped'])}):")
        for folder, why, paths in group_skipped(plan):
            if len(paths) == 1:
                print(f"  {paths[0]}\n    {why}")
            else:
                print(f"  {folder}: {len(paths)} files\n    {why}")
    leftovers = plan.get("clean_leftovers", [])
    for c in leftovers:
        if c.get("kind") != "synology":
            print(f"\nLeftover Clean folder, holding only tinyclean's own notes (no copies), will be removed: {c['dir']}")
    syno = [c for c in leftovers if c.get("kind") == "synology"]
    if syno:
        print(f"\nSynology @eaDir index folders for the old names, removed with the old folders ({len(syno)}; "
              "the NAS rebuilds them):")
        for c in syno[:50]:
            print(f"  {os.path.relpath(c['dir'], c['root'])}")
    renumbered = [it for it in plan["items"] if it.get("note")]
    if renumbered:
        print(f"\n{len(renumbered)} episode(s) renumbered to match their own titles (see the notes above).")
    extras = plan.get("extras", [])
    if extras:
        print(f"\nOther files moving with them ({len(extras)}):")
        for x in extras[:200]:
            print(f"  {os.path.relpath(x['old'], x['root'])}  ->  {os.path.relpath(x['new'], x['root'])}"
                  + (f"  ({x['note']})" if x.get("note") else ""))
        if len(extras) > 200:
            print(f"  ... and {len(extras) - 200} more")
    deletes = plan.get("deletes", [])
    if deletes:
        print(f"\nDownload leftovers deleted ({len(deletes)}; not in the recycle bin, and undo cannot bring them back):")
        for d in deletes:
            print(f"  {os.path.relpath(d['path'], d['root'])}  ({d['why']})")
    sc = sum(len(it["sidecars"]) for it in plan["items"])
    verb = {"rename": "rename", "restructure": "move", "newtree": "hard-link into the new tree"}[plan["mode"]]
    print(f"\nWill {verb} {len(plan['items'])} video(s)" + (f", {sc} subtitle/info file(s)" if sc else "")
          + (f" and {len(extras)} other file(s)" if extras else "")
          + (f"; delete {len(deletes)} download leftover(s)" if deletes else "")
          + f"; {plan['unchanged']} already named right; {len(plan['skipped'])} skipped.")


def plex_settings(env_file, url_override=None):
    """Plex connection and organizer settings from the env file (then the environment)."""
    env = {**{k: v for k, v in os.environ.items() if k.startswith("PLEX_")}, **load_env(env_file)}
    url = url_override or env.get("PLEX_URL")
    token = env.get("PLEX_TOKEN")
    pairs = [tuple(p.strip().split("=", 1)) for p in (env.get("PLEX_PATH_MAP") or "").split(",") if "=" in p]
    return {"url": url, "token": token, "pairs": pairs,
            "ignore": [t for t in (env.get("PLEX_IGNORE_LIBRARIES") or "").split(",") if t.strip()],
            "refresh": (env.get("PLEX_REFRESH") or "").strip().lower() in ("1", "yes", "true", "on")}


def plex_refresh(settings, folders):
    """Ask Plex to rescan just these folders: [(section key, local folder)]. Many folders: the whole library."""
    if not settings.get("refresh") or not settings.get("url") or not settings.get("token") or not folders:
        return
    import urllib.parse
    import urllib.request
    to_plex = path_mapper([(local, plex) for plex, local in settings["pairs"]])
    by_section = {}
    for key, folder in folders:
        if key:
            by_section.setdefault(str(key), set()).add(to_plex(str(folder)))
    for key, paths in by_section.items():
        calls = [None] if len(paths) > 20 else sorted(paths)
        for path in calls:
            q = urllib.parse.urlencode({"path": path} if path else {})
            req = urllib.request.Request(f"{settings['url'].rstrip('/')}/library/sections/{key}/refresh"
                                         + (f"?{q}" if q else ""), headers={"X-Plex-Token": settings["token"]})
            try:
                urllib.request.urlopen(req, timeout=30).close()
            except OSError as exc:
                print(f"  Plex rescan request failed: {exc}")
                return
    print(f"Asked Plex to rescan {sum(len(p) for p in by_section.values())} folder(s).")


def trim_state(state_dir):
    """Plans older than a week (they expire after a day) and undo logs older than 90 days."""
    now = time.time()
    for folder, days in (("plans", 7), ("undo", 90)):
        for f in (state_dir / folder).glob("*.json"):
            try:
                if now - f.stat().st_mtime > days * 86400:
                    f.unlink()
            except OSError:
                pass


def organize(args, state_dir):
    settings = plex_settings(args.env_file, args.plex_url)
    url, token = settings["url"], settings["token"]
    if not url or not token:
        sys.exit(f"Plex is not set up: put PLEX_URL and PLEX_TOKEN in {args.env_file} (readable by root and media only).")
    trim_state(state_dir)
    templates = dict(TEMPLATES)
    for name in TEMPLATES:
        value = getattr(args, name)
        if value:
            templates[name] = value
    bad = {n: template_problems(t) for n, t in templates.items() if template_problems(t)}
    if bad:
        sys.exit("Unknown template tokens: " + "; ".join(f"{n}: {', '.join(b)}" for n, b in bad.items())
                 + f". Known: {', '.join(sorted(TEMPLATE_TOKENS))}.")
    print("Reading the library from Plex...")
    plex, mapped = Plex(url, token), path_mapper(settings["pairs"])
    ignore = {t.strip().lower() for t in settings["ignore"]}
    try:
        sections = plex.sections()
        entries, skipped = read_library(plex, mapped, settings["ignore"])
    except OSError as exc:
        sys.exit(f"Could not read Plex at {url}: {exc}")
    if not sections:
        sys.exit("Plex answered but has no movie or TV libraries.")
    for sec in sections:
        locs = sec.get("Location", [])
        if sec.get("title", "").lower() in ignore:
            print(f"  {sec.get('title', '?')}: ignored (PLEX_IGNORE_LIBRARIES)")
            continue
        if locs and not any(os.path.isdir(mapped(loc["path"])) for loc in locs):
            print(f"  {sec.get('title', '?')}: not on this machine, left out")
            continue
        for loc in locs:
            here = mapped(loc["path"])
            seen = f"{loc['path']}  (here: {here})" if here != loc["path"] else loc["path"]
            print(f"  {sec.get('title', '?')} [{sec['type']}]: {seen}")
    print(f"Plex knows {len(entries) + len(skipped)} video file(s) in these folders.")
    plan = make_plan(entries, skipped, args.organize, templates, args.paths, args.folders, args.new_root_suffix,
                     args.two_episode, cleanup=not args.keep_leftovers)
    show_plan(plan)
    if not plan["items"]:
        if entries and not plan["skipped"] and not plan["unchanged"]:
            print(f"\nNone of Plex's files are under {', '.join(args.paths)}.\n"
                  "If Plex's folders above are not the paths on this machine, add a line like\n"
                  "  PLEX_PATH_MAP=<Plex's folder>=<same folder here>\n"
                  f"to {args.env_file} (several pairs separated by commas).")
        print("Nothing to change.")
        return
    plans = state_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"plan-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(plan, indent=1))
    print(f"Dry run only: nothing changed. Plan saved as {path}")
    print(f"To carry it out: tinyclean --apply-plan {path}")


def newest(folder, pattern):
    found = sorted(f for f in folder.glob(pattern) if not f.name.endswith(".undone.json"))
    return found[-1] if found else None


def remove_empty_dirs(start, stop):
    """Remove ``start`` and its parents while empty, never going above ``stop``."""
    d = Path(start)
    stop = Path(stop)
    while d != stop and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def apply_plan(plan_arg, state_dir, yes, env_file=ENV_FILE):
    plans = state_dir / "plans"
    path = newest(plans, "plan-*.json") if plan_arg == "latest" else Path(plan_arg)
    if not path or not path.is_file():
        sys.exit("No saved plan found. Run --organize first.")
    plan = json.loads(path.read_text())
    age_h = (time.time() - plan["created"]) / 3600
    if age_h > PLAN_MAX_AGE_HOURS:
        sys.exit(f"That plan is {age_h:.0f} hours old; run the dry run again so it matches the library now.")
    n = len(plan["items"])
    print(f"Plan {path.name}: {plan['mode']}, {n} video(s).")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not run from a terminal: add --yes to apply.")
        if input("Type 'apply' to go ahead: ").strip().lower() != "apply":
            print("Nothing changed.")
            return
    link = plan["mode"] == "newtree"
    undo_dir = state_dir / "undo"
    undo_dir.mkdir(parents=True, exist_ok=True)
    undo_path = undo_dir / f"undo-{time.strftime('%Y%m%d-%H%M%S')}.json"
    trim_state(state_dir)
    log = {"plan": str(path), "mode": plan["mode"], "done": [],
           "folders": sorted({(it.get("section"), str(Path(p).parent)) for it in plan["items"]
                              for p in (it["old"], it["new"])})}

    def record(kind, old, new, root):
        # The last item is the folder an undo may tidy empty folders up to, and never past.
        stop = root.with_name(root.name + plan.get("new_root_suffix", "-new")) if link else root
        log["done"].append([kind, str(old), str(new), str(stop)])
        undo_path.write_text(json.dumps(log, indent=1))

    def place(old, new, root):
        if new.exists():
            return f"name now in use: {new}"
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            if link:
                os.link(old, new)
            else:
                os.rename(old, new)
        except OSError as exc:
            return f"could not {'link' if link else 'move'}: {exc}"
        record("link" if link else "move", old, new, root)
        return None

    done, problems = 0, []
    for it in plan["items"]:
        old, new, root = Path(it["old"]), Path(it["new"]), Path(it["root"])
        try:
            st = old.stat()
        except OSError:
            problems.append((it["old"], "gone since the dry run"))
            continue
        if st.st_size != it["size"] or st.st_mtime_ns != it["mtime"]:
            problems.append((it["old"], "changed since the dry run"))
            continue
        why = place(old, new, root)
        if why:
            problems.append((it["old"], why))
            continue
        done += 1
        for s_old, s_new in it["sidecars"]:
            if Path(s_old).exists():
                why = place(Path(s_old), Path(s_new), root)
                if why:
                    problems.append((s_old, why))
        if not link:
            remove_empty_dirs(old.parent, root)
        print(f"  {'linked' if link else 'done'}  {new}")
    groups_done = {(it["root"], it["group"]) for it in plan["items"]
                   if not any(p == it["old"] for p, _ in problems)}
    extras_done = 0
    for x in plan.get("extras", []):
        if (x["root"], x["group"]) not in groups_done:
            continue
        old, new, root = Path(x["old"]), Path(x["new"]), Path(x["root"])
        if not old.exists():
            continue
        why = place(old, new, root)
        if why:
            problems.append((x["old"], why))
            continue
        extras_done += 1
        if not link:
            remove_empty_dirs(old.parent, root)
    if extras_done:
        print(f"  ...and {extras_done} other file(s) {'linked' if link else 'moved'} with them")
    deleted = 0
    if not link and plan.get("deletes"):
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "deleted.log", "a") as dlog:
            for d in plan["deletes"]:
                f = Path(d["path"])
                if (d["root"], d["group"]) not in groups_done or not f.is_file() or f.suffix.lower() in VIDEO_EXTS:
                    continue
                if d.get("size") is not None and f.stat().st_size != d["size"]:
                    problems.append((d["path"], "changed since the dry run, not deleted"))
                    continue
                try:
                    f.unlink()
                except OSError as exc:
                    problems.append((d["path"], f"could not delete: {exc}"))
                    continue
                dlog.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "file": d["path"],
                                       "size": d.get("size"), "why": d["why"]}) + "\n")
                log.setdefault("deleted", []).append(d["path"])
                deleted += 1
                remove_empty_dirs(f.parent, Path(d["root"]))
        if deleted:
            undo_path.write_text(json.dumps(log, indent=1))
            print(f"  deleted {deleted} download leftover(s) (ads, other-language subtitles); listed in deleted.log")
    if not link:
        syno_removed = 0
        for c in plan.get("clean_leftovers", []):
            if (c["root"], c["group"]) not in groups_done:
                continue
            d = Path(c["dir"])
            if c.get("kind") == "synology":
                if d.is_dir() and only_synology_files(d):
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    syno_removed += 1
                    remove_empty_dirs(d.parent, Path(c["root"]))
            elif remove_empty_clean(d):
                print(f"  removed the leftover Clean folder {d} (it held only tinyclean's own notes)")
                remove_empty_dirs(d.parent, Path(c["root"]))
        if syno_removed:
            print(f"  removed {syno_removed} Synology @eaDir index folder(s) for the old names")
    for p, why in problems:
        print(f"  [skip] {p}: {why}")
    print(f"\n{done} of {len(plan['items'])} video(s) {'linked' if link else 'moved'}. "
          f"Undo log: {undo_path}\nTo put them back: tinyclean --undo {undo_path}")
    settings = plex_settings(env_file)
    if done and settings["refresh"]:
        plex_refresh(settings, log["folders"])
    elif done:
        print("Rescan the library in Plex" + (", and Refresh in Sonarr/Radarr." if not link else "."))


def undo_run(undo_arg, state_dir, yes, env_file=ENV_FILE):
    undo_dir = state_dir / "undo"
    path = newest(undo_dir, "undo-*.json") if undo_arg == "latest" else Path(undo_arg)
    if not path or not path.is_file():
        sys.exit("No undo log found.")
    log = json.loads(path.read_text())
    print(f"Undo {path.name}: {len(log['done'])} change(s) from a {log['mode']} run.")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not run from a terminal: add --yes to undo.")
        if input("Type 'undo' to go ahead: ").strip().lower() != "undo":
            print("Nothing changed.")
            return
    put_back, problems = 0, []
    for kind, old, new, stop in reversed(log["done"]):
        old, new, stop = Path(old), Path(new), Path(stop)
        if kind == "move":
            if not new.exists() or old.exists():
                problems.append((str(new), "cannot move back: moved again or the old name is in use"))
                continue
            old.parent.mkdir(parents=True, exist_ok=True)
            os.rename(new, old)
            remove_empty_dirs(new.parent, stop)
        else:
            # Only ever remove the second name of a hard link, never the last copy of a video.
            if not (new.exists() and old.exists() and os.path.samefile(old, new)):
                problems.append((str(new), "not removed: the original name is gone or changed"))
                continue
            new.unlink()
            remove_empty_dirs(new.parent, stop)
        put_back += 1
    for p, why in problems:
        print(f"  [skip] {p}: {why}")
    path.rename(path.with_suffix(".undone.json"))
    print(f"Put back {put_back} of {len(log['done'])} change(s).")
    if put_back:
        plex_refresh(plex_settings(env_file), [tuple(x) for x in log.get("folders", [])])


# ------------------------------------------------------------------- audit --
# One look over the library for the things only a person can sort out, saved for the dashboard.

AUDIT_FILE = "audit.json"


def ledger_failures(state_dir):
    """Files whose latest attempt failed, from the ledger: [{file, time, notes, redownload}]."""
    import csv
    latest = {}
    try:
        with open(state_dir / "ledger.csv", newline="") as f:
            for row in csv.DictReader(f):
                latest[row.get("file")] = row
    except OSError:
        return []
    out = []
    for path, row in latest.items():
        if row.get("result") == "failed" and path and Path(path).exists():
            notes = row.get("notes") or ""
            out.append({"file": path, "time": row.get("time", ""), "notes": notes,
                        "redownload": bool(re.search(r"damaged or incomplete|re-download", notes, re.I))})
    return sorted(out, key=lambda r: r["time"], reverse=True)


def file_facts(path):
    try:
        st = Path(path).stat()
        return {"file": str(path), "size": st.st_size, "converted": bool(done_suffix(Path(path).stem))}
    except OSError:
        return {"file": str(path), "size": None, "converted": False, "missing": True}


def audit(args, state_dir):
    settings = plex_settings(args.env_file, args.plex_url)
    if not settings["url"] or not settings["token"]:
        sys.exit(f"Plex is not set up: put PLEX_URL and PLEX_TOKEN in {args.env_file}.")
    plex, mapped = Plex(settings["url"], settings["token"]), path_mapper(settings["pairs"])
    print("Reading the library from Plex...")
    info = {}
    try:
        entries, skipped = read_library(plex, mapped, settings["ignore"], info)
        machine = plex.get("/identity").get("machineIdentifier")
    except OSError as exc:
        sys.exit(f"Could not read Plex at {settings['url']}: {exc}")
    scope = [Path(p).resolve() for p in args.paths]

    def in_scope(f):
        return not scope or any(Path(f) == s_ or s_ in Path(f).parents for s_ in scope)

    roots = sorted({str(e["root"]) for e in entries}, key=len, reverse=True)

    def folder_of(f):
        root = next((r for r in roots if f.startswith(r.rstrip("/") + "/")), None)
        return str(Path(root) / Path(f).relative_to(root).parts[0]) if root else str(Path(f).parent)

    # Duplicates: one movie with several files, or one episode in several files.
    groups = {}
    for f, why in skipped:
        if why.startswith("several files or versions") and in_scope(f):
            i = info.get(str(f), {})
            groups.setdefault(("movie", i.get("section"), i.get("key")), []).append(str(f))
    for e in entries:
        f = str(e["file"])
        i = info.get(f, {})
        if e["kind"] == "episode" and not e.get("multi") and in_scope(f) and Path(f).exists():
            groups.setdefault(("episode", i.get("section"), i.get("key"), i.get("episode")), []).append(f)
    duplicates = []
    for key, files in groups.items():
        files = sorted(set(files))
        if len(files) < 2:
            continue
        i = info.get(files[0], {})
        duplicates.append({"kind": key[0], "title": i.get("title"), "year": i.get("year"),
                           "episode": i.get("episode") if key[0] == "episode" else None,
                           "plex_key": i.get("key"), "files": [file_facts(f) for f in files]})
    duplicates.sort(key=lambda d: (str(d["title"]).lower(), d["episode"] or ""))

    unmatched, other = {}, {}
    for f, why in skipped:
        f = str(f)
        if not in_scope(f):
            continue
        if "not matched in Plex" in why:
            u = unmatched.setdefault(folder_of(f), {"folder": folder_of(f), "files": 0, "plex_key": None,
                                                   "title": info.get(f, {}).get("title")})
            u["files"] += 1
            u["plex_key"] = u["plex_key"] or info.get(f, {}).get("key")
        elif not why.startswith("several files or versions"):
            o = other.setdefault((folder_of(f), why), {"folder": folder_of(f), "why": why, "files": []})
            o["files"].append(f)

    # Shows whose file names give other titles than Plex has for those numbers: Plex shows the wrong
    # episodes for them, and the organizer leaves them alone until the order is fixed.
    titles = {}
    for e in entries:
        v = e["values"]
        if e["kind"] == "episode" and not e.get("multi") and not is_placeholder_title(v.get("episode_title")):
            titles.setdefault((e.get("section"), v.get("tvdb") or v.get("show"), v.get("season")), []).append(
                v.get("episode_title"))
    order = {}
    for e in entries:
        f, v = e["file"], e["values"]
        if e["kind"] != "episode" or e.get("multi") or not in_scope(str(f)) or multi_episode_range(f.name):
            continue
        mine, theirs = title_from_name(f.name), v.get("episode_title")
        if not mine or is_placeholder_title(theirs) or titles_agree(mine, theirs):
            continue
        season = titles.get((e.get("section"), v.get("tvdb") or v.get("show"), v.get("season")), [])
        if len([t for t in season if titles_same(mine, t)]) == 1:
            continue                    # the organizer renumbers this one by itself
        key = folder_of(str(f))
        o = order.setdefault(key, {"folder": key, "title": v.get("show"), "files": 0, "examples": [],
                                   "plex_key": info.get(str(f), {}).get("key")})
        o["files"] += 1
        if len(o["examples"]) < 3:
            o["examples"].append({"file": f.name, "mine": mine, "plex": theirs,
                                  "code": f"S{int(v.get('season') or 0):02d}E{int(v.get('episode') or 0):02d}"})

    waiting = {}
    for e in entries:
        f = e["file"]
        if in_scope(str(f)) and has_clean_copy(f):
            waiting[str(f.parent)] = waiting.get(str(f.parent), 0) + 1

    failed = [r for r in ledger_failures(state_dir) if in_scope(r["file"])]
    result = {"version": 1, "created": time.time(), "scope": [str(p) for p in scope],
              "plex": {"url": settings["url"].rstrip("/"), "machine": machine},
              "duplicates": duplicates,
              "unmatched": sorted(unmatched.values(), key=lambda u: u["folder"].lower()),
              "waiting": [{"folder": k, "files": v} for k, v in sorted(waiting.items())],
              "failed": failed,
              "order": sorted(order.values(), key=lambda o: o["folder"].lower()),
              "other": sorted(other.values(), key=lambda o: o["folder"].lower())}
    result["counts"] = {k: len(result[k]) for k in ("duplicates", "unmatched", "order", "waiting", "failed", "other")}
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (AUDIT_FILE + ".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(state_dir / AUDIT_FILE)
    c = result["counts"]
    print(f"\nDuplicates: {c['duplicates']}   Not matched in Plex: {c['unmatched']} folder(s)   "
          f"Episode order differs from Plex: {c['order']} show(s)   "
          f"Clean copies waiting: {c['waiting']} folder(s)   Failed conversions: {c['failed']}   "
          f"Other: {c['other']}")
    for d in duplicates[:50]:
        print(f"  duplicate: {d['title']}" + (f" {d['episode']}" if d["episode"] else "") + ": "
              + ", ".join(Path(x["file"]).name for x in d["files"]))
    print(f"Saved for the dashboard as {state_dir / AUDIT_FILE}")


def delete_files(paths, state_dir, yes):
    """Delete duplicate videos the user picked, with their subtitle files. Only video files, one by one,
    each named in the output and in deleted.log. Over NFS nothing goes to the NAS recycle bin."""
    files = []
    for p in paths:
        f = Path(p)
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            sys.exit(f"Not a video file: {p}")
        files.append(f)
    print("Will delete:")
    for f in files:
        print(f"  {f}  ({human(f.stat().st_size)})")
        for sc in sidecars(f):
            print(f"    + {sc.name}")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("Not run from a terminal: add --yes to delete.")
        if input("Type 'delete' to go ahead: ").strip().lower() != "delete":
            print("Nothing deleted.")
            return
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "deleted.log", "a") as log:
        for f in files:
            for victim in [f, *sidecars(f)]:
                size = victim.stat().st_size
                victim.unlink()
                log.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "file": str(victim),
                                      "size": size}) + "\n")
                print(f"  deleted {victim}")
            try:
                f.parent.rmdir()
                print(f"  removed the now empty folder {f.parent}")
            except OSError:
                pass
    print("Rescan the library in Plex.")


def main():
    ap = argparse.ArgumentParser(prog="tinyclean", description="Shrink and normalize video files (see header for rules).")
    ap.add_argument("paths", nargs="*", help="folders or files to process")
    ap.add_argument("--dry-run", action="store_true", help="show the plan for each file, change nothing")
    ap.add_argument("--sample", type=int, metavar="N", help="encode a 60 s clip of the N largest files and report projected savings")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--min-free-gb", type=int, default=DEFAULTS["min_free_gb"], help="stop when free space would drop below this")
    ap.add_argument("--min-free-ram-mb", type=int, default=DEFAULTS["min_free_ram_mb"], metavar="MB",
                    help="stop a file's ffmpeg when the container's free memory stays below this "
                         "(default %(default)s, 0 = never), so the container cannot run out and lock up")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.tinyclean"))
    ap.add_argument("--encoder", choices=["qsv", "x265"], default="qsv")
    ap.add_argument("--device", default="/dev/dri/renderD128")
    ap.add_argument("--profile", choices=sorted(PROFILES), help="use this profile for every file (no questions)")
    ap.add_argument("--keep-hdr", action="store_true",
                    help="copy HDR video untouched (old behaviour) instead of re-encoding it at 1080p")
    ap.add_argument("--keep-sample", action="store_true",
                    help="with --sample: keep the last clip as /data/_tools/tinyclean-sample.mkv")
    ap.add_argument("--jobs", type=int, metavar="N",
                    help="convert N files at the same time (asked at start if not given; remembered)")
    ap.add_argument("--batch", type=int, default=10, metavar="N",
                    help="ask animated/live for N shows at a time, convert them, then ask for the next N "
                         "(0 = ask for every show up front)")
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
    ap.add_argument("--rename-suffix", action="store_true",
                    help=f"rename finished files from {'/'.join(OLD_SUFFIXES)} to {SUFFIX} and tag them, and give "
                         f"{SUFFIX} back to tinyclean files that lost it, e.g. renamed by Sonarr/Radarr (asks first)")
    ap.add_argument("--check", action="store_true",
                    help="compare originals with their Clean copies and report; changes nothing")
    ap.add_argument("--progress-lines", action="store_true",
                    help=f"print the run's progress as JSON lines starting with '{PROGRESS_PREFIX.strip()}' "
                         "(used by the mediatool agent)")
    ap.add_argument("--status", action="store_true",
                    help=f"rewrite {STATUS_NAME} at the top of the library and print it")
    org = ap.add_argument_group("organize (names and folders from Plex's matches; always a dry run first)")
    org.add_argument("--organize", choices=ORGANIZE_MODES,
                     help="rename: rename files where they are; restructure: also move them into standard "
                          "folders; newtree: hard-link everything into a new tree next to the library "
                          "(<library>-new), leaving the original untouched. Saves a plan; changes nothing")
    org.add_argument("--apply-plan", metavar="PLAN", help="carry out a saved plan ('latest' for the newest)")
    org.add_argument("--undo", metavar="LOG", help="put back an applied plan ('latest' for the newest)")
    org.add_argument("--folders", type=int, default=0, metavar="N",
                     help="only the first N movie or show folders, by name (0 = all); also limits a conversion run")
    org.add_argument("--new-root-suffix", default="-new", metavar="TEXT",
                     help="with newtree: name the new tree <library><TEXT> (default %(default)s)")
    for name, default in TEMPLATES.items():
        org.add_argument("--" + name.replace("_", "-"), metavar="TEMPLATE", help=f"default: {default}")
    org.add_argument("--audit", action="store_true",
                     help="list what needs a person (duplicates, unmatched titles, waiting copies, failures) "
                          "for the dashboard; changes nothing. Folders are optional")
    org.add_argument("--delete", action="store_true",
                     help="delete the given video files and their subtitle files (asks first)")
    org.add_argument("--two-episode", choices=("name", "skip"), default="name",
                     help="files holding two or more episodes: name them S01E01-E02 (default) or leave them")
    org.add_argument("--keep-leftovers", action="store_true",
                     help="keep download ads (www.YTS....jpg, YTS/YIFY .txt) and other-language subtitles; by "
                          "default the plan lists them for deleting and names English Subs-folder subtitles "
                          "after their video")
    org.add_argument("--env-file", default=ENV_FILE, help="where PLEX_URL, PLEX_TOKEN and PLEX_PATH_MAP are kept")
    org.add_argument("--plex-url", help="use this Plex address instead of the one in the env file")
    args = ap.parse_args()
    if not args.paths and not (args.apply_plan or args.undo or args.audit):
        ap.error("give one or more folders or files")

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
        "min_free_ram_mb": args.min_free_ram_mb,
        "hdr_encode": not args.keep_hdr,
    })
    MEMORY["floor_mb"] = max(0, cfg["min_free_ram_mb"])

    if args.organize or args.apply_plan or args.undo or args.audit or args.delete:
        org_state = Path(args.state_dir)
        if args.audit:
            audit(args, org_state)
        elif args.delete:
            delete_files(args.paths, org_state, args.yes)
        elif args.undo:
            undo_run(args.undo, org_state, args.yes, args.env_file)
        elif args.apply_plan:
            apply_plan(args.apply_plan, org_state, args.yes, args.env_file)
        else:
            organize(args, org_state)
        return
    if args.rename_suffix:
        rename_suffix(args.paths, cfg, args.yes)
        return
    if args.replace or args.check:
        replace_originals(args.paths, cfg, args.yes, check_only=args.check)
        return
    if args.status:
        for lib in dict.fromkeys(library_root(p) for p in args.paths):
            write_status(lib, echo=True)
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
    files = find_files(args.paths, cfg, {} if (args.dry_run or args.sample) else state,
                       include_done=bool(args.sample))
    if not args.dry_run and not args.sample:
        free = shutil.disk_usage(args.paths[0] if Path(args.paths[0]).is_dir() else Path(args.paths[0]).parent).free
        print(f"NAS free space: {human(free)} (tinyclean keeps at least {cfg['min_free_gb']} GB free).")
        if free < cfg["min_free_gb"] * 1024 ** 3:
            sys.exit(f"Not starting: free space is already below {cfg['min_free_gb']} GB. "
                     f"Replace some originals or clear space first.")
    if in_progress:
        files = [(p, sz) for p, sz in files if p not in in_progress]
    if args.folders and not args.sample:
        first = sorted(dict.fromkeys(show_key(p, args.paths) for p, _ in files),
                       key=lambda k: Path(k).name.lower())[:args.folders]
        files = [(p, sz) for p, sz in files if show_key(p, args.paths) in first]
        print(f"Only the first {len(first)} show/movie folder(s) this run.")
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
    def save_profiles():
        tmp = profiles_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, indent=1, sort_keys=True))
        tmp.replace(profiles_file)

    batching = (not args.profile and args.batch > 0 and sys.stdin.isatty()
                and not args.dry_run and not args.sample)
    show_info = {}
    for p, sz in files:
        k = show_key(p, args.paths)
        c, b = show_info.get(k, (0, 0))
        show_info[k] = (c + 1, b + sz)
    if batching:
        # Work show by show (alphabetical, biggest file first within a show) so each
        # batch of answers is finished before the next question comes up.
        files.sort(key=lambda x: (Path(show_key(x[0], args.paths)).name.lower(), -x[1]))
        unknown = sum(1 for s in show_info if s not in profiles)
        if unknown:
            print(f"{unknown} show(s) have no profile yet. I'll ask about {args.batch} at a time, "
                  f"convert those, then ask about the next {args.batch}.")
    else:
        shows = list(dict.fromkeys(show_key(p, args.paths) for p, _ in files))
        if not args.profile and ask_profiles(shows, profiles, sys.stdin.isatty()):
            save_profiles()

    def file_cfg(path):
        name = args.profile or profiles.get(show_key(path, args.paths), DEFAULT_PROFILE)
        return {**cfg, **PROFILES[name], **cfg["overrides"], "profile_name": name}

    status_root = library_root(args.paths[0])
    last_status = 0.0
    # (the first file refreshes the status straight away, marking itself as converting)
    counts = {"done": 0, "kept": 0, "failed": 0}
    totals = {"saved": 0, "done_bytes": 0, "finished": 0}
    run_start = time.time()
    lf = open(ledger, "a", newline="")
    writer = csv.writer(lf)
    if new_ledger:
        writer.writerow(["time", "file", "result", "orig_bytes", "new_bytes", "seconds", "notes"])

    # ---- how many files at once
    settings_file = state_dir / "settings.json"
    try:
        settings = json.loads(settings_file.read_text())
    except (OSError, ValueError):
        settings = {}
    jobs = args.jobs
    if args.dry_run or args.sample:
        jobs = 1
    elif not jobs:
        last = int(settings.get("jobs", 2))
        if sys.stdin.isatty():
            while True:
                ans = input(f"Convert how many files at the same time? 1-4 [Enter = {last}]: ").strip()
                if not ans:
                    jobs = last
                    break
                if ans.isdigit() and 1 <= int(ans) <= 4:
                    jobs = int(ans)
                    break
                print("  please type a number from 1 to 4")
        else:
            jobs = last
    jobs = max(1, min(int(jobs), 8))
    if not (args.dry_run or args.sample):
        settings["jobs"] = jobs
        try:
            settings_file.write_text(json.dumps(settings))
        except OSError:
            pass
        if jobs > 1:
            print(f"Converting {jobs} files at a time.\n")

    lock = threading.RLock()          # state, ledger, maps, counters, printing
    inflight = {}                     # slot -> (path, size)
    slot_text = {}                    # slot -> latest progress text
    prompting = threading.Event()
    multi = jobs > 1

    def progress_record():
        """The run's progress as one line of JSON, for the mediatool agent."""
        with lock:
            active = []
            for slot in sorted(inflight):
                t = slot_text.get(slot, "")
                m = re.search(r"([\d.]+)%.*?speed (\S+).*?ETA (\S+)", t)
                active.append({"file": str(inflight[slot][0]), "bytes": inflight[slot][1],
                               "pct": float(m.group(1)) if m else 0.0,
                               "speed": m.group(2) if m else "", "eta": m.group(3) if m else ""})
            elapsed = time.time() - run_start
            done_b = totals["done_bytes"]
            eta = elapsed / done_b * (total_bytes - done_b) if done_b else None
            return {"files_total": len(files), "files_finished": totals["finished"],
                    "converted": counts["done"], "failed": counts["failed"], "saved_bytes": totals["saved"],
                    "bytes_total": total_bytes, "bytes_finished": done_b, "elapsed": int(elapsed),
                    "eta_seconds": int(eta) if eta is not None else None, "active": active}

    def show_progress():
        """With several jobs: one combined progress line, redrawn every second.
        With --progress-lines: a JSON progress line every couple of seconds instead."""
        while not finished_all.is_set():
            time.sleep(2 if args.progress_lines else 1)
            if args.progress_lines:
                print(PROGRESS_PREFIX + json.dumps(progress_record()), flush=True)
                continue
            if not tty or prompting.is_set():
                continue
            with lock:
                bits = []
                for slot in sorted(inflight):
                    t = slot_text.get(slot, "")
                    m = re.search(r"([\d.]+)%.*?speed (\S+).*?ETA (\S+)", t)
                    name = inflight[slot][0].stem[:18]
                    bits.append(f"{name}: {m.group(1)}% {m.group(2)} ETA {m.group(3)}" if m else f"{name}: starting")
                if bits:
                    cols = shutil.get_terminal_size((120, 20)).columns
                    line = "  " + "  |  ".join(bits)
                    print("\r" + line[:cols - 1].ljust(cols - 1), end="", flush=True)

    finished_all = threading.Event()

    def emit(lines):
        """Print a finished file's block without tearing the progress line."""
        with lock:
            if multi and tty:
                cols = shutil.get_terminal_size((120, 20)).columns
                print("\r" + " " * (cols - 1) + "\r", end="")
            print("\n".join(lines), flush=True)

    def process(n, path, size, slot):
        out = []

        def say(line=""):
            if multi:
                out.append(line)
            else:
                print(line, flush=True)

        def prog(text):
            slot_text[slot] = text
            if not multi:
                progress(text)

        say(f"[{n}/{len(files)}] {path}  ({human(size)})")
        t0 = time.time()
        try:
            info = ffprobe(cfg["ffprobe"], path)
            if has_tag(info):
                say(f"  [skip] already made by tinyclean ({tag_of(info)}), just renamed\n")
                if not args.dry_run and not args.sample:
                    with lock:
                        state[str(path)] = {"status": "tagged", "mtime": int(path.stat().st_mtime)}
                        save_state()
                return out
            fcfg = file_cfg(path)
            plan = plan_file(path, info, fcfg)
        except Exception as exc:  # noqa: BLE001
            say(f"  [skip] cannot read: {exc}\n")
            with lock:
                counts["failed"] += 1
                if not args.dry_run and not args.sample:
                    state[str(path)] = {"status": "unreadable", "mtime": int(path.stat().st_mtime), "reason": str(exc)}
                    save_state()
            return out
        say(describe(plan))

        if args.dry_run:
            say()
            return out

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
                rc, err = run_ffmpeg(cmd, length, f"  sample ({decode} decode{', low-power' if lp else ''})", prog, plan.get("fps"))
                if rc == 0 or STOP.now or rc == LOW_MEMORY:
                    if rc == 0 and mode == "encode":
                        WORKED[:] = [(rate, lp)]
                    break
            if tty:
                print()
            if rc != 0:
                say("  sample failed: " + (err[-1] if err else f"exit {rc}") + "\n")
                return out
            if mode == "encode" and plan["hdr"]:
                say(f"  hdr   : {hdr_report(cfg, plan, sample_out)[1]}")
            ratio = sample_out.stat().st_size / (size * length / plan["duration"]) if plan["duration"] else 0
            say(f"  projected size: {ratio * 100:.0f}% of original (~{human(size * ratio)}, saves ~{human(size * (1 - ratio))})\n")
            if args.keep_sample:
                keep_dir = Path("/data/_tools") if Path("/data/_tools").is_dir() else state_dir
                kept = keep_dir / f"{path.stem}-sample.mkv"
                shutil.move(str(sample_out), str(kept))
                say(f"  sample kept: {kept}\n")
            sample_out.unlink(missing_ok=True)
            return out

        with lock:
            created_clean = clean_dir_for(path)

        attempts = [plan["video_mode"]]
        if plan["video_mode"] == "encode":
            attempts.append("copy")            # fallback if the re-encode is too big
        elif plan["video_why"].startswith("already efficient"):
            # Some HEVC encodes carry setup data the Matroska muxer refuses to copy
            # ("Could not write header"); a re-encode writes fresh setup data.
            attempts.append("encode")
        result, notes, new_size, mode = None, [], 0, plan["video_mode"]
        for mode in attempts:
            # Every file gets a verified Clean copy, even one that already meets the
            # rules (then it is a straight remux: same streams, new container and name).
            rc, err = 1, []
            for decode, rate, lp in variants(plan, fcfg, mode):
                cmd = build_cmd(plan, fcfg, out_path, mode, rate_mode=rate, decode=decode, low_power=lp)
                label = f"  {'encoding' if mode == 'encode' else 'remuxing'}"
                if mode == "encode" and cfg["encoder"] == "qsv":
                    label += f" ({decode} decode{', low-power' if lp else ''})"
                rc, err = run_ffmpeg(cmd, plan["duration"], label, prog, plan.get("fps"))
                if tty and not multi:
                    print()
                if rc == LOW_MEMORY:
                    # Other attempts (CPU decode, a copy) need as much or more, so stop here.
                    break
                if rc == 0 or STOP.now:
                    if rc == 0 and mode == "encode" and cfg["encoder"] == "qsv":
                        WORKED[:] = [(rate, lp)]
                    if rc == 0 and multi:
                        say(f"{label} done at {(slot_text.get(slot, '').split('speed ')[-1].split()[0] if 'speed ' in slot_text.get(slot, '') else '?')}")
                    break
                notes.append(f"retry: {decode}/{rate}{'/lp' if lp else ''} attempt failed ({(err[-1] if err else 'exit ' + str(rc))[:120]})")
            if STOP.now:
                out_path.unlink(missing_ok=True)
                return out
            if rc == LOW_MEMORY:
                notes.append(f"{err[-1]}; this file needs more memory than the container has, "
                             f"convert it on a worker with more RAM")
                out_path.unlink(missing_ok=True)
                break
            if rc != 0:
                notes.append(f"{mode} failed: {(err[-1] if err else 'exit ' + str(rc))[:200]}")
                out_path.unlink(missing_ok=True)
                continue
            ok, why = verify(cfg, plan, out_path)
            if ok and why != "ok":
                say(f"  note  : {why}")
                notes.append(why)
            if not ok:
                notes.append(f"{mode} check failed: {why}")
                out_path.unlink(missing_ok=True)
                continue
            new_size = out_path.stat().st_size
            if mode == "copy" or new_size <= size * cfg["max_growth"] or plan["video_mode"] == "copy":
                if mode == "encode" and plan["video_mode"] == "copy":
                    say("  note  : the video could not be copied as-is, so it was re-encoded")
                    notes.append("video could not be copied as-is, re-encoded")
                result = "done"
                if mode == "copy" and plan["video_mode"] == "encode":
                    say(f"  note  : re-encode came out too big, kept the original "
                        f"{plan['video'].get('codec_name')} video")
                break
            notes.append(f"{mode} gave {human(new_size)} vs {human(size)}, more than "
                         f"{(cfg['max_growth'] - 1) * 100:.0f}% bigger")
            out_path.unlink(missing_ok=True)

        mtime = int(path.stat().st_mtime)
        if result == "done" and mode == "encode" and (plan["needs_scale"] or plan["dovi"]):
            stem = scaled_name(path.stem, plan['out_w'], plan['out_h']) if plan["needs_scale"] else path.stem
            if plan["dovi"]:
                stem = DV_TOKENS.sub("", stem).strip(" ._-") or stem
            final_path = clean / f"{stem}{SUFFIX}.mkv"
        if result == "done" and mode == "encode" and plan["hdr"]:
            say(f"  hdr   : {hdr_report(cfg, plan, out_path)[1]}")
        with lock:
            if result == "done":
                if final_path.exists():
                    say(f"  [skip] {final_path.name} already exists in Clean, leaving it\n")
                    out_path.unlink(missing_ok=True)
                    counts["failed"] += 1
                    return out
                os.replace(out_path, final_path)
                mapping = read_map(clean)
                mapping[final_path.name] = path.name
                write_map(clean, mapping)
                totals["saved"] += size - new_size
                counts["done"] += 1
                state[str(path)] = {"status": "done", "mtime": mtime, "orig": size, "new": new_size}
                change = (1 - new_size / size) * 100
                how = (f"{change:.0f}% smaller" if change >= 0.5 else
                       f"{-change:.0f}% bigger" if change <= -0.5 else "same size, remuxed")
                say(f"  [done] {human(size)} -> {human(new_size)} ({how}), saved as Clean/{final_path.name}")
            else:
                result = "failed"
                counts["failed"] += 1
                state[str(path)] = {"status": "failed", "mtime": mtime, "reason": "; ".join(notes)}
                say("  [FAILED] " + "; ".join(notes))
            save_state()
            write_readme(path.parent, state)
            # Remove a Clean folder this file's run created but left with nothing in it.
            if created_clean and result != "done" \
                    and not any(v.get("status") == "done" for k, v in state.items() if Path(k).parent == path.parent):
                remove_empty_clean(clean)
            secs = time.time() - t0
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), str(path), result, size, new_size,
                             int(secs), "; ".join(notes)])
            lf.flush()
            totals["done_bytes"] += size
            totals["finished"] += 1
            elapsed = time.time() - run_start
            eta = elapsed / totals["done_bytes"] * (total_bytes - totals["done_bytes"]) if totals["done_bytes"] else 0
            say(f"  overall: {totals['finished']}/{len(files)} files, saved {human(totals['saved'])} so far, "
                f"est. {hms(eta)} left for the rest\n")
        return out

    def worker(n, path, size, slot):
        try:
            lines = process(n, path, size, slot)
        except Exception as exc:  # noqa: BLE001
            lines = [f"[{n}/{len(files)}] {path}", f"  [FAILED] unexpected error: {exc}\n"]
        finally:
            with lock:
                inflight.pop(slot, None)
                slot_text.pop(slot, None)
            free_slots.put(slot)
        if multi and lines:
            emit(lines)

    free_slots = queue.Queue()
    for i in range(1, jobs + 1):
        free_slots.put(i)
    threads = []
    ticker = threading.Thread(target=show_progress, daemon=True)
    if multi or args.progress_lines:
        ticker.start()

    stopped_for_space = False
    for n, (path, size) in enumerate(files, 1):
        if STOP.now or stop_flag.exists():
            emit(["\n[stop] Stop requested; letting the file(s) in progress finish." if stop_flag.exists() and not STOP.now
                  else "\n[stop] Stop requested."])
            break
        # Wait for a free slot (so prompts and space checks happen just before a file starts).
        slot = None
        while slot is None:
            try:
                slot = free_slots.get(timeout=1)
            except queue.Empty:
                if STOP.now:
                    break
        if slot is None:
            break
        if batching and show_key(path, args.paths) not in profiles:
            upcoming = []
            for p, _ in files[n - 1:]:
                k = show_key(p, args.paths)
                if k not in profiles and k not in upcoming:
                    upcoming.append(k)
                    if len(upcoming) == args.batch:
                        break
            prompting.set()
            with lock:
                if multi and tty:
                    print()
                ask_batch(upcoming, profiles, show_info)
                save_profiles()
            prompting.clear()
        if not args.dry_run and not args.sample:
            with lock:
                reserved = sum(sz for _, sz in inflight.values())
            free = shutil.disk_usage(path.parent).free
            if free - reserved - size < cfg["min_free_gb"] * 1024 ** 3:
                emit([f"\n[stop] Only {human(free)} free on the NAS; another copy could push it under "
                      f"{cfg['min_free_gb']} GB. Clear some space (or lower --min-free-gb) and run again."])
                free_slots.put(slot)
                stopped_for_space = True
                break
        with lock:
            inflight[slot] = (path, size)
            if not args.dry_run and not args.sample and time.time() - last_status > 120:
                write_status(status_root, working_on=[p for p, _ in inflight.values()])
                last_status = time.time()
        if multi:
            emit([f"[{n}/{len(files)}] started: {path.name}  ({human(size)})"])
            t = threading.Thread(target=worker, args=(n, path, size, slot), daemon=True)
            t.start()
            threads.append(t)
        else:
            worker(n, path, size, slot)
    for t in threads:
        while t.is_alive():
            t.join(timeout=1)
    finished_all.set()
    if args.progress_lines:
        print(PROGRESS_PREFIX + json.dumps(progress_record()), flush=True)
    if multi and tty:
        print()

    lf.close()
    if not args.dry_run and not args.sample:
        write_status(status_root)
        print("=" * 60)
        print(f"Converted {counts['done']}, failed/skipped {counts['failed']}."
              + (" Stopped early to protect free space." if stopped_for_space else ""))
        print(f"Space the Clean copies would save once originals are removed: {human(totals['saved'])}")
        print(f"Ledger: {ledger}")
        print(f"Status of every show/movie: {status_root / STATUS_NAME}")
        if counts["done"]:
            print("Originals are untouched. Check the Clean folders, then replace originals when you're happy.")


if __name__ == "__main__":
    main()
