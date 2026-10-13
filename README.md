# Timelapse Generator

Turn a folder of photos into an MP4 timelapse video. Runs on Windows and Linux, from a guided console wizard or a single command, and ships as a standalone executable that needs nothing installed.

[![build](../../actions/workflows/build.yml/badge.svg)](../../actions/workflows/build.yml)

**Features:** natural file ordering (`img2` before `img10`), non-ASCII path support, letterboxing for mixed sizes, optional date overlay, optional smaller H.264 output, resize for large photos, skips corrupt photos, never overwrites existing videos, never modifies your photos.

## Running with nothing installed (Windows and Linux)
The standalone build bundles Python, OpenCV and ffmpeg, so the target machine needs **nothing** installed.
- Windows: copy `timelapse.exe` and double-click it, or run `timelapse.exe "C:\Photos"`.
- Linux: copy `timelapse`, then `chmod +x timelapse` and run `./timelapse /path/to/photos`.

A build only runs on the OS it was built on. To produce both, either run `python build.py` on each OS
(after `pip install -r requirements-dev.txt`), or push this folder to GitHub: the workflow in
`.github/workflows/build.yml` builds `timelapse.exe` and the Linux `timelapse` and publishes them as downloadable artifacts.
Linux binaries need a reasonably recent glibc (the CI build targets Ubuntu 22.04+); no extra libraries are required.

## Requirements (only when running from source or building)
Python 3.9+ and the packages in `requirements.txt` (CI tests on Python 3.12):
```
pip install -r requirements.txt
python main.py
```
Use `requirements-dev.txt` to also get `pytest` and `pyinstaller`.

## Project layout
| File | Role |
|---|---|
| `engine.py` | Core logic: find/read images, build the video, H.264 re-encode. No UI, no printing. |
| `ui.py` | All console presentation (banner, prompts, progress, panels). |
| `main.py` | Entry point: argument parsing, wizard flow, diagnostics. |
| `test_engine.py`, `test_main.py`, `test_ui.py` | Tests for each layer. |
| `build.py` | Builds the standalone executable with PyInstaller. |
| `.github/workflows/build.yml` | CI: runs the tests and builds Windows and Linux executables. |

## Tests
```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Options
`--fps N` (skips the speed prompt), `--output_name NAME`, `--output_dir FOLDER`, `--verbose`.
- `--recursive`: also include images in subfolders (hidden folders are skipped).
- `--timestamp`: overlay each photo's modified time on the video.
- `--crf [N]`: re-encode to smaller H.264 (quality 0-51, default 23) using the bundled ffmpeg.
- `--max-width N`: shrink the video to at most N pixels wide (aspect ratio kept, never enlarged). Much faster and smaller for large photos, e.g. `--max-width 1920`.
- Default output folder: `~/Videos/TIMELAPSE` (or `~/TIMELAPSE` if there is no `Videos` folder), on both Windows and Linux. Override with `--output_dir`.
- `--check`: print a diagnostic report (OS, Python, OpenCV, ffmpeg, output folder writable) and exit.
The program exits with 0 on success, 1 on failure, 2 on bad arguments and 130 when cancelled, so it can run unattended (pass the folder and `--fps`).
Existing videos are never overwritten, and the video is only saved into the output folder once fully built.

## Usage
Run `timelapse` (the standalone build) or `python main.py` (from source) from a terminal, or double-click the standalone build.

### Simple Usage (Interactive)
Start the program with no arguments to get a guided, four-step console wizard:
1. **Choose your photos**: drag a folder into the window or paste its path. Quotes and shell-escaped spaces are handled, a photo file is accepted too (its folder is used), and if the photos are only in subfolders you are offered to include them.
2. **Pick a speed**: a table shows the resulting video length for each preset (Slow, Normal, Fast), or choose a custom length in seconds or a custom frame rate.
3. **Options**: date/time overlay, H.264 compression, file name and save location.
4. **Render**: review a summary, then watch live progress through each phase (reading, building, compressing, saving) with counts and time remaining; every finished phase leaves a ✔ line. The video is built in a temporary folder and appears in the output folder when finished, and the terminal bell sounds on completion. A result card shows the file, size, length, resolution and any skipped photos, with an offer to open the output folder or make another timelapse.

Press Ctrl+C at any time to cancel. When output is not a terminal (pipes, scheduled tasks), the program falls back to plain log lines and never prompts.

### Speed Options
Without `--fps`, the program counts your photos and asks how fast the video should be:
- **Slow**: Best for seeing details (10 photos per second).
- **Normal**: Standard video speed (24 photos per second).
- **Fast**: Good for long events (60 photos per second).
- **Custom**: You tell the program exactly how many seconds you want the video to last (e.g., "I want a 10 second video"), and it calculates the speed for you!

### Command Line Usage
Windows: `timelapse.exe "C:\Path\To\Photos"`  Linux: `./timelapse /path/to/photos`

The video is saved to the default output folder (see Options) with a name like `timelapse_YYYYMMDD_HHMMSS.mp4`.

## Troubleshooting
Run `timelapse --check` (or `python main.py --check`) to see what is working and what is not.
If running from source and you see `ModuleNotFoundError`, run `pip install -r requirements.txt`.

## Safety & Data Integrity
**Your source photos are safe.** The input folder is only read:
- No delete, move, or rename operations are performed on your original images.
- All output is saved to a separate directory, keeping your source folder untouched.

### Custom Output Name
```
timelapse /path/to/photos --output_name my_timelapse
```

### Custom Frames Per Second (FPS)
Default is 24 FPS. To change it:
```
timelapse /path/to/photos --fps 30
```

## How it works
1. The program finds all common image files (.jpg, .jpeg, .png, .bmp, .tif, .tiff) in the input folder.
2. It sorts them naturally (`img2` before `img10`).
3. It creates a video using the dimensions of the first readable image (or `--max-width` if smaller).
4. Images with different sizes are letterboxed (not stretched). Unreadable images are skipped and logged; the run aborts if too many fail.
