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
| Container | Matroska (.mkv), named `<original>-noidea.mkv`, with a hidden `TINYCLEAN` tag so it is recognised even after Sonarr/Radarr rename it. |

Every file gets a Clean copy, so a whole show ends up in one consistent format.
Files that already meet the rules are remuxed (no quality loss).

## Safety

- Originals are never touched by a conversion. Copies go to a `Clean/` folder
  next to the source, hidden from Plex and Jellyfin (`.plexignore`, `.ignore`).
- Each copy is written as a hidden temp file, verified (duration, track counts,
  HDR), then renamed into place.
- A free-space floor (default 500 GB) is checked before every file.
- Replacing moves the Clean copy into place first and deletes the original
  second, so a failure partway (a NAS hiccup) never leaves a video missing.
  An emptied Clean folder is removed only when nothing but tinyclean's own
  files are left in it.
- Lengths are compared on the video track, so a commentary or subtitle track
  that runs past the film and gets dropped does not fail the check. A copy
  that is still shorter always fails: if the original has no video past where
  the copy stops, the reason says the original looks damaged or incomplete and
  should be re-downloaded. The original is never replaced by a shorter copy.
- An already-efficient HEVC video is copied as-is; if the copy is refused
  ("Could not write header"), the video is re-encoded instead and kept even if
  slightly bigger.
- A memory floor (default 256 MB, `--min-free-ram-mb`) is watched while ffmpeg
  runs. If the container's free memory stays under it, that file's ffmpeg is
  stopped and the file is marked failed with the reason, instead of the
  container running out of memory and swap and no longer answering its console.
  No retry is tried for that file, since the other attempts need as much memory.
  Convert it on a worker with more RAM, then run with `--retry-failed`.
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
tinyclean "/data/Episodes"                    # asks how many at once, then animated/live 10 shows at a time
tinyclean --jobs 3 "/data/Episodes"           # convert 3 files at the same time (remembered)
tinyclean --profile live "/data/Movies"       # no questions
tinyclean --sample 3 "/data/Movies/Some Film" # 60 s test clips, projected savings
tinyclean --dry-run "/data/Episodes/Show"     # show the plan, change nothing
tinyclean --replace "/data/Episodes/Show"     # swap originals for Clean copies (asks first)
tinyclean --status "/data/Episodes"           # refresh and print the library status file
tinyclean --rename-suffix "/data/Episodes"    # old -Clean files -> -noidea, tagged (asks first)
tinyclean --organize rename "/data/Movies"    # names from Plex's matches; dry run (see Organize)
```

Each library gets `_tinyclean-status.txt` at the top (refreshed every couple of
minutes during a run, and after `--replace`). It groups every show or movie as
ready to review, converting now, partly done, needs attention, replaced, or not
started, with sizes, so you can review and replace one folder at a time.

| Profile | Preset | Quality HD / SD |
|---|---|---|
| animated | faster | 24 / 23 |
| live | faster | 22 / 22 |

Other options: `--jobs N`, `--batch N`, `--min-free-gb`, `--keep-hdr`, `--keep-sample`,
`--limit`, `--retry-failed`, `--state-dir`, `--check`, `--rename-suffix`.
Tagging old files needs `mkvpropedit` (`apt install mkvtoolnix`). Ctrl+C stops cleanly; touching
`~/.tinyclean/STOP` finishes the current file, then stops.

## Organize (names and folders from Plex)

Plex has already matched every file, so tinyclean can name files from Plex's
matches instead of guessing from file names. It only reads from Plex.

Settings live in `/etc/mediatool/mediatool.env` (owner `root`, group `media`,
mode `640`), never in the repo:

```
PLEX_URL=http://<plex address>:32400
PLEX_TOKEN=<your Plex token>
PLEX_PATH_MAP=<library path inside Plex>=<same folder on this machine>
```

`PLEX_PATH_MAP` is only needed when Plex sees the library under a different
path; several pairs are separated by commas, e.g. `/movies=/data/Movies,/tv=/data/Episodes`.

```
tinyclean --organize rename "/data/Movies"                  # dry run: rename files where they are
tinyclean --organize restructure --folders 5 "/data/Movies" # dry run: also move into standard folders, 5 folders only
tinyclean --organize newtree "/data/Movies"                 # dry run: hard-link into /data/Movies-new
tinyclean --apply-plan latest                               # carry out the last dry run (asks first)
tinyclean --undo latest                                     # put the last applied run back (asks first)
```

| Mode | What changes |
|---|---|
| `rename` | File names only; folders stay as they are. |
| `restructure` | Names and folders: `Movie (Year) {tmdb-…}/` and `Show (Year) {tvdb-…}/Season 01/`. Emptied old folders are removed. |
| `newtree` | Nothing in the library. Every video is hard-linked into a new tree next to it (`<library>-new`, or `--new-root-suffix`), already organised. Hard links take no extra space. Point Plex at the new tree when you are happy, then delete the old one. |

Every `--organize` run is a dry run: it prints every change with the skipped
files and their reasons at the bottom, and saves the plan in
`~/.tinyclean/plans/`. `--apply-plan` carries out exactly that plan, refuses one
older than 24 hours, and skips any file that changed since. Each applied run
writes an undo log in `~/.tinyclean/undo/`. Undoing a `newtree` run only
removes the new names, never the last copy of a video.

Subtitle and `.nfo` files named after a video move with it. Files ending in
`-noidea` keep the suffix. Skipped, with the reason: files Plex has not matched,
movies split into several files or with several versions, files holding
several episodes, folders with Clean copies waiting for `--replace`, and any
change that would clash with another file's name.

Templates (the defaults follow Plex's naming guide):

| Option | Default |
|---|---|
| `--movie-folder` | `{title} ({year}) {tmdb-{tmdb}}` |
| `--movie-file` | `{title} ({year})` |
| `--show-folder` | `{show} ({year}) {tvdb-{tvdb}}` |
| `--season-folder` | `Season {season}` |
| `--episode-file` | `{show} ({year}) - S{season}E{episode} - {episode_title}` |

Tokens: `title year imdb tmdb tvdb show season episode episode_title resolution edition`.
Seasons and episodes are two digits; `{episode:3}` gives three. A bracket
group whose token has no value disappears, so `{title} ({imdb}) {year}` gives
`Heat (tt0113277) 1995`, or `Heat 1995` when Plex has no IMDb id. Examples:
`--movie-file "{title} ({imdb}) {year}"`, `--movie-file "{title} {year} ({imdb})"`.

After applying, rescan the library in Plex and Refresh in Sonarr/Radarr.

## Updating

On each worker, as root:

```
t=$(mktemp) && curl -fsSL https://raw.githubusercontent.com/Noidea24/tinyclean/main/tinyclean.py -o "$t" && install -m 755 "$t" /usr/local/bin/tinyclean && rm -f "$t"
```

A fresh temporary file each time, so a copy another user left in /tmp cannot block the download.

## With Sonarr and Radarr

Leave `-noidea` out of Sonarr's and Radarr's naming formats. tinyclean skips
any file ending in `-noidea`, so if they added it, new downloads would look
finished and never be converted. The suffix is tinyclean's to give:

- New downloads arrive with plain names; tinyclean converts them, and
  `--replace` names the result `...-noidea.mkv`.
- If Sonarr or Radarr renames finished files (a library import, or Rename
  Files), they lose the suffix. Each one still carries tinyclean's hidden tag,
  so it is never converted twice, and `tinyclean --rename-suffix <folder>`
  finds them by that tag and gives `-noidea` back. Files without the tag are
  left alone.
- After either, Refresh in Sonarr/Radarr so they pick up the new names.

## Tests

`python3 -m pytest -q` runs the test suite: names and helpers, what is planned
for each kind of file, the checks a copy must pass, the memory guard, and full
runs of the command on a small generated library (convert, check, replace, a
short copy refused, a damaged original, a failed move, `--rename-suffix`), and
the organizer against a small fake Plex (templates, every mode, clashes, apply,
undo, stale plans). The
tests make their own small videos, so they need ffmpeg and ffprobe on PATH.
GitHub Actions runs them on Python 3.11 and 3.12 for every push and tag.
