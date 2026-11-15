"""File names, suffixes and small helpers. No ffmpeg needed."""


def test_done_suffix_recognises_current_and_old_suffixes(tc):
    assert tc.done_suffix("Movie (2001)-noidea") == "-noidea"
    assert tc.done_suffix("Movie (2001)-NOIDEA") == "-noidea"
    assert tc.done_suffix("Old Show S01E01-Clean") == "-Clean"
    assert tc.done_suffix("Movie (2001)") is None
    assert tc.done_suffix("noidea in the middle of a name") is None


def test_strip_suffix(tc):
    assert tc.strip_suffix("Movie (2001)-noidea.mkv") == "Movie (2001)"
    assert tc.strip_suffix("Show - S01E02-Clean.mkv") == "Show - S01E02"
    assert tc.strip_suffix("Plain.mkv") == "Plain"


def test_episode_key(tc):
    assert tc.episode_key("Show - S01E05 - Title.mkv") == "S01E05"
    assert tc.episode_key("Dexter's Lab - 1x07.avi") == "S01E07"
    assert tc.episode_key("show.s2.e10.mkv") == "S02E10"
    assert tc.episode_key("Movie (2001).mkv") is None


def test_mmss(tc):
    assert tc.mmss(65) == "1:05"
    assert tc.mmss(3725) == "1:02:05"


def test_stream_seconds_reads_duration_or_matroska_tag(tc):
    assert tc.stream_seconds({"duration": "12.5"}) == 12.5
    assert tc.stream_seconds({"tags": {"DURATION": "02:36:31.716000000"}}) == 9391.716
    assert tc.stream_seconds({"tags": {"DURATION-eng": "00:00:10.000000000"}}) == 10.0
    assert tc.stream_seconds({"tags": {"DURATION": "bad"}}) == 0.0
    assert tc.stream_seconds({}) == 0.0


def test_video_seconds_skips_cover_pictures(tc):
    info = {"streams": [{"codec_type": "video", "disposition": {"attached_pic": 1}, "duration": "1"},
                        {"codec_type": "video", "duration": "300"}]}
    assert tc.video_seconds(info) == 300.0


def test_duration_of_prefers_the_header(tc):
    assert tc.duration_of({"format": {"duration": "9654.0"}, "streams": []}) == 9654.0
    assert tc.duration_of({"format": {}, "streams": [{"duration": "5"}, {"duration": "7"}]}) == 7.0


def test_human(tc):
    assert tc.human(512) == "512 B"
    assert tc.human(1536) == "1.5 KB"
    assert tc.human(3 * 1024 ** 3) == "3.0 GB"
