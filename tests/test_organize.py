"""The organizer: names from Plex's matches, plans, applying, undoing. Plex is faked; no ffmpeg needed."""
import http.server
import json
import os
import threading
import time
from pathlib import Path

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
    assert tc.render(tc.TEMPLATES["movie_file"], {**v, "edition": "Unrated"}) == "The Matrix (1999) {edition-Unrated}"
    e = {"show": "Breaking Bad", "year": 2008, "tvdb": "81189", "season": 1, "episode": 2,
         "episode_title": "Cat's in the Bag..."}
    assert tc.render(tc.TEMPLATES["show_folder"], e) == "Breaking Bad (2008) {tvdb-81189}"
    assert tc.render(tc.TEMPLATES["season_folder"], e) == "Season 01"
    assert tc.render(tc.TEMPLATES["episode_file"], e) == "Breaking Bad (2008) - S01E02 - Cat's in the Bag"


def test_a_year_already_in_the_title_is_not_added_twice(tc):
    e = {"show": "Archer (2009)", "year": 2009, "tvdb": "110381", "season": 1, "episode": 1,
         "episode_title": "Mole Hunt"}
    assert tc.render(tc.TEMPLATES["show_folder"], e) == "Archer (2009) {tvdb-110381}"
    assert tc.render(tc.TEMPLATES["episode_file"], e) == "Archer (2009) - S01E01 - Mole Hunt"
    v = {"title": "Dune (2021)", "year": 2021, "tmdb": "438631"}
    assert tc.render(tc.TEMPLATES["movie_folder"], v) == "Dune (2021) {tmdb-438631}"
    # a different year in the title (Plex's own name) wins: never two years
    b = {"show": "Battlestar Galactica (2003)", "year": 2004, "tvdb": "73545", "season": 1, "episode": 1,
         "episode_title": "33"}
    assert tc.render(tc.TEMPLATES["show_folder"], b) == "Battlestar Galactica (2003) {tvdb-73545}"
    assert tc.render(tc.TEMPLATES["episode_file"], b) == "Battlestar Galactica (2003) - S01E01 - 33"
    assert tc.render(tc.TEMPLATES["movie_file"], {"title": "Blade Runner 2049", "year": 2017}) \
        == "Blade Runner 2049 (2017)"
    assert tc.render("{show}", e) == "Archer (2009)"


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


def test_a_file_downloaded_again_is_no_longer_listed_as_failed(tc, lib, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    size = lib["heat"].stat().st_size
    (state / "ledger.csv").write_text(
        "time,file,result,orig_bytes,new_bytes,seconds,notes\n"
        f"2026-11-01 10:00,{lib['heat']},failed,{size},,,duration mismatch\n"
        f"2026-11-01 10:00,{lib['matrix']},failed,{size + 999},,,duration mismatch\n")
    a = run_audit(tc, lib, tmp_path, monkeypatch)
    assert [r["file"] for r in a["failed"]] == [str(lib["heat"])]


def test_delete_removes_the_picked_video_and_its_subtitles_only(tc, lib, tmp_path):
    sub = touch(lib["heat"].with_suffix(".en.srt"), b"srt")
    keep = touch(lib["heat"].parent / "poster.jpg", b"jpg")
    tc.delete_files([str(lib["heat"])], tmp_path / "state", yes=True)
    assert not lib["heat"].exists() and not sub.exists() and keep.exists()
    log = (tmp_path / "state" / "deleted.log").read_text()
    assert str(lib["heat"]) in log and str(sub) in log
    with pytest.raises(SystemExit, match="Not a video"):
        tc.delete_files([str(keep)], tmp_path / "state", yes=True)


def test_placeholder_titles_from_plex_keep_the_file_names_title(tc, lib):
    f = touch(lib["shows"] / "breaking bad" / "S1" / "s2014e05 - Too Many Cooks-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 9, "Episode 9", str(f)))
    new = {it["old"]: it["new"] for it in plan(tc, lib, "rename")["items"]}[str(f)]
    assert new.endswith("Breaking Bad (2008) - S01E09 - Too Many Cooks-noidea.mkv")


def test_a_three_digit_number_plex_misread_is_skipped(tc, lib):
    """Avengers Assemble - 101 - ...: Plex took 101 as episode 101 (no such episode), not S01E01."""
    f = touch(lib["shows"] / "breaking bad" / "S1" / "Breaking Bad - 101 - Pilot {C_P}-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 101, "Episode 101", str(f)))
    p = plan(tc, lib, "rename")
    assert str(f) not in {it["old"] for it in p["items"]}
    assert "probably S01E01" in dict(p["skipped"])[str(f)]
    assert not tc.three_digit_code("Show - S01E101 - x.mkv", {"season": 1, "episode": 101})
    assert not tc.three_digit_code("Show - 101 - x.mkv", {"season": 1, "episode": 9})


def test_a_very_long_episode_title_is_shortened_to_fit(tc, lib):
    """Interspecies Reviewers S01E10: Plex's title alone is over 255 bytes."""
    f = touch(lib["shows"] / "breaking bad" / "S1" / "Breaking Bad - S01E09 - Let Your Eyes Behold the Glory-noidea.mkv")
    long = "Let Your Eyes Behold the Glory " * 12
    lib["plex"].episodes.append(episode("10", 1, 9, long.strip() + "!!!", str(f)))
    new = {it["old"]: it["new"] for it in plan(tc, lib, "rename")["items"]}[str(f)]
    name = new.rsplit("/", 1)[1]
    assert len(name.encode()) <= 240 and name.endswith("-noidea.mkv")
    assert name.startswith("Breaking Bad (2008) - S01E09 - Let Your Eyes Behold the Glory")


def test_a_number_plex_guessed_from_the_name_is_skipped(tc, lib):
    """Captain Scarlet 13 Point 783: Plex has no episode 783, it read the number from the title."""
    f = touch(lib["shows"] / "breaking bad" / "S1" / "Captain Scarlet 13 Point 783-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 783, "Episode 783", str(f)))
    p = plan(tc, lib, "rename")
    assert str(f) not in {it["old"] for it in p["items"]}
    assert "no episode 783" in dict(p["skipped"])[str(f)]


def test_a_show_without_a_tvdb_id_keeps_another_id_in_its_folder(tc):
    v = {"show": "The New Scooby-Doo Mysteries", "year": 1984, "imdb": "tt0086767"}
    assert tc.id_fallback(tc.render(tc.TEMPLATES["show_folder"], v), v, "tvdb") \
        == "The New Scooby-Doo Mysteries (1984) {imdb-tt0086767}"
    v = {"show": "Unsolved Mysteries", "year": 2020, "tmdb": "100698", "imdb": "tt12588448"}
    assert tc.id_fallback(tc.render(tc.TEMPLATES["show_folder"], v), v, "tvdb") \
        == "Unsolved Mysteries (2020) {tmdb-100698}"
    v = {"show": "Archer", "year": 2009, "tvdb": "110381", "imdb": "tt1486217"}
    assert tc.id_fallback(tc.render(tc.TEMPLATES["show_folder"], v), v, "tvdb") == "Archer (2009) {tvdb-110381}"
    assert tc.id_fallback("Nothing (2001)", {"show": "Nothing"}, "tvdb") == "Nothing (2001)"


def test_title_from_name(tc):
    assert tc.title_from_name("s2014e05 - Too Many Cooks-noidea.mkv") == "Too Many Cooks"
    assert tc.title_from_name("Show - S01E02 - Pilot [WEBDL-1080p][x265]-noidea.mkv") == "Pilot"
    assert tc.title_from_name("60 Days In - S01e05 -hevc-noidea.mkv") is None
    assert tc.title_from_name("No code here.mkv") is None
    assert tc.title_from_name("COW and CHICKEN - S03 E14 - P.E. (480p - DVDRip).mp4") == "P.E"
    assert tc.title_from_name("BSG (2003) - S01E12 - Kobol's Last Gleaming (1) Bluray-1080p[x265]-noidea.mkv") \
        == "Kobol's Last Gleaming (1)"
    assert tc.is_placeholder_title("Episode 12") and not tc.is_placeholder_title("Episode IV: A New Hope")


def test_episodes_numbered_differently_from_plex_are_renumbered_by_title(tc, lib):
    """All That: the files' order differs from Plex's, so Plex's title for each number is another file's."""
    names = {16: "Silk", 17: "Terry Ellis", 18: "Faith Evans"}        # what the files say
    plex = {16: "Terry Ellis", 17: "Faith Evans", 18: "Silk"}         # what Plex has at each number
    files = {}
    for n, t in names.items():
        files[n] = touch(lib["shows"] / "breaking bad" / "S2" / f"s02e{n} - {t}-noidea.mkv")
        lib["plex"].episodes.append(episode("10", 2, n, plex[n], str(files[n])))
    special = touch(lib["shows"] / "breaking bad" / "s00e01 - Good Burger Special-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 0, 1, "10th Anniversary Reunion Special Part 1", str(special)))
    p = plan(tc, lib, "rename")
    new = {it["old"]: Path(it["new"]).name for it in p["items"]}
    assert new[str(files[16])] == "Breaking Bad (2008) - S02E18 - Silk-noidea.mkv"
    assert new[str(files[17])] == "Breaking Bad (2008) - S02E16 - Terry Ellis-noidea.mkv"
    assert new[str(files[18])] == "Breaking Bad (2008) - S02E17 - Faith Evans-noidea.mkv"
    assert all("renumbered" in it["note"] for it in p["items"] if it["old"] in map(str, files.values()))
    assert "don't match Plex's titles" in dict(p["skipped"])[str(special)]


def test_titles_found_in_another_season_are_renumbered_there(tc, lib):
    """Darkwing Duck: the files follow another order, so a title can be in another season in Plex."""
    f1 = touch(lib["shows"] / "breaking bad" / "S1" / "s01e07 - That Sinking Feeling-noidea.mkv")
    f2 = touch(lib["shows"] / "breaking bad" / "S2" / "s02e03 - Dirty Money-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 7, "Dirty Money", str(f1)))
    lib["plex"].episodes.append(episode("10", 2, 3, "That Sinking Feeling", str(f2)))
    new = {it["old"]: Path(it["new"]).name for it in plan(tc, lib, "rename")["items"]}
    assert new[str(f1)] == "Breaking Bad (2008) - S02E03 - That Sinking Feeling-noidea.mkv"
    assert new[str(f2)] == "Breaking Bad (2008) - S01E07 - Dirty Money-noidea.mkv"


def test_a_file_whose_inside_title_disagrees_is_not_renumbered(tc, lib, monkeypatch):
    """Recess: the name said Dodgeball City, the title inside the file said The Shiner / Stand Up Randall."""
    f1 = touch(lib["shows"] / "breaking bad" / "S1" / "s01e07 - That Sinking Feeling-noidea.mkv")
    f2 = touch(lib["shows"] / "breaking bad" / "S2" / "s02e03 - Dirty Money-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 7, "Dirty Money", str(f1)))
    lib["plex"].episodes.append(episode("10", 2, 3, "That Sinking Feeling", str(f2)))
    inside = {str(f1): "Show S04E01 - The Shiner / Stand Up Randall", str(f2): "Show.S01E07.Dirty.Money.720p-GRP"}
    monkeypatch.setattr(tc, "builtin_title", lambda b, p: inside.get(str(p)))
    entries, skipped = tc.read_library(lib["plex"], lambda p: p)
    p = tc.make_plan(entries, skipped, "rename", dict(tc.TEMPLATES), (), ffprobe_bin="ffprobe")
    assert "labels disagree" in dict(p["skipped"])[str(f1)]
    # f2's inside title agrees with its name, but its target S01E07 stays held by f1, so no new duplicate
    assert "would be a new duplicate" in dict(p["skipped"])[str(f2)]
    assert not [it for it in p["items"] if it["old"] in (str(f1), str(f2))]


def test_built_in_titles_that_agree_let_the_renumbering_through(tc):
    assert tc.labels_agree("Downtown Dashin'", "Real Stories of The Highway Patrol--Season 1, Episode 6: Downtown Dashin'")
    assert tc.labels_agree("Stand Up Randall", "Recess S03E03E04 - The Shiner / Stand Up Randall")
    assert tc.labels_agree("That Sinking Feeling", "Show.S01E07.That.Sinking.Feeling.720p.WEB-DL")
    assert not tc.labels_agree("Dodgeball City", "Recess S03E03E04 - The Shiner / Stand Up Randall")


def test_synology_eadir_folders_are_removed_not_moved(tc, lib, tmp_path):
    ea = lib["heat"].parent / "@eaDir" / "heat.mkv@SynoEAStream"
    touch(ea, b"syno")
    p = plan(tc, lib, "restructure")
    assert not [x for x in p["extras"] if "@eaDir" in x["old"]]
    assert [c["kind"] for c in p["clean_leftovers"]] == ["synology"]
    state = tmp_path / "state"
    tc.apply_plan(str(save(tc, state, p)), state, yes=True)
    assert not lib["heat"].parent.exists()


def test_an_eadir_with_someone_elses_file_is_kept(tc, lib):
    touch(lib["heat"].parent / "@eaDir" / "notes.txt", b"mine")
    assert not plan(tc, lib, "restructure")["clean_leftovers"]


def test_artwork_named_after_the_original_download_is_renamed_with_its_video(tc, lib):
    thumb = touch(lib["matrix"].with_name("The.Matrix.1999.1080p.BluRay-thumb.jpg"), b"jpg")
    other = touch(lib["matrix"].with_name("folder.jpg"), b"jpg")
    it = {i["old"]: i for i in plan(tc, lib, "rename")["items"]}[str(lib["matrix"])]
    names = sorted(Path(n).name for _, n in it["sidecars"])
    assert names == ["The Matrix (1999)-noidea.en.srt", "The Matrix (1999)-thumb.jpg"]
    assert str(other) not in [o for o, _ in it["sidecars"]]


def test_titles_from_real_file_names(tc):
    assert tc.title_from_name("Atlanta (2016) - S01E01 (1080p AMZN WEB-DL x265 HEVC 10bit)-noidea.mkv") is None
    assert tc.title_from_name("Sabrina - S01E08 - Steves Bald    -playWEB-noidea.mkv") == "Steves Bald"
    assert tc.title_from_name("Show.S01E02.The.Big.Bang.1080p.WEB-DL-noidea.mkv") == "The Big Bang"
    assert tc.titles_agree("Steves Bald", "Steve's Bald")
    assert tc.titles_agree("Tony! Toni! Ton\u00e9! feat. DJ Quik", "Tony Toni Tone")
    assert not tc.titles_agree("Chicken's First Kiss", "School Bully")
    assert tc.multi_episode_name("Show - S08E06 & E07 The Bleakening Part One and Two") == "S08E06-E07"


def test_more_release_junk_is_not_a_title(tc):
    assert tc.title_from_name("CSI - S01E02 - NORDiC ENG-noidea.mkv") is None
    assert tc.title_from_name("Family Guy S01E03 XviD-AFG-noidea.mkv") is None
    assert tc.title_from_name("SpongeBob - S01E05 - Gary Takes a Bath - SDTV-noidea.mkv") == "Gary Takes a Bath"
    assert tc.title_from_name("Superjail - S01E02 - Negaton (1280x720 x264)-noidea.mkv") == "Negaton"
    assert tc.titles_agree("Redemption Part I", "Redemption (1)")
    assert tc.titles_agree("The Origin of Donnie, Part Three", "The Origin of Donnie (3)")
    assert tc.titles_agree("Dirty Pranking Number 2", "Dirty Pranking No. 2")


def test_audit_lists_shows_whose_episode_order_differs(tc, lib, tmp_path, monkeypatch):
    for n, (mine, theirs) in enumerate([("Alpha", "Gamma"), ("Beta", "Delta"), ("Gamma", "Epsilon")], start=5):
        f = touch(lib["shows"] / "breaking bad" / "S3" / f"bb s03e{n:02d} - {mine}-noidea.mkv")
        lib["plex"].episodes.append(episode("10", 3, n, theirs, str(f)))
    a = run_audit(tc, lib, tmp_path, monkeypatch)
    assert [(o["folder"].split("/")[-1], o["files"]) for o in a["order"]] == [("breaking bad", 2)]
    assert sorted(x["plex"] for x in a["order"][0]["examples"]) == ["Delta", "Gamma"]


def test_two_editions_of_one_movie_get_their_own_names(tc, lib):
    cut = touch(lib["movies"] / "the.matrix.1999.1080p" / "The.Matrix.1999.UNRATED-noidea.mkv")
    m = movie("The Matrix", 1999, str(cut))
    m["editionTitle"] = "Unrated"
    lib["plex"].movies.append(m)
    p = plan(tc, lib, "rename")
    names = sorted(Path(i["new"]).name for i in p["items"] if "Matrix" in i["new"])
    assert names == ["The Matrix (1999) {edition-Unrated}-noidea.mkv", "The Matrix (1999)-noidea.mkv"]


# ----------------------------------------------------------------- cleanup --

def test_subtitle_languages_from_real_download_names(tc):
    lang = tc.sub_language
    for label in ["French.fre", "Spanish (Latin America).spa", "Portuguese (Brazil).por", "Chinese (Traditional).chi",
                  "Norwegian.nob", "Filipino.fil", "Malay.may", "3_French", ".fr", ".de.forced"]:
        assert lang(label) not in (None, "en"), label
    for label in ["English", "English (SDH).eng", "SDH.eng.HI", "2_English", ".en", ".en.forced", "eng"]:
        assert lang(label) == "en", label
    # Not purely a language: kept.
    for label in ["", ".", "What Is It", "Commentary", "Bad Taste 1987"]:
        assert lang(label) is None, label
    assert tc.sub_kind("English (SDH).eng") == "sdh" and tc.sub_kind("SDH.eng.HI") == "sdh"
    assert tc.sub_kind(".en.forced") == "forced" and tc.sub_kind("English") == ""


def test_junk_names(tc):
    for n in ["www.YTS.MX.jpg", "WWW.YTS.AG.jpg", "YTSProxies.com.txt", "YIFYStatus.com.txt",
              "YTSYifyUP... (TOR).txt", "RARBG.txt", "RARBG_DO_NOT_MIRROR.exe", "Visit us.url"]:
        assert tc.JUNK_NAME.search(n), n
    for n in ["poster.jpg", "fanart.jpg", "clearlogo.png", "movieset-poster.jpg", "notes.txt", "Movie.nfo"]:
        assert not tc.JUNK_NAME.search(n), n


def yts_movie(lib):
    d = lib["movies"] / "Andrew Schulz Life (2025)"
    v = touch(d / "Andrew.Schulz.Life.2025.1080p.WEBRip.x264.AAC5.1-[YTS.MX]-noidea.mkv")
    for n in ["www.YTS.MX.jpg", "YTSProxies.com.txt", "poster.jpg"]:
        touch(d / n, b"x")
    touch(d / "Subs" / "English (SDH).eng.srt", b"sdh subs")
    touch(d / "Subs" / "English.srt", b"plain subs, longer")
    touch(d / "Subs" / "French.fre.srt", b"fr")
    touch(d / "Subs" / "Arabic.ara.srt", b"ar")
    touch(d / "Subs" / "Commentary.srt", b"c")
    touch(v.with_name(v.stem + ".fr.srt"), b"fr")
    lib["plex"].movies.append(movie("Andrew Schulz - Life", 2025, str(v), guids=("tmdb://1435760",)))
    return d, v


def test_restructure_deletes_download_leftovers_and_names_english_subs(tc, lib):
    d, v = yts_movie(lib)
    p = plan(tc, lib, "restructure", scope=[d])
    assert sorted(Path(x["path"]).name for x in p["deletes"]) == sorted(
        ["www.YTS.MX.jpg", "YTSProxies.com.txt", "French.fre.srt", "Arabic.ara.srt", v.stem + ".fr.srt"])
    assert p["items"][0]["sidecars"] == []
    moves = {Path(x["old"]).name: Path(x["new"]) for x in p["extras"]}
    base = "Andrew Schulz - Life (2025)-noidea"
    assert moves["English.srt"].name == base + ".en.srt"
    assert moves["English (SDH).eng.srt"].name == base + ".en.sdh.srt"
    assert moves["English.srt"].parent == Path(p["items"][0]["new"]).parent
    assert moves["Commentary.srt"].parts[-2] == "Subs"          # not a language: moved along as it is
    assert moves["poster.jpg"].name == "poster.jpg"


def test_apply_deletes_only_the_listed_leftovers(tc, lib, tmp_path):
    d, v = yts_movie(lib)
    state = tmp_path / "state"
    p = plan(tc, lib, "restructure", scope=[d])
    tc.apply_plan(str(save(tc, state, p)), state, yes=True, env_file=str(tmp_path / "none.env"))
    new = lib["movies"] / "Andrew Schulz - Life (2025) {tmdb-1435760}"
    assert sorted(tree(new)) == sorted([
        "Andrew Schulz - Life (2025)-noidea.mkv", "Andrew Schulz - Life (2025)-noidea.en.srt",
        "Andrew Schulz - Life (2025)-noidea.en.sdh.srt", "poster.jpg", "Subs/Commentary.srt"])
    assert not d.exists()
    logged = [json.loads(l)["file"] for l in (state / "deleted.log").read_text().splitlines()]
    assert len(logged) == 5


def test_keep_leftovers_and_newtree_delete_nothing(tc, lib):
    d, v = yts_movie(lib)
    entries, skipped = tc.read_library(lib["plex"], lambda p: p)
    off = tc.make_plan(entries, skipped, "restructure", dict(tc.TEMPLATES), [d], cleanup=False)
    assert off["deletes"] == [] and len(off["items"][0]["sidecars"]) == 1
    nt = tc.make_plan(entries, skipped, "newtree", dict(tc.TEMPLATES), [d])
    assert nt["deletes"] == []
    assert not any("YTS" in x["old"] or "French" in x["old"] for x in nt["extras"])


def test_rename_mode_cleans_a_folder_that_keeps_its_name(tc, lib):
    d, v = yts_movie(lib)
    p = plan(tc, lib, "rename", scope=[d])
    assert len(p["deletes"]) == 5
    moves = {Path(x["old"]).name: Path(x["new"]).name for x in p["extras"]}
    assert moves == {"English.srt": "Andrew Schulz - Life (2025)-noidea.en.srt",
                     "English (SDH).eng.srt": "Andrew Schulz - Life (2025)-noidea.en.sdh.srt"}


def test_episode_subs_folders_follow_their_episode(tc, lib):
    ep = lib["ep1"]
    sub = touch(ep.parent / "Subs" / ep.stem / "2_English.srt", b"en")
    touch(ep.parent / "Subs" / ep.stem / "3_Spanish.srt", b"es")
    p = plan(tc, lib, "restructure", scope=[lib["shows"]])
    assert [Path(x["path"]).name for x in p["deletes"]] == ["3_Spanish.srt"]
    x = next(x for x in p["extras"] if x["old"] == str(sub))
    assert Path(x["new"]).name == "Breaking Bad (2008) - S01E01 - Pilot.en.srt"


def test_duplicates_show_what_tells_the_copies_apart(tc, lib, tmp_path, monkeypatch):
    other = touch(lib["movies"] / "Heat (1995)" / "Heat.1995.720p.mkv")
    touch(other.with_name("Heat.1995.720p.en.srt"), b"1\n00:00:01,000 --> 00:00:02,000\nhi\n\n2\n00:00:03,000 --> 00:00:04,000\nyo\n")
    lib["plex"].movies[1]["Media"].append({"videoResolution": "720", "Part": [{"file": str(other)}]})
    fake = {"format": {"duration": "6600"}, "streams": [
        {"codec_type": "video", "width": 1280, "height": 720},
        {"codec_type": "audio", "channels": 2, "tags": {"language": "eng"}},
        {"codec_type": "subtitle"}]}
    monkeypatch.setattr(tc, "ffprobe", lambda b, p: fake)
    import argparse
    env = tmp_path / "mediatool.env"
    env.write_text("PLEX_URL=http://plex:32400/\nPLEX_TOKEN=t\n")
    monkeypatch.setattr(tc, "Plex", lambda url, token: lib["plex"])
    tc.audit(argparse.Namespace(env_file=str(env), plex_url=None, paths=[]), tmp_path / "st", "ffprobe")
    a = json.loads((tmp_path / "st" / "audit.json").read_text())
    d = next(d for d in a["duplicates"] if d["title"] == "Heat")
    f = next(f for f in d["files"] if f["file"] == str(other))
    assert (f["minutes"], f["picture"], f["audio"], f["sub_tracks"], f["srt_lines"]) == (110.0, "1280x720", ["eng 2ch"], 1, 2)


def test_a_year_before_the_title_is_not_part_of_it(tc):
    assert tc.title_from_name("Scooby Doo And Scrappy Doo - S02E06 - (1979) Stow-Aways-noidea.mkv") == "Stow-Aways"
    assert tc.JUNK_NAME.search("Thumbs.db") and tc.JUNK_NAME.search("desktop.ini")


def test_a_file_named_for_two_segments_gets_both_numbers(tc, lib):
    """Rocket Power: 'Secret Spot - Ice Queens' sits at S01E02, but Plex has those as S01E03 and S01E04."""
    d = lib["shows"] / "breaking bad" / "S1"
    pair = touch(d / "s01e02 - Secret Spot - Ice Queens-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 2, "Down the Drain", str(pair)))
    for n, t in ((3, "Secret Spot"), (4, "Ice Queens")):
        other = touch(d / f"s01e0{n} - somewhere else {n}-noidea.mkv")
        lib["plex"].episodes.append(episode("10", 1, n, t, str(other)))
    p = plan(tc, lib, "rename")
    new = {it["old"]: it for it in p["items"]}
    # the files now at E03/E04 stay there, so moving the pair onto them would stack two files: refused
    assert "new duplicate" in dict(p["skipped"])[str(pair)]
    for n in (3, 4):
        (d / f"s01e0{n} - somewhere else {n}-noidea.mkv").unlink()
    lib["plex"].episodes = [e for e in lib["plex"].episodes if "somewhere" not in e["Media"][0]["Part"][0]["file"]]
    # with Plex's titles known from elsewhere in the show, the pair is renumbered as a range
    lib["plex"].episodes.append(episode("10", 1, 3, "Secret Spot", str(touch(d / "x" / "keep3.mkv"))))
    lib["plex"].episodes.append(episode("10", 1, 4, "Ice Queens", str(touch(d / "x" / "keep4.mkv"))))
    entries, skipped = tc.read_library(lib["plex"], lambda p: p)
    hit = tc.two_part_hit(tc.episode_index(entries), next(e for e in entries if e["file"] == pair), "Secret Spot - Ice Queens")
    assert hit == (1, 3, 4, "Secret Spot & Ice Queens")


def test_left_alone_folders_are_never_touched(tc, lib, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "left-alone.json").write_text(json.dumps({"order": [str(lib["shows"] / "breaking bad")]}))
    entries, skipped = tc.read_library(lib["plex"], lambda p: p)
    p = tc.make_plan(entries, skipped, "rename", dict(tc.TEMPLATES), (), left_alone=tc.left_alone_folders(state))
    assert not [it for it in p["items"] if "breaking bad" in it["old"]]
    assert "left as is" in dict(p["skipped"])[str(lib["ep1"])]
    assert [it for it in p["items"] if it["old"] == str(lib["matrix"])]


def test_a_two_segment_name_is_never_cut_down_to_one_title(tc, lib):
    """Rocket Power: 'New Squid On The Block - Down The Drain' at S01E01 holds Plex's E01 and E02."""
    d = lib["shows"] / "breaking bad" / "S1"
    f = touch(d / "s01e01 - New Squid On The Block - Down The Drain-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 1, "New Squid on the Block", str(f)))
    p = plan(tc, lib, "rename")
    # Plex's E02 title is unknown here: naming it after E01 alone would hide Down the Drain
    assert "holds two titles" in dict(p["skipped"])[str(f)]
    g = touch(d / "s01e02 - Something Else Entirely-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 2, "Down the Drain", str(g)))
    p = plan(tc, lib, "rename")
    # now E02 is known, but another file keeps it: no stacking, both stay
    assert "new duplicate" in dict(p["skipped"])[str(f)]


def test_one_title_with_a_dash_that_is_plexs_title_is_still_renamed(tc, lib):
    f = touch(lib["shows"] / "breaking bad" / "S1" / "s01e09 - Dial M for Monkey - Barbequor-noidea.mkv")
    lib["plex"].episodes.append(episode("10", 1, 9, "Dial M for Monkey: Barbequor", str(f)))
    assert [it for it in plan(tc, lib, "rename")["items"] if it["old"] == str(f)]


def test_a_cancelled_move_still_blocks_its_old_number(tc, lib):
    """A wants E03 (held by B); B wants E05, held by C who stays. B is cancelled and keeps E03,
    so A must be cancelled too, or A and B would share E03."""
    d = lib["shows"] / "breaking bad" / "S1"
    a = touch(d / "s01e02 - Bravo Title-noidea.mkv")
    b = touch(d / "s01e03 - Charlie Title-noidea.mkv")
    c = touch(d / "s01e05 - Zulu Nothing-noidea.mkv")
    lib["plex"].episodes += [episode("10", 1, 2, "Alpha Title", str(a)), episode("10", 1, 3, "Bravo Title", str(b)),
                             episode("10", 1, 5, "Charlie Title", str(c))]
    p = plan(tc, lib, "rename")
    moved = {it["old"] for it in p["items"]}
    assert str(a) not in moved and str(b) not in moved
    assert "new duplicate" in dict(p["skipped"])[str(a)]


def test_files_a_run_could_not_open_show_in_needs_you(tc, lib, tmp_path, monkeypatch):
    import os
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    bad = lib["heat"]
    (state / "state.json").write_text(json.dumps({
        str(bad): {"status": "unreadable", "mtime": int(bad.stat().st_mtime), "reason": "moov atom not found"},
        str(lib["matrix"]): {"status": "done", "mtime": 1}}))
    rows = tc.unreadable_files(state)
    assert [r["file"] for r in rows] == [str(bad)] and rows[0]["redownload"] and "moov atom" in rows[0]["notes"]
    os.utime(bad, (1, 1))                       # replaced since: no longer listed
    assert tc.unreadable_files(state) == []
