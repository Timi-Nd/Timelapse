import argparse
import io
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("rich")
from rich.console import Console

import engine
import main
import ui


@pytest.fixture
def term(monkeypatch):
    """A forced-terminal rich console that records its output."""
    c = Console(force_terminal=True, width=100, record=True, file=io.StringIO())
    monkeypatch.setattr(ui, "_console", c)
    return c


def feed(monkeypatch, *answers):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(answers) + "\n"))


def make_photos(folder, n=5):
    folder.mkdir(exist_ok=True)
    for i in range(n):
        ok, buf = cv2.imencode(".jpg", np.random.randint(0, 255, (48, 64, 3), np.uint8))
        buf.tofile(str(folder / f"p{i}.jpg"))


def test_clean_path_handles_drag_and_drop(tmp_path):
    assert ui.clean_path(f'"{tmp_path}"') == tmp_path
    assert ui.clean_path(f"& '{tmp_path}'") == tmp_path
    assert ui.clean_path(f"  {tmp_path}  ") == tmp_path


def test_formatters():
    assert ui.fmt_duration(2.5) == "2.5 s"
    assert ui.fmt_duration(125) == "2 min 05 s"
    assert ui.fmt_size(512) == "512 B"
    assert ui.fmt_size(3 * 1024 * 1024) == "3.0 MB"


def test_banner_and_check_render(term):
    ui.banner()
    ui.show_check([("OS", "x", True), ("ffmpeg", "missing", False)])
    text = term.export_text()
    assert "█" in text and "Diagnostics" in text and "missing" in text


def test_wizard_end_to_end(term, monkeypatch, tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    make_photos(src)
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: None)
    # bad path, real path, speed 2, no overlay, name, output dir, start, no open, no repeat
    feed(monkeypatch, str(tmp_path / "nope"), f'"{src}"', "2", "n", "demo", str(out), "y", "n", "n")
    args = argparse.Namespace(recursive=False, output_dir=None, timestamp=False, crf=None, output_name=None, max_width=None)
    assert main.run_wizard(args) == 0
    text = term.export_text()
    assert "That is not a folder" in text and "Found photos" in text and "Timelapse ready" in text
    assert (out / "demo.mp4").stat().st_size > 0


def test_wizard_quit_and_subfolder_hint(term, monkeypatch, tmp_path):
    make_photos(tmp_path / "deep")
    feed(monkeypatch, str(tmp_path), "n", "q")
    args = argparse.Namespace(recursive=False, output_dir=None, timestamp=False, crf=None, output_name=None, max_width=None)
    assert main.run_wizard(args) == 0
    assert "in subfolders" in term.export_text()


def test_wizard_decline_start_restarts(term, monkeypatch, tmp_path):
    make_photos(tmp_path / "src")
    feed(monkeypatch, str(tmp_path / "src"), "2", "n", "", str(tmp_path / "o"), "n", "q")
    args = argparse.Namespace(recursive=False, output_dir=None, timestamp=False, crf=None, output_name=None, max_width=None)
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: None)
    assert main.run_wizard(args) == 0
    assert "Starting over" in term.export_text()
    assert not (tmp_path / "o").exists()


def test_custom_length_speed(term, monkeypatch):
    feed(monkeypatch, "4", "0", "5")
    assert ui.ask_speed(100) == 20


def test_progress_and_failure_panel(term):
    with ui.RunProgress() as p:
        p("start", total=3)
        p("frame", done=3)
        p("stage", text="Saving")
    ui.show_result(False, Path("x.mp4"), {}, 24)
    assert "Failed" in term.export_text()
