"""Timelapse engine: everything that turns a folder of photos into a video.

This module has no user-interface code. It never prints; it reports through the
standard `logging` module (logger "timelapse") and through the optional
`on_event` / `stats` arguments of `generate_timelapse`, so it can be driven by the
console wizard, a plain command line, a test, or another program.

Layout of this file
    1. Constants
    2. Environment helpers      (default output folder, ffmpeg discovery)
    3. Photo discovery          (finding and ordering the source images)
    4. Image / frame handling   (reading, resizing, date overlay)
    5. Video output             (codec selection, H.264 re-encode, output naming)
    6. Orchestration            (generate_timelapse)

Safety guarantee: source photos are only ever read, never modified, moved or deleted.
"""
from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

import cv2
import numpy as np

log = logging.getLogger("timelapse")

# --------------------------------------------------------------------------- #
# 1. Constants
# --------------------------------------------------------------------------- #

#: File extensions treated as photos (compared in lower case).
VALID_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"})

#: Allowed frame rates (frames per second) and H.264 quality (CRF) values.
MAX_FPS = 240
MAX_CRF = 51
MIN_MAX_WIDTH = 16  # smallest accepted --max-width
DEFAULT_CRF = 23  # visually near-lossless for most photo sequences

#: Abort the run if more than this share of photos is unreadable (and at least
#: MIN_SKIPPED_TO_ABORT of them), because the result would be a misleading video.
MAX_SKIP_RATIO = 0.2
MIN_SKIPPED_TO_ABORT = 5

#: Cloud-synced and network files can fail transiently, so reads are retried.
READ_RETRIES = 3
RETRY_BACKOFF_S = 0.5  # attempt N waits N * this many seconds

#: Photos decoded in parallel ahead of the video writer (capped by CPU count).
READ_WORKERS = 4

#: OpenCV codecs tried in order. "mp4v" is built into every OpenCV wheel;
#: "avc1" (H.264) is a bonus where the platform provides it.
WRITER_CODECS = ("mp4v", "avc1")

#: Limit for the optional ffmpeg re-encode, in seconds.
FFMPEG_TIMEOUT_S = 3600

#: Chunk size when copying the finished video to another volume.
COPY_CHUNK_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 2. Environment helpers
# --------------------------------------------------------------------------- #

def default_output_dir() -> Path:
    """Return the default save folder: ``~/Videos/TIMELAPSE``.

    Falls back to ``~/TIMELAPSE`` on systems whose home folder has no ``Videos``
    directory, so nothing is created in an unexpected place.
    """
    home = Path.home()
    return (home / "Videos" if (home / "Videos").is_dir() else home) / "TIMELAPSE"


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg executable, or return None (result is cached).

    The binary bundled by the ``imageio-ffmpeg`` package is preferred so the
    program works on machines with nothing installed; a system ffmpeg on PATH is
    the fallback. The first lookup imports a sizeable package, so the UI calls
    ``warm_up_ffmpeg`` early to keep that cost out of the user's way.
    """
    try:
        import imageio_ffmpeg  # imported lazily: ffmpeg support is optional

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:  # missing package, unsupported platform, broken install...
        pass
    return shutil.which("ffmpeg")


def warm_up_ffmpeg() -> None:
    """Start looking for ffmpeg in a background thread so a later call is instant."""
    threading.Thread(target=find_ffmpeg, name="ffmpeg-warmup", daemon=True).start()

# --------------------------------------------------------------------------- #
# 3. Photo discovery
# --------------------------------------------------------------------------- #

def natural_key(path: Path) -> list:
    """Sort key giving "natural" order: ``img2`` before ``img10``.

    Each path component is split into text and number chunks; numbers compare as
    numbers and text compares case-insensitively.
    """
    return [
        [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", part)]
        for part in path.parts
    ]


def find_images(folder: Path, recursive: bool = False,
                on_progress: Optional[Callable[[int], None]] = None) -> list[Path]:
    """List supported photos in ``folder`` in natural order.

    Skips hidden folders, macOS resource files (``._*``), Office temp files
    (``~*``) and anything that is not a regular file. With ``recursive`` the
    subfolders are searched too and ordered by their relative path.

    Uses ``os.scandir`` so file types come from the directory listing itself
    (no extra round trip per file, which matters a lot on network drives).
    ``on_progress(count)`` is called periodically with the photos found so far.
    """
    images: list[Path] = []
    pending = [folder]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if recursive and not name.startswith("."):
                        pending.append(Path(entry.path))
                    continue
                if (
                    not name.startswith(("._", "~"))
                    and os.path.splitext(name)[1].lower() in VALID_EXTENSIONS
                    and entry.is_file()
                ):
                    images.append(Path(entry.path))
                    if on_progress and len(images) % 200 == 0:
                        on_progress(len(images))
    if on_progress:
        on_progress(len(images))
    return sorted(images, key=lambda p: natural_key(p.relative_to(folder)))


# --------------------------------------------------------------------------- #
# 4. Image / frame handling
# --------------------------------------------------------------------------- #

def read_image(path: Path) -> Optional[np.ndarray]:
    """Read a photo as a BGR array, or return None if it cannot be decoded.

    Reads the bytes ourselves and decodes in memory, because ``cv2.imread``
    fails on non-ASCII paths on Windows. I/O errors and empty reads are retried
    (cloud-synced files may not be fully available yet); data that is present but
    undecodable is not retried because retrying cannot fix it.
    """
    for attempt in range(1, READ_RETRIES + 1):
        try:
            data = np.fromfile(str(path), dtype=np.uint8)
            if data.size:
                return cv2.imdecode(data, cv2.IMREAD_COLOR)  # None if corrupt
        except OSError as exc:
            log.debug("Read error on %s (attempt %d): %s", path.name, attempt, exc)
        time.sleep(RETRY_BACKOFF_S * attempt)
    return None


def fit_to_canvas(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Fit ``frame`` inside a ``size`` = (width, height) canvas without stretching.

    The image is scaled to fit and centred on black bars (letterbox/pillarbox).
    Frames already at the target size are returned untouched.
    """
    width, height = size
    frame_h, frame_w = frame.shape[:2]
    if (frame_w, frame_h) == (width, height):
        return frame

    scale = min(width / frame_w, height / frame_h)
    new_w, new_h = max(1, round(frame_w * scale)), max(1, round(frame_h * scale))
    # INTER_AREA gives the best quality when shrinking, INTER_LINEAR when enlarging.
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(frame, (new_w, new_h), interpolation=interpolation)

    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    x, y = (width - new_w) // 2, (height - new_h) // 2
    canvas[y:y + new_h, x:x + new_w] = resized
    return canvas


def draw_timestamp(frame: np.ndarray, path: Path) -> np.ndarray:
    """Return a copy of ``frame`` with the photo's modified time in the bottom-left.

    Text is white with a dark outline so it stays legible on any background, and
    scales with the frame height. If the file's time cannot be read the frame is
    returned unchanged.
    """
    try:
        text = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return frame

    frame = frame.copy()  # never draw on the caller's array
    scale = max(0.5, frame.shape[0] / 720)
    thickness = max(1, round(scale * 2))
    origin = (int(10 * scale), frame.shape[0] - int(12 * scale))
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame, text, origin, font, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)  # outline
    cv2.putText(frame, text, origin, font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------- #
# 5. Video output
# --------------------------------------------------------------------------- #

def open_writer(path: str, fps: float, size: tuple[int, int]) -> Optional[cv2.VideoWriter]:
    """Open an OpenCV video writer, trying each codec in ``WRITER_CODECS``.

    Returns the first writer that opens successfully, or None if none do.
    """
    for codec in WRITER_CODECS:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*codec), fps, size)
        if writer.isOpened():
            log.debug("Using codec %s", codec)
            return writer
        writer.release()
    return None


def reencode_h264(
    src: Path,
    dst: Path,
    crf: int,
    on_progress: Optional[Callable[[int], None]] = None,
) -> bool:
    """Re-encode ``src`` to compact, web-friendly H.264 at ``dst`` using ffmpeg.

    ``on_progress(frames_done)`` is called as ffmpeg reports progress. Returns
    True on success. Any problem (no ffmpeg, crash, timeout, empty output)
    returns False and is logged, so the caller can keep the original file.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-i", str(src),
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-pix_fmt", "yuv420p",        # widest player compatibility
        "-movflags", "+faststart",    # lets browsers start playing before full download
        str(dst),
    ]
    proc = None
    try:
        with tempfile.TemporaryFile() as err:  # a file, not a pipe: cannot fill up and block ffmpeg
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err, text=True)

            def pump() -> None:
                """Forward ffmpeg's ``frame=N`` progress lines to the callback."""
                for line in proc.stdout:
                    if on_progress and line.startswith("frame="):
                        try:
                            on_progress(int(line[6:]))
                        except ValueError:
                            pass

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            try:
                proc.wait(timeout=FFMPEG_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg timed out; keeping the original encoding.")
                return False
            reader.join(timeout=5)
            err.seek(0)
            stderr = err.read().decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg failed to run (%s); keeping the original encoding.", exc)
        return False
    finally:
        if proc is not None and proc.poll() is None:  # timeout, Ctrl+C or error
            proc.kill()
            proc.wait()
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        log.warning("ffmpeg re-encode failed (%s); keeping the original encoding.", stderr.strip()[:200])
        return False
    return True


def move_with_progress(src: Path, dst: Path, on_progress: Optional[Callable[[int], None]] = None) -> None:
    """Move ``src`` to ``dst``, reporting bytes copied through ``on_progress``.

    Same-volume moves are an instant rename. Across volumes (for example a
    network share) the file is copied in chunks to ``<name>.part`` next to the
    destination and renamed when complete, so a partial file never carries the
    final name and the user can see the copy under way.
    """
    try:
        os.replace(src, dst)
        if on_progress:
            on_progress(src.stat().st_size if src.exists() else dst.stat().st_size)
        return
    except OSError:
        pass  # different volume: fall through to a chunked copy

    part = dst.with_name(dst.name + ".part")
    copied = 0
    try:
        with open(src, "rb") as source, open(part, "wb") as target:
            while chunk := source.read(COPY_CHUNK_BYTES):
                target.write(chunk)
                copied += len(chunk)
                if on_progress:
                    on_progress(copied)
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)  # never leave a partial file behind
        raise


def resolve_output_path(output_dir: Path, output_name: Optional[str]) -> Path:
    """Decide where the video will be saved, creating the folder if needed.

    * If ``output_dir`` cannot be created, falls back to ``~/Videos/TIMELAPSE``.
    * ``output_name`` is reduced to a plain file name (no folders, no characters
      that are illegal on Windows) and given an ``.mp4`` extension.
    * Without a name, ``timelapse_YYYYMMDD_HHMMSS.mp4`` is used.
    * An existing file is never overwritten; a time suffix is added instead.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback = Path.home() / "Videos" / "TIMELAPSE"
        log.warning("Cannot use output folder '%s' (%s). Falling back to '%s'.", output_dir, exc, fallback)
        output_dir = fallback
        output_dir.mkdir(parents=True, exist_ok=True)

    if output_name:
        name = re.sub(r'[<>:"/\\|?*]', "_", Path(output_name).name)  # blocks path traversal
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
    else:
        name = f"timelapse_{datetime.now():%Y%m%d_%H%M%S}.mp4"

    final = output_dir / name
    if final.exists():
        final = final.with_name(f"{final.stem}_{datetime.now():%H%M%S}{final.suffix}")
    return final


# --------------------------------------------------------------------------- #
# 6. Orchestration
# --------------------------------------------------------------------------- #

#: Signature of the optional progress callback: ``on_event(kind, **info)``.
EventCallback = Callable[..., None]


def _first_readable_frame(images: Sequence[Path]) -> tuple[Optional[np.ndarray], dict]:
    """Probe for the first photo that decodes, used to fix the video resolution.

    Returns ``(frame, known)`` where ``known`` maps list index -> already-read
    result (None for photos found unreadable), so they are not read twice.
    """
    known: dict = {}
    for index, image in enumerate(images):
        frame = read_image(image)
        known[index] = frame
        if frame is not None:
            return frame, known
    return None, known


def _prefetch(images: Sequence[Path], known: dict) -> Iterator[tuple[Path, Optional[np.ndarray]]]:
    """Yield ``(path, frame)`` in order, decoding a few photos ahead on worker threads.

    Decoding is the slow part and OpenCV releases the GIL, so reading ahead keeps
    the writer fed. The read-ahead window is bounded to limit memory use.
    """
    workers = max(1, min(READ_WORKERS, os.cpu_count() or 1))
    window = workers * 2
    pending: deque = deque()
    remaining = iter(enumerate(images))

    def fill(pool: ThreadPoolExecutor) -> None:
        while len(pending) < window:
            nxt = next(remaining, None)
            if nxt is None:
                return
            index, image = nxt
            if index in known:  # already read during the probe
                done: Future = Future()
                done.set_result(known.pop(index))
                pending.append((image, done))
            else:
                pending.append((image, pool.submit(read_image, image)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            fill(pool)
            while pending:
                image, future = pending.popleft()
                fill(pool)  # keep the pipeline full while we wait
                yield image, future.result()
        finally:
            for _, future in pending:  # early exit (abort / Ctrl+C)
                future.cancel()


def _render_frames(
    images: Sequence[Path],
    writer: cv2.VideoWriter,
    size: tuple[int, int],
    timestamp: bool,
    stats: dict,
    emit: EventCallback,
    known: Optional[dict] = None,
) -> bool:
    """Write every readable photo to ``writer``. Returns False if the run must abort.

    Unreadable photos are skipped and recorded in ``stats["skipped_names"]``.
    ``known`` optionally supplies photos that were already read (see
    ``_first_readable_frame``).
    """
    total = len(images)
    skipped = 0
    emit("start", total=total)
    for index, (image, frame) in enumerate(_prefetch(images, known or {}), 1):
        if frame is None:
            skipped += 1
            stats["skipped_names"].append(image.name)
            log.warning("Skipping unreadable image: %s", image.name)
            if skipped / total > MAX_SKIP_RATIO and skipped >= MIN_SKIPPED_TO_ABORT:
                log.error("Too many unreadable images (%d). Aborting.", skipped)
                return False
            emit("frame", done=index)
            continue

        frame = fit_to_canvas(frame, size)
        if timestamp:
            frame = draw_timestamp(frame, image)
        writer.write(frame)
        stats["written"] += 1
        emit("frame", done=index)
        if index % 100 == 0 or index == total:
            log.info("Processed %d/%d frames", index, total)
    return True


def generate_timelapse(
    images: Sequence[Path],
    output_path: Path,
    fps: float,
    timestamp: bool = False,
    crf: Optional[int] = None,
    on_event: Optional[EventCallback] = None,
    stats: Optional[dict] = None,
    max_width: Optional[int] = None,
) -> bool:
    """Build a timelapse video from ``images`` and save it at ``output_path``.

    Args:
        images: Photos in playback order (see ``find_images``).
        output_path: Destination file (see ``resolve_output_path``).
        fps: Playback frame rate.
        timestamp: Overlay each photo's modified time.
        crf: If given, re-encode to H.264 at this quality (0-51, lower is better)
            when ffmpeg is available; otherwise keep the standard encoding.
        on_event: Optional progress callback ``on_event(kind, **info)``. Kinds:
            ``"start"`` (total photos), ``"frame"`` (done), ``"stage"`` (text,
            optional total and unit: a new phase begins) and ``"progress"``
            (done: progress inside the current phase).
        stats: Optional dict filled in with a run summary. Keys: ``total``,
            ``written``, ``skipped_names``, ``resolution``, ``compressed``,
            ``size_bytes`` and ``elapsed`` (the last three only on success).
        max_width: If given, shrink the video to at most this width in pixels,
            keeping the aspect ratio (never enlarges). Much faster and smaller
            for high-resolution photos.

    Returns:
        True if the video was saved, False otherwise (the reason is logged).
        No partial output is ever left at ``output_path``.
    """
    emit = on_event or (lambda *args, **kwargs: None)
    stats = {} if stats is None else stats
    stats.update(written=0, skipped_names=[], total=len(images))
    started = time.monotonic()

    first, known = _first_readable_frame(images)
    if first is None:
        log.error("None of the images could be read.")
        return False

    # The first readable photo defines the resolution. Many codecs need even
    # dimensions, so odd sizes are rounded down by one pixel.
    height, width = first.shape[:2]
    if max_width and width > max_width:
        height, width = round(height * max_width / width), max_width
    size = (max(2, width - width % 2), max(2, height - height % 2))
    stats["resolution"] = size

    # Encode in a local temp folder, then move the finished file into place.
    # OpenCV's writer is unreliable on cloud-synced / non-ASCII paths, and sync
    # clients could otherwise upload a half-written file.
    tmp_dir = Path(tempfile.mkdtemp(prefix="timelapse_"))
    tmp_file = tmp_dir / "out.mp4"
    writer = None
    try:
        writer = open_writer(str(tmp_file), fps, size)
        if writer is None:
            log.error("Could not open a video writer (no usable codec).")
            return False

        log.info("Starting timelapse: %d frames at %s fps (%dx%d)", len(images), fps, *size)
        if not _render_frames(images, writer, size, timestamp, stats, emit, known):
            return False
        writer.release()  # flush the file before checking or moving it
        writer = None

        if stats["written"] == 0 or not tmp_file.exists() or tmp_file.stat().st_size == 0:
            log.error("No video produced.")
            return False

        if crf is not None:
            written = stats["written"]
            emit("stage", text="Compressing (H.264)", total=written)
            log.info("Compressing to H.264 (this can take a while for long videos)...")
            h264_file = tmp_dir / "out_h264.mp4"
            if reencode_h264(tmp_file, h264_file, crf,
                             on_progress=lambda n: emit("progress", done=min(n, written))):
                emit("progress", done=written)
                tmp_file = h264_file
                stats["compressed"] = True
            elif not find_ffmpeg():
                log.warning("ffmpeg is not available; saving with the default (larger) encoding.")

        size_bytes = tmp_file.stat().st_size
        emit("stage", text="Saving to output folder", total=size_bytes, unit="bytes")
        log.info("Saving %s ...", output_path)
        move_with_progress(tmp_file, output_path, lambda n: emit("progress", done=min(n, size_bytes)))
        stats["size_bytes"] = output_path.stat().st_size
        stats["elapsed"] = time.monotonic() - started
        log.info("Saved %s (%d frames, %d skipped)",
                 output_path, stats["written"], len(stats["skipped_names"]))
        return True
    except KeyboardInterrupt:
        log.error("Cancelled by user. No output was saved.")
        return False
    except Exception:
        log.exception("Unexpected error while generating video")
        return False
    finally:
        if writer is not None:  # only still set if we left early
            writer.release()
        shutil.rmtree(tmp_dir, ignore_errors=True)  # always remove temp files
