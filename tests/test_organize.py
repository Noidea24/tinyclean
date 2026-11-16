"""The organizer: names from Plex's matches, plans, applying, undoing. Plex is faked; no ffmpeg needed."""
import http.server
import json
import os
import threading
import time

import pytest

from conftest import run_cli


# ------------------------------------------------------------------ fixtures --

def movie(title, year, file, guids=("tmdb://603", "imdb://tt0133093"), parts=1, resolution="1080"):
    return {"title": title, "year": year, "Guid": [{"id": g} for g in guids],
            "Media": [{"videoResolution": resolution, "Part": [{"file": file}] * parts}]}


def show(key, title, year, guids=("tvdb://81189",)):
    return {"ratingKey": key, "title": title, "year": year, "Guid": [{"id": g} for g in guids]}


def episode(show_key, season, number, title, file):
    return {"grandparentRatingKey": show_key, "parentIndex": season, "index": number, "title": title,
            "Media": [{"videoResolution": "720", "Part": [{"file": file}]}]}


class FakePlex:
    """Answers the same questions as tinyclean's Plex class, from lists."""

    def __init__(self, movies_root, shows_root, movies=(), shows=(), episodes=()):
        self.movies_root, self.shows_root = str(movies_root), str(shows_root)
        self.movies, self.shows, self.episodes = list(movies), list(shows), list(episodes)

    sections_extra = ()

    def sections(self):
        return [{"key": "1", "type": "movie", "title": "Movies", "Location": [{"path": self.movies_root}]},
                {"key": "2", "type": "show", "title": "Shows", "Location": [{"path": self.shows_root}]},
                *self.sections_extra]

    def get(self, path, **params):
        return {"machineIdentifier": "machine-1"} if path == "/identity" else {}

    def all(self, path, type, includeGuids=0):
        return {1: self.movies, 2: self.shows, 4: self.episodes}[type]


def touch(path, data=b"video"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def lib(tmp_path):
    """A small messy library and Plex's view of it."""
    movies, shows = tmp_path / "Movies", tmp_path / "Shows"
    matrix = touch(movies / "the.matrix.1999.1080p" / "The.Matrix.1999.1080p.BluRay-noidea.mkv")
    touch(matrix.with_name("The.Matrix.1999.1080p.BluRay-noidea.en.srt"), b"subs")
    heat = touch(movies / "Heat" / "heat.mkv")
    ep1 = touch(shows / "breaking bad" / "S1" / "bb.s01e01.mkv")
    ep2 = touch(shows / "breaking bad" / "S1" / "bb.s01e02.mkv")
    plex = FakePlex(movies, shows,
                    movies=[movie("The Matrix", 1999, str(matrix)),
                            movie("Heat", 1995, str(heat), guids=("tmdb://949", "imdb://tt0113277"))],
                    shows=[show("10", "Breaking Bad", 2008)],
                    episodes=[episode("10", 1, 1, "Pilot", str(ep1)),
                              episode("10", 1, 2, "Cat's in the Bag...", str(ep2))])
    return {"movies": movies, "shows": shows, "matrix": matrix, "heat": heat, "ep1": ep1, "ep2": ep2,
            "plex": plex}


def plan(tc, lib, mode, scope=(), templates=None, folders=0):
    entries, skipped = tc.read_library(lib["plex"], lambda p: p)
    return tc.make_plan(entries, skipped, mode, templates or dict(tc.TEMPLATES), scope, folders)


def save(tc, state, p):
    (state / "plans").mkdir(parents=True, exist_ok=True)
    path = state / "plans" / "plan-test.json"
    path.write_text(json.dumps(p))
    return path


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ----------------------------------------------------------------- templates --

def test_default_templates_follow_plex_naming(tc):
    v = {"title": "The Matrix", "year": 1999, "tmdb": "603", "imdb": "tt0133093"}
    assert tc.render(tc.TEMPLATES["movie_folder"], v) == "The Matrix (1999) {tmdb-603}"
    assert tc.render(tc.TEMPLATES["movie_file"], v) == "The Matrix (1999)"
    e = {"show": "Breaking Bad", "year": 2008, "tvdb": "81189", "season": 1, "episode": 2,
         "episode_title": "Cat's in the Bag..."}
    assert tc.render(tc.TEMPLATES["show_folder"], e) == "Breaking Bad (2008) {tvdb-81189}"
    assert tc.render(tc.TEMPLATES["season_folder"], e) == "Season 01"
    assert tc.render(tc.TEMPLATES["episode_file"], e) == "Breaking Bad (2008) - S01E02 - Cat's in the Bag"


def test_custom_templates_put_the_id_and_year_anywhere(tc):
    v = {"title": "The Matrix", "year": 1999, "imdb": "tt0133093"}
    assert tc.render("{title} ({imdb}) {year}", v) == "The Matrix (tt0133093) 1999"
    assert tc.render("{title} {year} ({imdb})", v) == "The Matrix 1999 (tt0133093)"
    assert tc.render("{title} ({year}) [imdbid-{imdb}]", v) == "The Matrix (1999) [imdbid-tt0133093]"


def test_missing_values_drop_their_brackets(tc):
    v = {"title": "Heat", "year": 1995}
    assert tc.render("{title} ({year}) {tmdb-{tmdb}}", v) == "Heat (1995)"
    assert tc.render("{title} ({imdb}) {year}", v) == "Heat 1995"
    assert tc.render("{show} - S{season}E{episode} - {episode_title}",
                     {"show": "X", "season": 1, "episode": 3}) == "X - S01E03"


def test_names_lose_characters_that_are_not_allowed(tc):
    assert tc.render("{title}", {"title": "Mission: Impossible / Fallout?"}) == "Mission - Impossible - Fallout"
    assert tc.render("Season {season:1}", {"season": 3}) == "Season 3"
    assert tc.render("E{episode:3}", {"episode": 7}) == "E007"


def test_unknown_tokens_are_reported(tc):
    assert tc.template_problems("{title} {yaer}") == ["yaer"]
    assert tc.template_problems(tc.TEMPLATES["movie_folder"]) == []


def test_path_mapper_uses_the_longest_prefix(tc):
    m = tc.path_mapper([("/data", "/mnt/a"), ("/data/movies", "/mnt/m")])
    assert m("/data/movies/x.mkv") == "/mnt/m/x.mkv"
    assert m("/data/tv/y.mkv") == "/mnt/a/tv/y.mkv"
    assert m("/database/z.mkv") == "/database/z.mkv"


def test_env_file(tc, tmp_path):
    env = tmp_path / "mediatool.env"
    env.write_text("# comment\nPLEX_URL=http://plex.lan:32400\nPLEX_TOKEN='abc'\n")
    assert tc.load_env(env) == {"PLEX_URL": "http://plex.lan:32400", "PLEX_TOKEN": "abc"}
    assert tc.load_env(tmp_path / "missing") == {}


# --------------------------------------------------------------------- plans --

def test_rename_keeps_folders_and_the_noidea_suffix_and_moves_sidecars(tc, lib):
    p = plan(tc, lib, "rename")
    by_old = {it["old"]: it for it in p["items"]}
    m = by_old[str(lib["matrix"])]
    assert m["new"] == str(lib["matrix"].with_name("The Matrix (1999)-noidea.mkv"))
    assert [os.path.basename(n) for _, n in m["sidecars"]] == ["The Matrix (1999)-noidea.en.srt"]
    assert by_old[str(lib["ep2"])]["new"].endswith("S1/Breaking Bad (2008) - S01E02 - Cat's in the Bag.mkv")


def test_restructure_builds_plex_folders(tc, lib):
    p = plan(tc, lib, "restructure")
    news = sorted(os.path.relpath(it["new"], it["root"]) for it in p["items"])
    assert news == ["Breaking Bad (2008) {tvdb-81189}/Season 01/Breaking Bad (2008) - S01E01 - Pilot.mkv",
                    "Breaking Bad (2008) {tvdb-81189}/Season 01/Breaking Bad (2008) - S01E02 - Cat's in the Bag.mkv",
                    "Heat (1995) {tmdb-949}/Heat (1995).mkv",
                    "The Matrix (1999) {tmdb-603}/The Matrix (1999)-noidea.mkv"]


def test_a_second_plan_after_applying_finds_nothing(tc, lib, tmp_path):
    state = tmp_path / "state"
    tc.apply_plan(str(save(tc, state, plan(tc, lib, "restructure"))), state, yes=True)
    lib["plex"].movies[0]["Media"][0]["Part"][0]["file"] = str(
        lib["movies"] / "The Matrix (1999) {tmdb-603}" / "The Matrix (1999)-noidea.mkv")
    lib["plex"].movies[1]["Media"][0]["Part"][0]["file"] = str(
        lib["movies"] / "Heat (1995) {tmdb-949}" / "Heat (1995).mkv")
    for ep, n, t in ((lib["plex"].episodes[0], 1, "Pilot"), (lib["plex"].episodes[1], 2, "Cat's in the Bag")):
        ep["Media"][0]["Part"][0]["file"] = str(
            lib["shows"] / "Breaking Bad (2008) {tvdb-81189}" / "Season 01" / f"Breaking Bad (2008) - S01E0{n} - {t}.mkv")
    again = plan(tc, lib, "restructure")
    assert again["items"] == [] and again["unchanged"] == 4


def test_scope_and_folder_limit(tc, lib):
    assert {it["old"] for it in plan(tc, lib, "rename", scope=[lib["shows"]])["items"]} == \
        {str(lib["ep1"]), str(lib["ep2"])}
    limited = plan(tc, lib, "rename", folders=2)
    assert len({(it["root"], it["group"]) for it in limited["items"]}) == 2


def test_unmatched_split_and_multi_episode_files_are_skipped(tc, lib):
    odd = touch(lib["movies"] / "odd" / "odd.mkv")
    two = touch(lib["movies"] / "Two" / "cd1.mkv")
    double = touch(lib["shows"] / "breaking bad" / "S1" / "bb.s01e03e04.mkv")
    lib["plex"].movies += [movie("Odd", 2000, str(odd), guids=()), movie("Two", 2001, str(two), parts=2)]
    lib["plex"].episodes += [episode("10", 1, 3, "A", str(double)), episode("10", 1, 4, "B", str(double))]
    p = plan(tc, lib, "rename")
    why = dict(p["skipped"])
    assert why[str(odd)] == "not matched in Plex"
    assert why[str(two)].startswith("several files")
    new = {it["old"]: it["new"] for it in p["items"]}[str(double)]
    assert new.endswith("/Breaking Bad (2008) - S01E03-E04.mkv")


def test_clashes_and_waiting_clean_copies_are_skipped(tc, lib):
    touch(lib["heat"].with_name("Heat (1995).mkv"), b"another file")
    touch(lib["ep1"].parent / "Clean" / "bb.s01e01-noidea.mkv", b"copy")
    why = dict(plan(tc, lib, "rename")["skipped"])
    assert why[str(lib["heat"])].startswith("name already in use")
    assert "run --replace first" in why[str(lib["ep1"])]


def test_two_files_aiming_at_one_name_are_both_skipped(tc, lib):
    lib["plex"].episodes[1]["index"] = 1
    lib["plex"].episodes[1]["title"] = "Pilot"
    why = dict(plan(tc, lib, "rename")["skipped"])
    assert "same name" in why[str(lib["ep1"])] and "same name" in why[str(lib["ep2"])]


# ------------------------------------------------------------ apply and undo --

def test_apply_and_undo_restructure(tc, lib, tmp_path):
    state = tmp_path / "state"
    before = tree(tmp_path / "Movies") + tree(tmp_path / "Shows")
    tc.apply_plan(str(save(tc, state, plan(tc, lib, "restructure"))), state, yes=True)
    assert tree(lib["movies"]) == ["Heat (1995) {tmdb-949}/Heat (1995).mkv",
                                   "The Matrix (1999) {tmdb-603}/The Matrix (1999)-noidea.en.srt",
                                   "The Matrix (1999) {tmdb-603}/The Matrix (1999)-noidea.mkv"]
    assert not (lib["movies"] / "Heat").exists(), "the emptied old folder is removed"
    assert lib["movies"].is_dir() and lib["shows"].is_dir()
    tc.undo_run("latest", state, yes=True)
    assert tree(tmp_path / "Movies") + tree(tmp_path / "Shows") == before
    assert not (lib["movies"] / "Heat (1995) {tmdb-949}").exists()
    assert lib["movies"].is_dir()
    assert list((state / "undo").glob("*.undone.json"))


def test_newtree_hard_links_and_leaves_the_library_alone(tc, lib, tmp_path):
    state = tmp_path / "state"
    before = tree(lib["movies"])
    tc.apply_plan(str(save(tc, state, plan(tc, lib, "newtree"))), state, yes=True)
    assert tree(lib["movies"]) == before
    new = tmp_path / "Movies-new" / "The Matrix (1999) {tmdb-603}" / "The Matrix (1999)-noidea.mkv"
    assert new.is_file() and os.path.samefile(new, lib["matrix"]) and lib["matrix"].stat().st_nlink == 2
    tc.undo_run("latest", state, yes=True)
    assert lib["matrix"].is_file() and lib["matrix"].stat().st_nlink == 1
    assert not (tmp_path / "Movies-new" / "The Matrix (1999) {tmdb-603}").exists()
    assert tree(lib["movies"]) == before


def test_undo_never_removes_the_last_copy(tc, lib, tmp_path):
    state = tmp_path / "state"
    tc.apply_plan(str(save(tc, state, plan(tc, lib, "newtree", scope=[lib["movies"] / "Heat"]))), state, yes=True)
    lib["heat"].unlink()                        # the original went away; the new name is the only copy
    tc.undo_run("latest", state, yes=True)
    assert (tmp_path / "Movies-new" / "Heat (1995) {tmdb-949}" / "Heat (1995).mkv").is_file()


def test_apply_skips_files_changed_since_the_dry_run(tc, lib, tmp_path):
    state = tmp_path / "state"
    path = save(tc, state, plan(tc, lib, "rename"))
    lib["heat"].write_bytes(b"a different, longer file")
    tc.apply_plan(str(path), state, yes=True)
    assert lib["heat"].is_file()
    assert lib["matrix"].with_name("The Matrix (1999)-noidea.mkv").is_file()


def test_an_old_plan_is_refused(tc, lib, tmp_path):
    state = tmp_path / "state"
    p = plan(tc, lib, "rename")
    p["created"] = time.time() - 25 * 3600
    with pytest.raises(SystemExit, match="hours old"):
        tc.apply_plan(str(save(tc, state, p)), state, yes=True)
    assert lib["heat"].is_file()


# ------------------------------------------------- the command, with real HTTP --

@pytest.fixture
def plex_server(lib):
    """A tiny HTTP server answering like Plex, from the fake library. Checks the token."""
    fake = lib["plex"]
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            seen.append(self.path)
            if self.headers.get("X-Plex-Token") != "secret":
                self.send_response(401)
                self.end_headers()
                return
            if u.path == "/library/sections":
                body = {"Directory": fake.sections()}
            else:
                items = fake.all(u.path, int(q["type"]))
                start, size = int(q.get("X-Plex-Container-Start", 0)), int(q.get("X-Plex-Container-Size", 500))
                body = {"Metadata": items[start:start + size], "totalSize": len(items)}
            data = json.dumps({"MediaContainer": body}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_command_dry_run_apply_and_undo(lib, plex_server, tmp_path):
    url, seen = plex_server
    home = tmp_path / "home"
    home.mkdir()
    env = tmp_path / "mediatool.env"
    env.write_text(f"PLEX_URL={url}\nPLEX_TOKEN=secret\n")
    out = run_cli(lib["movies"], "--organize", "restructure", "--env-file", env, home=home).stdout
    assert "Will move 2 video(s), 1 subtitle/info file(s)" in out and "Dry run only: nothing changed." in out
    assert "secret" not in out
    assert lib["heat"].is_file()
    result = run_cli("--apply-plan", "latest", home=home, check=False)
    assert result.returncode != 0 and "add --yes" in result.stderr
    out = run_cli("--apply-plan", "latest", "--yes", home=home).stdout
    assert "2 of 2 video(s) moved" in out
    assert (lib["movies"] / "Heat (1995) {tmdb-949}" / "Heat (1995).mkv").is_file()
    out = run_cli("--undo", "latest", "--yes", home=home).stdout
    assert "Put back 3 of 3" in out and lib["heat"].is_file()


def test_command_with_custom_template_and_no_plex_settings(lib, plex_server, tmp_path):
    url, _ = plex_server
    home = tmp_path / "home"
    home.mkdir()
    env = tmp_path / "mediatool.env"
    env.write_text(f"PLEX_URL={url}\nPLEX_TOKEN=secret\n")
    out = run_cli(lib["movies"], "--organize", "rename", "--movie-file", "{title} ({imdb}) {year}",
                  "--env-file", env, home=home).stdout
    assert "Heat (tt0113277) 1995.mkv" in out
    bad = run_cli(lib["movies"], "--organize", "rename", "--movie-file", "{titel}", "--env-file", env,
                  home=home, check=False)
    assert bad.returncode != 0 and "titel" in bad.stderr
    missing = run_cli(lib["movies"], "--organize", "rename", "--env-file", tmp_path / "nope", home=home, check=False)
    assert missing.returncode != 0 and "PLEX_TOKEN" in missing.stderr


def test_command_needs_paths_except_for_apply_and_undo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = run_cli(home=home, check=False)
    assert r.returncode != 0 and "give one or more folders" in r.stderr


def test_command_explains_when_plex_sees_other_paths(lib, plex_server, tmp_path):
    url, _ = plex_server
    home = tmp_path / "home"
    home.mkdir()
    env = tmp_path / "mediatool.env"
    env.write_text(f"PLEX_URL={url}\nPLEX_TOKEN=secret\n")
    elsewhere = tmp_path / "Elsewhere"
    elsewhere.mkdir()
    out = run_cli(elsewhere, "--organize", "rename", "--env-file", env, home=home).stdout
    assert f"[movie]: {lib['movies']}" in out and "Plex knows 4 video file(s)" in out
    assert "None of Plex's files are under" in out and "PLEX_PATH_MAP" in out


# ----------------------------------------------------- the rest of a folder --

def test_restructure_takes_posters_extras_and_subfolders_along(tc, lib, tmp_path):
    folder = lib["matrix"].parent
    touch(folder / "poster.jpg", b"jpg")
    touch(folder / "movie.nfo", b"nfo")
    touch(folder / "Subs" / "2_English.srt", b"srt")
    touch(folder / "Featurettes" / "Making of.mkv", b"video")
    touch(lib["shows"] / "breaking bad" / "poster.jpg", b"jpg")
    touch(lib["shows"] / "breaking bad" / "S1" / "season01-poster.jpg", b"jpg")
    state = tmp_path / "state"
    before = tree(tmp_path / "Movies") + tree(tmp_path / "Shows")
    p = plan(tc, lib, "restructure")
    assert len(p["extras"]) == 6
    tc.apply_plan(str(save(tc, state, p)), state, yes=True)
    assert not folder.exists() and not (lib["shows"] / "breaking bad").exists()
    new = lib["movies"] / "The Matrix (1999) {tmdb-603}"
    assert sorted(str(f.relative_to(new)) for f in new.rglob("*") if f.is_file()) == [
        "Featurettes/Making of.mkv", "Subs/2_English.srt", "The Matrix (1999)-noidea.en.srt",
        "The Matrix (1999)-noidea.mkv", "movie.nfo", "poster.jpg"]
    show = lib["shows"] / "Breaking Bad (2008) {tvdb-81189}"
    assert (show / "poster.jpg").is_file() and (show / "Season 01" / "season01-poster.jpg").is_file()
    tc.undo_run("latest", state, yes=True)
    assert tree(tmp_path / "Movies") + tree(tmp_path / "Shows") == before
    assert not new.exists() and not show.exists()


def test_a_folder_holding_another_movie_keeps_its_other_files(tc, lib):
    touch(lib["heat"].parent / "poster.jpg", b"jpg")
    touch(lib["heat"].parent / "Heat 2.mkv", b"another movie")
    p = plan(tc, lib, "restructure")
    assert not [x for x in p["extras"] if "Heat" in x["old"]]
    assert any("Heat 2.mkv" in why for _, why in p["skipped"])


def test_moved_files_plex_still_lists_are_not_duplicates(tc, lib):
    gone = str(lib["movies"] / "Old Name" / "matrix.mkv")
    lib["plex"].movies[0]["Media"].append({"videoResolution": "1080", "Part": [{"file": gone}]})
    lib["plex"].episodes[0]["Media"].append({"videoResolution": "720", "Part": [{"file": gone + ".ep"}]})
    p = plan(tc, lib, "rename")
    assert not p["skipped"]
    assert {it["old"] for it in p["items"]} >= {str(lib["matrix"]), str(lib["ep1"])}


def test_undo_latest_walks_back_one_run_at_a_time(tc, lib, tmp_path):
    state = tmp_path / "state"
    before = tree(tmp_path / "Movies")
    tc.apply_plan(str(save(tc, state, plan(tc, lib, "restructure", scope=[lib["heat"].parent]))), state, yes=True)
    time.sleep(1.1)
    p2 = plan(tc, lib, "restructure", scope=[lib["matrix"].parent])
    (state / "plans" / "plan-2.json").write_text(json.dumps(p2))
    tc.apply_plan(str(state / "plans" / "plan-2.json"), state, yes=True)
    tc.undo_run("latest", state, yes=True)
    tc.undo_run("latest", state, yes=True)
    assert tree(tmp_path / "Movies") == before
    with pytest.raises(SystemExit, match="No undo log"):
        tc.undo_run("latest", state, yes=True)


# ------------------------------------------------- lessons from the real library --

def test_a_two_episode_file_is_named_from_its_file_name(tc, lib):
    two = touch(lib["shows"] / "breaking bad" / "S1" / "bb - S01e03-04 - A + B-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 3, "Wrong Title", str(two)))
    p = plan(tc, lib, "restructure")
    new = {it["old"]: it["new"] for it in p["items"]}[str(two)]
    assert new.endswith("Season 01/Breaking Bad (2008) - S01E03-E04-noidea.mkv"), new
    entries, skipped = tc.read_library(lib["plex"], lambda x: x)
    off = tc.make_plan(entries, skipped, "restructure", dict(tc.TEMPLATES), (), two_episode="skip")
    assert "two-episode naming is off" in dict(off["skipped"])[str(two)]


def test_only_files_with_their_own_waiting_copy_are_held_back(tc, lib):
    clean = lib["ep1"].parent / "Clean"
    touch(clean / "bb.s01e01-noidea.mkv", b"copy")
    why = dict(plan(tc, lib, "rename")["skipped"])
    assert "Clean copy is waiting" in why[str(lib["ep1"])]
    assert str(lib["ep2"]) not in why


def test_a_leftover_clean_folder_with_only_notes_goes_and_frees_the_old_folder(tc, lib, tmp_path):
    clean = lib["heat"].parent / "Clean"
    touch(clean / ".tinyclean-map.json", b"{}")
    touch(clean / "README-tinyclean.txt", b"notes")
    state = tmp_path / "state"
    p = plan(tc, lib, "restructure")
    assert [c["dir"] for c in p["clean_leftovers"]] == [str(clean)]
    tc.apply_plan(str(save(tc, state, p)), state, yes=True)
    assert not lib["heat"].parent.exists()


def test_a_clean_folder_holding_a_copy_keeps_the_old_folder_and_its_extras(tc, lib):
    touch(lib["heat"].parent / "poster.jpg", b"jpg")
    touch(lib["heat"].parent / "Clean" / "other-noidea.mkv", b"a copy of something else")
    p = plan(tc, lib, "restructure")
    assert not [x for x in p["extras"] if "Heat" in x["old"]] and not p["clean_leftovers"]


def test_subtitle_folders_follow_their_season(tc, lib):
    sub = touch(lib["shows"] / "breaking bad" / "S1" / "Subs" / "ep1" / "2_English.srt", b"srt")
    p = plan(tc, lib, "restructure")
    new = {x["old"]: x["new"] for x in p["extras"]}[str(sub)]
    assert new.endswith("Breaking Bad (2008) {tvdb-81189}/Season 01/Subs/ep1/2_English.srt")


def test_skipped_files_are_grouped_by_folder_and_reason(tc, lib):
    for n in range(3, 8):
        f = touch(lib["shows"] / "Odd Show" / f"odd {n}.mkv")
        lib["plex"].episodes.append(episode("99", 1, n, "x", str(f)))
    groups = tc.group_skipped(plan(tc, lib, "rename"))
    odd = [g for g in groups if g[0].endswith("Odd Show")]
    assert len(odd) == 1 and len(odd[0][2]) == 5 and odd[0][1] == "show not matched in Plex"


def test_ignored_and_unmounted_libraries_are_left_out(tc, lib, tmp_path):
    lib["plex"].sections_extra = [{"key": "3", "type": "show", "title": "Youtube Shows",
                                   "Location": [{"path": str(tmp_path / "not-here")}]}]
    base = tc.read_library(lib["plex"], lambda x: x)[0]
    shows_ignored = tc.read_library(lib["plex"], lambda x: x, ignore=["shows"])[0]
    assert {e["kind"] for e in shows_ignored} == {"movie"} and len(base) > len(shows_ignored)


def test_plex_rescan_is_asked_for_the_changed_folders(tc, lib, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: asked.append(req.full_url) or _Closable())
    settings = {"refresh": True, "url": "http://plex:32400", "token": "t", "pairs": [("/media/NAS", "/data")]}
    tc.plex_refresh(settings, [("2", "/data/Episodes/Show"), ("2", "/data/Episodes/Show"), (None, "/x")])
    assert asked == ["http://plex:32400/library/sections/2/refresh?path=%2Fmedia%2FNAS%2FEpisodes%2FShow"]
    asked.clear()
    tc.plex_refresh({**settings, "refresh": False}, [("2", "/data/Episodes/Show")])
    assert asked == []


class _Closable:
    def close(self):
        pass


def test_old_plans_are_trimmed(tc, tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    old, new = plans / "plan-old.json", plans / "plan-new.json"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    tc.trim_state(tmp_path)
    assert not old.exists() and new.exists()


# -------------------------------------------------------------- audit, delete --

def run_audit(tc, lib, tmp_path, monkeypatch, paths=()):
    import argparse
    env = tmp_path / "mediatool.env"
    env.write_text("PLEX_URL=http://plex:32400/\nPLEX_TOKEN=t\n")
    monkeypatch.setattr(tc, "Plex", lambda url, token: lib["plex"])
    state = tmp_path / "state"
    tc.audit(argparse.Namespace(env_file=str(env), plex_url=None, paths=list(paths)), state)
    return json.loads((state / "audit.json").read_text())


def test_audit_lists_duplicates_unmatched_and_waiting_copies(tc, lib, tmp_path, monkeypatch):
    second = touch(lib["movies"] / "Heat (1995) 720p" / "Heat.720p.mkv", b"smaller")
    lib["plex"].movies[1]["Media"].append({"videoResolution": "720", "Part": [{"file": str(second)}]})
    lib["plex"].movies[1]["ratingKey"] = "77"
    dup_ep = touch(lib["shows"] / "breaking bad" / "S1" / "bb.s01e01.again-noidea.mkv", b"same episode")
    lib["plex"].episodes.append(episode("10", 1, 1, "Pilot", str(dup_ep)))
    odd = touch(lib["movies"] / "Odd" / "odd.mkv")
    lib["plex"].movies.append(movie("Odd", 2000, str(odd), guids=()))
    touch(lib["ep2"].parent / "Clean" / "bb.s01e02-noidea.mkv", b"copy")
    a = run_audit(tc, lib, tmp_path, monkeypatch)
    dups = {(d["title"], d["episode"]): d for d in a["duplicates"]}
    heat = dups[("Heat", None)]
    assert heat["plex_key"] == "77" and {f["file"] for f in heat["files"]} == {str(lib["heat"]), str(second)}
    pilot = dups[("Breaking Bad", "S01E01")]
    assert sorted(f["converted"] for f in pilot["files"]) == [False, True]
    assert [u["folder"] for u in a["unmatched"]] == [str(lib["movies"] / "Odd")]
    assert a["waiting"] == [{"folder": str(lib["ep2"].parent), "files": 1}]
    assert a["plex"] == {"url": "http://plex:32400", "machine": "machine-1"}
    assert a["counts"]["duplicates"] == 2


def test_audit_reads_failures_from_the_ledger(tc, lib, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "ledger.csv").write_text(
        "time,file,result,notes\n"
        f"2026-11-01 10:00,{lib['heat']},failed,the original looks damaged or incomplete; re-download it\n"
        f"2026-11-01 10:00,{lib['matrix']},failed,ffmpeg error\n"
        f"2026-11-02 10:00,{lib['matrix']},done,\n")
    a = run_audit(tc, lib, tmp_path, monkeypatch)
    assert [(r["file"], r["redownload"]) for r in a["failed"]] == [(str(lib["heat"]), True)]


def test_delete_removes_the_picked_video_and_its_subtitles_only(tc, lib, tmp_path):
    sub = touch(lib["heat"].with_suffix(".en.srt"), b"srt")
    keep = touch(lib["heat"].parent / "poster.jpg", b"jpg")
    tc.delete_files([str(lib["heat"])], tmp_path / "state", yes=True)
    assert not lib["heat"].exists() and not sub.exists() and keep.exists()
    log = (tmp_path / "state" / "deleted.log").read_text()
    assert str(lib["heat"]) in log and str(sub) in log
    with pytest.raises(SystemExit, match="Not a video"):
        tc.delete_files([str(keep)], tmp_path / "state", yes=True)
