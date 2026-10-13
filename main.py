"""Timelapse Generator: command-line entry point.

Three ways to run it, chosen automatically:

    timelapse                 interactive wizard (a real terminal, no folder given)
    timelapse <folder> ...    one-shot render with styled progress (real terminal)
    timelapse <folder> ...    plain log output, never prompts (pipes, schedulers, CI)

Module layout
    engine.py   image processing and video creation (no UI)
    ui.py       all console presentation and prompts
    main.py     this file: argument parsing and the flow that connects the two

Exit codes: 0 success, 1 failure, 2 invalid arguments, 130 cancelled by the user.
"""
from __future__ import annotations

import argparse
import logging
import platform
import sys
from pathlib import Path
from typing import Optional

import cv2

import engine
import ui

log = logging.getLogger("timelapse")

EXIT_OK, EXIT_FAILED, EXIT_BAD_ARGS, EXIT_CANCELLED = 0, 1, 2, 130


# --------------------------------------------------------------------------- #
# Diagnostics (--check)
# --------------------------------------------------------------------------- #

def run_check(output_dir: Path) -> int:
    """Report what works on this computer. Exit code reflects the output folder."""
    rows = [  # (label, value, ok)
        ("OS", platform.platform(), True),
        ("Python", sys.version.split()[0], True),
        ("OpenCV", cv2.__version__, True),
    ]

    ffmpeg = engine.find_ffmpeg()
    rows.append(("ffmpeg", ffmpeg or "not available (--crf will be skipped)", bool(ffmpeg)))

    # Prove the folder is writable by writing and removing a probe file.
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".write_test"
        probe.write_bytes(b"x")
        probe.unlink()
        rows.append(("Output folder", f"{output_dir} (writable)", True))
    except OSError as exc:
        rows.append(("Output folder", f"{output_dir} NOT writable ({exc})", False))

    if ui.fancy_output():
        ui.banner()
        ui.show_check(rows)
    else:
        print("--- Timelapse diagnostic ---")
        for label, value, ok in rows:
            print(f"{'OK  ' if ok else 'FAIL'} {label}: {value}")

    output_folder_ok = rows[-1][2]
    return EXIT_OK if output_folder_ok else EXIT_FAILED


# --------------------------------------------------------------------------- #
# Interactive wizard
# --------------------------------------------------------------------------- #

def run_wizard(args: argparse.Namespace) -> int:
    """Guide the user through four steps, repeating until they quit.

    Command-line options given alongside (``--timestamp``, ``--crf``, ...) are
    used as the wizard's pre-selected defaults.
    """
    engine.warm_up_ffmpeg()  # runs while the user types their answers
    ui.banner()
    ok = True
    while True:
        # Step 1: which photos?
        ui.step(1, 4, "Choose your photos")
        picked = ui.ask_folder(engine.find_images, args.recursive)
        if picked is None:  # user typed q
            return EXIT_OK if ok else EXIT_FAILED
        folder, images, _recursive = picked

        # Step 2: how fast?
        ui.step(2, 4, "Pick a speed")
        fps = ui.ask_speed(len(images))

        # Step 3: extras and destination.
        ui.step(3, 4, "Options")
        default_dir = Path(args.output_dir).expanduser() if args.output_dir else engine.default_output_dir()
        opts = ui.ask_options(default_dir, engine.find_ffmpeg() is not None,
                              timestamp=args.timestamp, crf=args.crf, output_name=args.output_name)

        # Step 4: review, confirm, render.
        ui.step(4, 4, "Render")
        summary = [
            ("Photos", f"{len(images):,} from {folder}"),
            ("Speed", f"{fps:g} fps  ·  {ui.fmt_duration(len(images) / fps)}"),
            ("Date overlay", "yes" if opts["timestamp"] else "no"),
            ("Compression", "H.264" if opts["crf"] is not None else "standard"),
            ("Save to", str(opts["output_dir"])),
        ]
        if not ui.confirm_plan(summary):
            ui.info("Cancelled. Starting over.")
            continue

        output_path = engine.resolve_output_path(opts["output_dir"], opts["output_name"])
        ui.info(f"Building in a temporary folder. [bold]{output_path.name}[/] will appear in "
                f"[bold]{output_path.parent}[/] when it is finished.")
        stats: dict = {}
        with ui.RunProgress() as progress:
            ok = engine.generate_timelapse(images, output_path, fps, timestamp=opts["timestamp"],
                                           crf=opts["crf"], on_event=progress, stats=stats,
                                           max_width=args.max_width)
        ui.show_result(ok, output_path, stats, fps)
        if ok:
            ui.offer_open(output_path.parent)
        if not ui.ask_again():
            return EXIT_OK if ok else EXIT_FAILED


# --------------------------------------------------------------------------- #
# One-shot command line
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Define the command-line options."""
    parser = argparse.ArgumentParser(description="Generate a timelapse video from a folder of images.")
    parser.add_argument("input", nargs="?", help="Folder containing photos (omit for the interactive wizard).")
    parser.add_argument("--output_name", help="Output file name (optional).")
    parser.add_argument("--output_dir", default=None, help="Output folder (default: ~/Videos/TIMELAPSE).")
    parser.add_argument("--fps", type=float, default=None, help="Frames per second. Skips the speed prompt.")
    parser.add_argument("--recursive", action="store_true", help="Include images in subfolders.")
    parser.add_argument("--timestamp", action="store_true", help="Overlay each file's modified time.")
    parser.add_argument("--crf", type=int, nargs="?", const=engine.DEFAULT_CRF, default=None,
                        help=f"Re-encode to smaller H.264 (quality 0-{engine.MAX_CRF}, lower is better; "
                             f"default {engine.DEFAULT_CRF}).")
    parser.add_argument("--max-width", dest="max_width", type=int, default=None,
                        help="Shrink the video to at most this width in pixels (keeps aspect ratio, "
                             "never enlarges). Faster and smaller for big photos, e.g. 1920.")
    parser.add_argument("--check", action="store_true", help="Print a diagnostic report and exit.")
    parser.add_argument("--verbose", action="store_true", help="Show detailed log messages.")
    return parser


def run_single(args: argparse.Namespace, fancy: bool, interactive: bool) -> int:
    """Render one folder given on the command line."""
    folder = ui.clean_path(args.input) if fancy else Path(args.input).expanduser()
    if not folder.is_dir():
        log.error("Input path '%s' is not a valid directory.", folder)
        return EXIT_FAILED

    try:
        images = engine.find_images(folder, args.recursive)
    except OSError as exc:
        log.error("Cannot read folder '%s': %s", folder, exc)
        return EXIT_FAILED
    if not images:
        log.error("No images (%s) found in '%s'.", ", ".join(sorted(engine.VALID_EXTENSIONS)), folder)
        return EXIT_FAILED

    if fancy:
        ui.banner()

    # Without --fps: ask in a terminal, otherwise use the standard 24 fps.
    fps = args.fps
    if fps is None:
        fps = ui.ask_speed(len(images)) if interactive else 24

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else engine.default_output_dir()
    output_path = engine.resolve_output_path(output_dir, args.output_name)
    stats: dict = {}
    if fancy:
        ui.info(f"Rendering [bold]{len(images):,}[/] photos at {fps:g} fps. Building in a temporary folder; "
                f"[bold]{output_path.name}[/] will appear in [bold]{output_path.parent}[/] when finished.")
        with ui.RunProgress() as progress:
            ok = engine.generate_timelapse(images, output_path, fps, timestamp=args.timestamp,
                                           crf=args.crf, on_event=progress, stats=stats,
                                           max_width=args.max_width)
        ui.show_result(ok, output_path, stats, fps)
    else:
        ok = engine.generate_timelapse(images, output_path, fps, timestamp=args.timestamp,
                                       crf=args.crf, stats=stats, max_width=args.max_width)
    return EXIT_OK if ok else EXIT_FAILED


def main(argv: Optional[list] = None) -> int:
    """Program entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    # Styled output only when a human is watching; plain logs otherwise.
    fancy = ui.fancy_output()
    if fancy:
        ui.setup_logging(args.verbose)
    else:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(levelname)s: %(message)s")

    if args.check:
        return run_check(Path(args.output_dir).expanduser() if args.output_dir else engine.default_output_dir())

    # Validate numeric options up front, before any work or prompting.
    if args.fps is not None and not 0 < args.fps <= engine.MAX_FPS:
        log.error("--fps must be between 0 and %d.", engine.MAX_FPS)
        return EXIT_BAD_ARGS
    if args.crf is not None and not 0 <= args.crf <= engine.MAX_CRF:
        log.error("--crf must be between 0 and %d.", engine.MAX_CRF)
        return EXIT_BAD_ARGS
    if args.max_width is not None and args.max_width < engine.MIN_MAX_WIDTH:
        log.error("--max-width must be at least %d.", engine.MIN_MAX_WIDTH)
        return EXIT_BAD_ARGS

    interactive = fancy and sys.stdin is not None and sys.stdin.isatty()
    try:
        if args.input is None:
            if not interactive:
                log.error("No input folder given.")
                return EXIT_BAD_ARGS
            return run_wizard(args)
        return run_single(args, fancy, interactive)
    except (KeyboardInterrupt, EOFError):
        ui.console().print("\n[dim]Cancelled.[/]")
        return EXIT_CANCELLED


if __name__ == "__main__":
    exit_code = main()
    # A double-clicked executable would close its window before the result can be
    # read, so wait for Enter (only for a frozen build started without arguments).
    if getattr(sys, "frozen", False) and len(sys.argv) == 1 and ui.fancy_output():
        ui.pause()
    sys.exit(exit_code)
