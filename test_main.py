"""End-to-end tests of the command-line interface (main.py driving engine.py)."""
from pathlib import Path

import cv2
import pytest

import engine
import main
from test_engine import make_image


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Skip the retry back-off so corrupt-file tests run instantly."""
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)


def run_cli(tmp_path, *extra, name="t"):
    """Run main() on tmp_path/src, writing to tmp_path/o."""
    return main.main([str(tmp_path / "src"), "--fps", "5", *extra,
                      "--output_dir", str(tmp_path / "o"), "--output_name", name])


def make_src(tmp_path):
    (tmp_path / "src").mkdir()
    make_image(tmp_path / "src" / "a.jpg")


def test_end_to_end_skips_bad_image(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    for i, size in enumerate([(64, 48), (80, 60), (64, 48)]):
        make_image(src / f"f{i}.jpg", *size)
    (src / "f9_bad.jpg").write_bytes(b"junk")
    code = main.main([str(src), "--fps", "5", "--output_dir", str(out), "--output_name", "t"])
    assert code == 0
    cap = cv2.VideoCapture(str(out / "t.mp4"))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 3
    cap.release()


def test_all_unreadable_fails_and_leaves_no_output(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"junk")
    assert main.main([str(src), "--fps", "5", "--output_dir", str(out)]) == 1
    assert not list(out.glob("*.mp4"))


@pytest.mark.parametrize("args,code", [
    (["nonexistent_dir_xyz"], 1),
    ([], 2),
])
def test_bad_arguments(args, code, monkeypatch):
    monkeypatch.setattr(main.sys, "stdin", None)
    assert main.main(args) == code


def test_invalid_fps(tmp_path):
    make_image(tmp_path / "a.jpg")
    assert main.main([str(tmp_path), "--fps", "0", "--output_dir", str(tmp_path / "o")]) == 2


def test_max_width_shrinks_video_and_never_enlarges(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_image(src / "a.jpg", 200, 100)
    make_image(src / "b.jpg", 200, 100)

    def size_of(name):
        cap = cv2.VideoCapture(str(tmp_path / "o" / f"{name}.mp4"))
        dims = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return dims

    assert run_cli(tmp_path, "--max-width", "100", name="small") == 0
    assert size_of("small") == (100, 50)
    assert run_cli(tmp_path, "--max-width", "1000", name="big") == 0
    assert size_of("big") == (200, 100)


def test_invalid_max_width(tmp_path):
    make_image(tmp_path / "a.jpg")
    assert main.main([str(tmp_path), "--fps", "5", "--max-width", "4", "--output_dir", str(tmp_path / "o")]) == 2


def test_invalid_crf(tmp_path):
    make_image(tmp_path / "a.jpg")
    assert main.main([str(tmp_path), "--fps", "5", "--crf", "99", "--output_dir", str(tmp_path / "o")]) == 2


def test_timestamp_run(tmp_path):
    make_src(tmp_path)
    assert run_cli(tmp_path, "--timestamp") == 0
    assert (tmp_path / "o" / "t.mp4").stat().st_size > 0


def test_crf_without_ffmpeg_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: None)
    make_src(tmp_path)
    assert run_cli(tmp_path, "--crf") == 0
    assert (tmp_path / "o" / "t.mp4").exists()


def test_crf_uses_ffmpeg_output(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: "ffmpeg")

    class FakeProc:
        """Stands in for subprocess.Popen: writes the output file, reports progress."""
        returncode = 0

        def __init__(self, cmd, **kw):
            Path(cmd[-1]).write_bytes(b"h264")
            self.stdout = iter(["frame=1\n", "progress=end\n"])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(engine.subprocess, "Popen", FakeProc)
    make_src(tmp_path)
    assert run_cli(tmp_path, "--crf", "28") == 0
    assert (tmp_path / "o" / "t.mp4").read_bytes() == b"h264"


def test_crf_ffmpeg_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: "ffmpeg")
    class FailingProc:
        returncode = 1

        def __init__(self, cmd, **kw):
            self.stdout = iter([])

        def wait(self, timeout=None):
            return 1

        def poll(self):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(engine.subprocess, "Popen", FailingProc)
    make_src(tmp_path)
    assert run_cli(tmp_path, "--crf") == 0
    assert (tmp_path / "o" / "t.mp4").read_bytes() != b"h264"


def test_check_reports(tmp_path, capsys):
    assert main.main(["--check", "--output_dir", str(tmp_path / "o")]) == 0
    assert "ffmpeg" in capsys.readouterr().out
