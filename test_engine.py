"""Unit tests for engine.py (image discovery, reading, drawing, output paths)."""
import cv2
import numpy as np

import engine


def make_image(path, w=64, h=48):
    """Write a random image; imencode + tofile works for non-ASCII paths too."""
    ok, buf = cv2.imencode(path.suffix, np.random.randint(0, 255, (h, w, 3), np.uint8))
    assert ok
    buf.tofile(str(path))


def test_natural_sort_and_filtering(tmp_path):
    for name in ["img10.jpg", "img2.jpg", "img1.png", "notes.txt", "._img3.jpg"]:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub.jpg").mkdir()
    assert [p.name for p in engine.find_images(tmp_path)] == ["img1.png", "img2.jpg", "img10.jpg"]


def test_recursive_find(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x2.jpg").write_bytes(b"x")
    (tmp_path / "a" / "x10.jpg").write_bytes(b"x")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "h.jpg").write_bytes(b"x")
    (tmp_path / "top.jpg").write_bytes(b"x")
    assert [p.name for p in engine.find_images(tmp_path)] == ["top.jpg"]
    assert [p.name for p in engine.find_images(tmp_path, recursive=True)] == ["x2.jpg", "x10.jpg", "top.jpg"]


def test_read_image_handles_non_ascii_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    good = tmp_path / "\u00f1and\u00fa.jpg"
    make_image(good)
    assert engine.read_image(good).shape == (48, 64, 3)
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"junk")
    assert engine.read_image(bad) is None
    assert engine.read_image(tmp_path / "missing.jpg") is None


def test_fit_to_canvas_letterboxes():
    frame = np.full((10, 40, 3), 255, np.uint8)
    out = engine.fit_to_canvas(frame, (40, 40))
    assert out.shape == (40, 40, 3)
    assert out[0].sum() == 0 and out[20].sum() > 0


def test_resolve_output_path(tmp_path):
    p = engine.resolve_output_path(tmp_path / "out", "a/b:c")
    assert p.parent == tmp_path / "out" and p.name == "b_c.mp4"
    p.write_bytes(b"x")
    p2 = engine.resolve_output_path(tmp_path / "out", "b_c")
    assert p2 != p and p.read_bytes() == b"x"


def test_prefetch_keeps_order_and_reuses_known(tmp_path):
    paths = []
    for i in range(12):
        p = tmp_path / f"f{i:02d}.png"
        make_image(p, 32 + i, 24)
        paths.append(p)
    first, known = engine._first_readable_frame(paths)
    assert first.shape[1] == 32 and list(known) == [0]
    got = list(engine._prefetch(paths, known))
    assert [p for p, _ in got] == paths
    assert [f.shape[1] for _, f in got] == [32 + i for i in range(12)]


def test_prefetch_early_exit_is_clean(tmp_path):
    paths = []
    for i in range(20):
        p = tmp_path / f"f{i:02d}.png"
        make_image(p)
        paths.append(p)
    gen = engine._prefetch(paths, {})
    next(gen)
    gen.close()  # must not hang or raise


def test_find_images_progress_recursive_and_hidden(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hid").mkdir()
    for rel in ("a.jpg", "sub/b.PNG", ".hid/c.jpg", "._d.jpg", "e.txt"):
        (tmp_path / rel).write_bytes(b"x")
    seen = []
    flat = engine.find_images(tmp_path, False, on_progress=seen.append)
    assert [p.name for p in flat] == ["a.jpg"] and seen[-1] == 1
    deep = engine.find_images(tmp_path, True)
    assert [p.name for p in deep] == ["a.jpg", "b.PNG"]


def test_move_with_progress_same_and_cross_volume(tmp_path, monkeypatch):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 1000)
    seen = []
    engine.move_with_progress(src, tmp_path / "fast.bin", seen.append)
    assert (tmp_path / "fast.bin").stat().st_size == 1000 and seen[-1] == 1000

    # Force the cross-volume path: rename fails, so the chunked copy is used.
    src2 = tmp_path / "b.bin"
    src2.write_bytes(b"y" * 1000)
    monkeypatch.setattr(engine.os, "replace", lambda a, b, real=engine.os.replace: (
        (_ for _ in ()).throw(OSError("cross-device")) if str(a) == str(src2) else real(a, b)))
    monkeypatch.setattr(engine, "COPY_CHUNK_BYTES", 300)
    seen.clear()
    engine.move_with_progress(src2, tmp_path / "slow.bin", seen.append)
    assert (tmp_path / "slow.bin").read_bytes() == b"y" * 1000
    assert seen == [300, 600, 900, 1000] and not (tmp_path / "slow.bin.part").exists()


def test_timestamp_overlay_changes_frame(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    frame = np.zeros((200, 300, 3), np.uint8)
    out = engine.draw_timestamp(frame, f)
    assert out.sum() > 0 and frame.sum() == 0
