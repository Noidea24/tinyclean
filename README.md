# tinyclean

Shrinks and normalizes a home media library for comfortable TV playback, using
Intel Quick Sync. Built for "the tiny server" homelab: Proxmox LXC workers
reading and writing a Synology NAS over NFS.

## What it does to each video

| Part | Rule |
|---|---|
| Video | H.265 (HEVC) on Quick Sync. 4K is scaled to fit 1920x1080 on the iGPU. Interlaced DVD material is deinterlaced. 10-bit stays 10-bit. |
| HDR | HDR10 / HLG is re-encoded at 1080p and stays HDR, then checked. Dolby Vision with no HDR10 base layer is never touched. |
| Audio | Keeps one English and one Japanese track, plus the original-language default track (e.g. Korean). Surround becomes a "night mode" stereo downmix: dialogue up, LFE out, loudness levelled, so there is no volume roller coaster. AAC 192k. |
| Subtitles | Keeps English (forced, full, SDH; SRT, ASS, PGS). A lone untagged track is assumed English. Everything else is dropped. Only forced tracks are set to show by default. |
| Container | Matroska (.mkv), named `<original>-Clean.mkv`. |

Every file gets a Clean copy, so a whole show ends up in one consistent format.
Files that already meet the rules are remuxed (no quality loss).

## Safety

- Originals are never touched by a conversion. Copies go to a `Clean/` folder
  next to the source, hidden from Plex and Jellyfin (`.plexignore`, `.ignore`).
- Each copy is written as a hidden temp file, verified (duration, track counts,
  HDR), then renamed into place.
- A free-space floor (default 500 GB) is checked before every file.
- `--replace` first checks every original against its Clean copy (length,
  resolution, audio, subtitles, HDR) and shows the facts for each pair. Copies
  that fail are left alone. It also warns about the same episode appearing
  twice, and lists videos that have no Clean copy yet. Nothing is deleted until
  you type `replace`. `--check` does the same report without changing anything.

## Install

Needs Python 3 and `jellyfin-ffmpeg` (or any ffmpeg with Quick Sync), plus
access to `/dev/dri/renderD128`.

```
install -m 755 tinyclean.py /usr/local/bin/tinyclean
```

## Use

```
tinyclean "/data/Episodes"                    # asks animated/live 10 shows at a time
tinyclean --profile live "/data/Movies"       # no questions
tinyclean --sample 3 "/data/Movies/Some Film" # 60 s test clips, projected savings
tinyclean --dry-run "/data/Episodes/Show"     # show the plan, change nothing
tinyclean --replace "/data/Episodes/Show"     # swap originals for Clean copies (asks first)
tinyclean --status "/data/Episodes"           # refresh and print the library status file
```

Each library gets `_tinyclean-status.txt` at the top (refreshed every couple of
minutes during a run, and after `--replace`). It groups every show or movie as
ready to review, converting now, partly done, needs attention, replaced, or not
started, with sizes, so you can review and replace one folder at a time.

| Profile | Preset | Quality HD / SD |
|---|---|---|
| animated | faster | 24 / 23 |
| live | faster | 22 / 22 |

Other options: `--batch N`, `--min-free-gb`, `--keep-hdr`, `--keep-sample`,
`--limit`, `--retry-failed`, `--state-dir`. Ctrl+C stops cleanly; touching
`~/.tinyclean/STOP` finishes the current file, then stops.
