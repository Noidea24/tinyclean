"""What tinyclean decides to do with a file, checked on small real videos."""
from conftest import make_video, needs_ffmpeg

pytestmark = needs_ffmpeg


def plan(tc, cfg, path):
    return tc.plan_file(path, tc.ffprobe(cfg["ffprobe"], path), cfg)


def test_h264_is_re_encoded(tc, cfg, tmp_path):
    p = plan(tc, cfg, make_video(tmp_path / "a.mkv"))
    assert p["video_mode"] == "encode" and not p["needs_scale"]


def test_already_efficient_hevc_is_kept(tc, cfg, tmp_path):
    # No audio: on a 4 s clip the audio bitrate would count towards the video estimate.
    src = make_video(tmp_path / "b.mkv", vcodec="libx265", audio=False, extra=["-b:v", "20k"])
    p = plan(tc, cfg, src)
    assert p["video_mode"] == "copy" and p["video_why"].startswith("already efficient")


def test_4k_is_scaled_to_1080p(tc, cfg, tmp_path):
    p = plan(tc, cfg, make_video(tmp_path / "c.mkv", size="3840x2160", seconds=1, audio=False))
    assert p["video_mode"] == "encode" and p["needs_scale"] and (p["out_w"], p["out_h"]) == (1920, 1080)


def test_padded_1080p_keeps_its_size(tc, cfg, tmp_path):
    """No Game No Life specials: 1920x1088 is 1080p padded to 16-pixel blocks; never squeeze it."""
    p = plan(tc, cfg, make_video(tmp_path / "pad.mkv", size="1920x1088", seconds=1, audio=False))
    assert not p["needs_scale"] and (p["out_w"], p["out_h"]) == (1920, 1088)


def test_surround_is_downmixed_to_stereo(tc, cfg, tmp_path):
    p = plan(tc, cfg, make_video(tmp_path / "d.mkv", channels=6))
    assert [a["mode"] for a in p["audio"]] == ["downmix"]


def test_stereo_aac_is_copied(tc, cfg, tmp_path):
    p = plan(tc, cfg, make_video(tmp_path / "e.mkv", channels=2))
    assert [a["mode"] for a in p["audio"]] == ["copy"]


def test_english_subtitles_kept_others_dropped(tc, cfg, tmp_path):
    src = make_video(tmp_path / "f.mkv", subs=[("eng", "English"), ("spa", "Spanish"), ("eng", "Forced")])
    p = plan(tc, cfg, src)
    assert sorted(s["tags"]["language"] for s in p["subs"]) == ["eng", "eng"]
    assert p["subs_dropped"] == 1


def test_font_attachments_are_kept(tc, cfg, tmp_path):
    p = plan(tc, cfg, make_video(tmp_path / "g.mkv", fonts=["font.ttf"]))
    assert p["attachments"] is True


def test_a_file_without_video_is_refused(tc, cfg, tmp_path):
    import subprocess
    src = tmp_path / "h.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(src)], check=True)
    try:
        plan(tc, cfg, src)
    except RuntimeError as exc:
        assert "no video" in str(exc)
    else:
        raise AssertionError("expected a refusal")
