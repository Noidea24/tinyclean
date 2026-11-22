"""tinyclean run as a command on a small library, the way the workers run it."""
import csv
import subprocess

import pytest

from conftest import make_video, needs_ffmpeg, probe, run_cli

pytestmark = needs_ffmpeg

CONVERT = ["--encoder", "x265", "--x265-preset", "ultrafast", "--profile", "live", "--jobs", "1",
           "--min-age", "0", "--min-free-gb", "0"]


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def movies(tmp_path):
    lib = tmp_path / "Movies"
    make_video(lib / "Film A (2001)" / "Film.A.2001.mkv", seconds=6, channels=6,
               subs=[("eng", "English"), ("spa", "Spanish")])
    return lib


def ledger_rows(home):
    with open(home / ".tinyclean" / "ledger.csv", newline="") as f:
        return list(csv.DictReader(f))


def test_convert_makes_a_clean_copy_and_leaves_the_original(tc, movies, home):
    original = movies / "Film A (2001)" / "Film.A.2001.mkv"
    before = original.stat().st_size
    out = run_cli(movies, *CONVERT, home=home).stdout
    copy = movies / "Film A (2001)" / "Clean" / "Film.A.2001-noidea.mkv"
    assert copy.is_file(), out
    assert original.stat().st_size == before
    info = probe(copy)
    streams = info["streams"]
    assert [s["codec_name"] for s in streams if s["codec_type"] == "video"] == ["hevc"]
    assert [s["channels"] for s in streams if s["codec_type"] == "audio"] == [2]
    assert [s["tags"]["language"] for s in streams if s["codec_type"] == "subtitle"] == ["eng"]
    assert tc.has_tag(info)
    assert [r["result"] for r in ledger_rows(home)] == ["done"]


def test_check_reports_and_changes_nothing(movies, home):
    run_cli(movies, *CONVERT, home=home)
    out = run_cli(movies, "--check", home=home).stdout
    assert "1 passed the checks" in out and "Check only: nothing changed." in out
    assert (movies / "Film A (2001)" / "Film.A.2001.mkv").is_file()


def test_replace_swaps_in_the_copy_and_removes_the_empty_clean_folder(movies, home):
    run_cli(movies, *CONVERT, home=home)
    out = run_cli(movies, "--replace", "--yes", home=home).stdout
    folder = movies / "Film A (2001)"
    assert "Replaced 1 file(s)" in out
    assert not (folder / "Film.A.2001.mkv").exists()
    assert (folder / "Film.A.2001-noidea.mkv").is_file()
    assert not (folder / "Clean").exists()


def test_a_second_run_finds_nothing_to_do(movies, home):
    run_cli(movies, *CONVERT, home=home)
    run_cli(movies, "--replace", "--yes", home=home)
    out = run_cli(movies, *CONVERT, home=home).stdout
    assert "0 file(s) to look at" in out


def test_replace_needs_confirmation_when_not_interactive(movies, home):
    run_cli(movies, *CONVERT, home=home)
    result = run_cli(movies, "--replace", home=home, check=False)
    assert result.returncode != 0 and "add --yes" in result.stderr
    assert (movies / "Film A (2001)" / "Film.A.2001.mkv").is_file()


def test_a_short_copy_is_never_used_to_replace(movies, home):
    folder = movies / "Film A (2001)"
    (folder / "Clean").mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(folder / "Film.A.2001.mkv"), "-t", "2", "-c", "copy",
                    str(folder / "Clean" / "Film.A.2001-noidea.mkv")], check=True)
    out = run_cli(movies, "--replace", "--yes", home=home).stdout
    assert "PROBLEM" in out and "length differs" in out and "Nothing to replace." in out
    assert (folder / "Film.A.2001.mkv").is_file()
    assert (folder / "Clean" / "Film.A.2001-noidea.mkv").is_file()


def test_a_clean_folder_holding_something_else_is_kept(movies, home):
    run_cli(movies, *CONVERT, home=home)
    note = movies / "Film A (2001)" / "Clean" / "my notes.txt"
    note.write_text("keep me")
    run_cli(movies, "--replace", "--yes", home=home)
    assert note.read_text() == "keep me"


def test_a_failed_move_keeps_the_original(tc, cfg, movies, home, monkeypatch):
    run_cli(movies, *CONVERT, home=home)

    def refuse(*args):
        raise OSError("NAS went away")
    monkeypatch.setattr(tc.os, "replace", refuse)
    tc.replace_originals([str(movies)], cfg, yes=True)
    folder = movies / "Film A (2001)"
    assert (folder / "Film.A.2001.mkv").is_file()
    assert (folder / "Clean" / "Film.A.2001-noidea.mkv").is_file()


def test_a_damaged_original_fails_with_a_reason_and_leaves_no_copy(tmp_path, home):
    lib = tmp_path / "Movies"
    good = make_video(tmp_path / "good.mkv", seconds=40)
    folder = lib / "Broken (2002)"
    folder.mkdir(parents=True)
    data = good.read_bytes()
    (folder / "Broken.mkv").write_bytes(data[:len(data) // 2] + b"\0" * (len(data) - len(data) // 2))
    run_cli(lib, *CONVERT, home=home)
    rows = ledger_rows(home)
    assert rows[-1]["result"] == "failed" and "damaged or incomplete" in rows[-1]["notes"]
    assert not (folder / "Clean").exists()


def test_rename_suffix_restores_the_tag_only_on_tinyclean_files(tmp_path, home):
    lib = tmp_path / "Movies"
    made = make_video(lib / "Film C (2003) {tmdb-3}" / "Film C (2003).mkv", metadata=[("TINYCLEAN", "v8.0")])
    fresh = make_video(lib / "Film D (2004) {tmdb-4}" / "Film D (2004).mkv")
    out = run_cli(lib, "--rename-suffix", "--yes", home=home).stdout
    assert "Renamed 1 file(s)" in out
    assert not made.exists() and made.with_name("Film C (2003)-noidea.mkv").is_file()
    assert fresh.is_file()


def test_already_efficient_hevc_is_remuxed_not_re_encoded(tmp_path, home):
    lib = tmp_path / "Episodes"
    make_video(lib / "Show" / "S01" / "Show - S01E01.mkv", vcodec="libx265", audio=False, extra=["-b:v", "20k"])
    out = run_cli(lib, *CONVERT, home=home).stdout
    assert "already efficient" in out
    copy = lib / "Show" / "S01" / "Clean" / "Show - S01E01-noidea.mkv"
    assert [s["codec_name"] for s in probe(copy)["streams"] if s["codec_type"] == "video"] == ["hevc"]
    assert [r["result"] for r in ledger_rows(home)] == ["done"]


def test_progress_lines_and_folder_limit(tmp_path, home):
    import json
    lib = tmp_path / "Movies"
    make_video(lib / "A (2001)" / "A.mkv")
    make_video(lib / "B (2002)" / "B.mkv")
    out = run_cli(lib, *CONVERT, "--folders", "1", "--progress-lines", home=home).stdout
    assert "Only the first 1 show/movie folder(s) this run." in out
    records = [json.loads(l[len("@progress "):]) for l in out.splitlines() if l.startswith("@progress ")]
    assert records and records[-1]["files_total"] == 1 and records[-1]["files_finished"] == 1
    assert records[-1]["converted"] == 1 and records[-1]["active"] == []
    assert (lib / "A (2001)" / "Clean").is_dir() and not (lib / "B (2002)" / "Clean").exists()


def test_shows_are_listed_and_their_profiles_saved(tc, tmp_path, home):
    """The dashboard's per-show choice: list shows with their saved profile, save new ones."""
    import json
    lib = tmp_path / "Episodes"
    make_video(lib / "Recess" / "S01" / "Recess - S01E01.mkv", seconds=2)
    make_video(lib / "The Wire" / "Season 1" / "The Wire - S01E01.mkv", seconds=2)
    shows = json.loads(run_cli("--list-shows", "--min-age", "0", "--", lib, home=home).stdout)
    assert [(s["name"], s["files"], s["profile"]) for s in shows] == [("Recess", 1, None), ("The Wire", 1, None)]
    saved = run_cli("--set-profiles", json.dumps({shows[0]["show"]: "animated"}), home=home).stdout
    assert json.loads(saved) == {"saved": 1}
    again = json.loads(run_cli("--list-shows", "--min-age", "0", "--", lib, home=home).stdout)
    assert [s["profile"] for s in again] == ["animated", None]
    bad = run_cli("--set-profiles", json.dumps({"x": "cartoon"}), home=home, check=False)
    assert bad.returncode != 0
