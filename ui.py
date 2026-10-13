"""Stylised console UI for the timelapse generator.

Built on `rich` (bundled with the standalone build). Everything degrades to plain
logging when output is not a terminal (pipes, schedulers, CI) or rich is missing.

This module only presents and asks; it never touches images or video. Sections:
    Setup and helpers   console, logging, formatting, path cleaning
    Display             banner, step headings, messages, panels
    Prompts             folder / speed / options / confirmation
    Progress and result live progress bar, result and diagnostics panels
    Small actions       open folder, repeat, pause
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine import DEFAULT_CRF, MAX_FPS

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.progress import (BarColumn, Progress, SpinnerColumn,
                               TaskProgressColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn)
    from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
    from rich.rule import Rule
    from rich.table import Column, Table
    from rich.text import Text
    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False

# --------------------------------------------------------------------------- #
# Setup and helpers
# --------------------------------------------------------------------------- #

VERSION = "2.0"
ACCENT = "cyan"  # main highlight colour
_GRADIENT = ((0, 215, 255), (255, 95, 215))  # banner colours, left to right
_FONT = {  # 5-row block letters for the banner; only the letters of "TIMELAPSE"
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "I": ["███", " █ ", " █ ", " █ ", "███"],
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "E": ["████", "█   ", "███ ", "█   ", "████"],
    "L": ["█   ", "█   ", "█   ", "█   ", "████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "P": ["████ ", "█   █", "████ ", "█    ", "█    "],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
}
_console = None  # created lazily by console()


def fancy_output():
    """True when styled output is possible: rich is installed and stdout is a terminal."""
    return HAVE_RICH and sys.stdout is not None and sys.stdout.isatty()


def console():
    """Return the shared rich Console, creating it (UTF-8 output, window title) on first use."""
    global _console
    if _console is None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        _console = Console(highlight=False)
        try:
            _console.set_window_title("Timelapse Generator")
        except Exception:
            pass
    return _console


def setup_logging(verbose):
    """Route log records through rich so warnings match the UI style."""
    handler = RichHandler(console=console(), show_time=False, show_path=False, markup=False)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING,
                        format="%(message)s", handlers=[handler], force=True)


def fmt_duration(seconds):
    """12.5 -> '12.5 s'; 125 -> '2 min 05 s'."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m} min {s:02d} s"


def fmt_size(num):
    """Bytes -> human-readable size ('1.5 MB')."""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def clean_path(raw):
    """Accept drag-and-dropped paths: quotes, PowerShell's '& ' prefix, shell-escaped spaces."""
    s = raw.strip()
    if s.startswith("& "):
        s = s[2:].strip()
    s = s.strip("\"'")
    if os.name != "nt":
        s = s.replace("\\ ", " ")
    return Path(s).expanduser()


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #

def _gradient_line(row, width):
    """Colour one banner row with a left-to-right gradient."""
    text = Text(no_wrap=True)
    for i, ch in enumerate(row.ljust(width)):
        t = i / max(1, width - 1)
        r, g, b = (round(a + (z - a) * t) for a, z in zip(*_GRADIENT))
        text.append(ch, style=f"rgb({r},{g},{b})" if ch != " " else None)
    return text


def banner():
    """Print the logo; narrow terminals get a compact title instead."""
    c = console()
    rows = [""] * 5
    for ch in "TIMELAPSE":
        rows = [r + g + " " for r, g in zip(rows, _FONT[ch])]
    width = max(len(r) for r in rows)
    tagline = Text("Turn a folder of photos into a video", style="dim")
    if c.width >= width + 10:
        body = Group(*[_gradient_line(r, width) for r in rows], Text(""), Align.center(tagline))
    else:
        body = Group(Align.center(Text("TIMELAPSE", style=f"bold {ACCENT}")), Align.center(tagline))
    c.print(Panel(Align.center(body), box=box.ROUNDED, border_style="bright_black",
                  padding=(1, 2), subtitle=f"[dim]v{VERSION}[/]"))


def step(n, total, title):
    """Print a numbered wizard step heading."""
    c = console()
    c.print()
    c.print(Rule(f"[bold {ACCENT}]{n}/{total}[/]  [bold]{title}[/]", style="bright_black", align="left"))


def info(message):
    """Print an accent-coloured status line."""
    console().print(f"[{ACCENT}]›[/] {message}")


def error(message):
    """Print a red error line."""
    console().print(f"[bold red]✖[/] {message}")


def _grid():
    """Two-column label/value table used inside panels."""
    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim", justify="right")
    g.add_column()
    return g


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def ask_folder(scan, recursive=False):
    """Ask for the photos folder until it holds images.

    ``scan(folder, recursive)`` returns the image list. Returns
    ``(folder, images, recursive)``, or None if the user quits.
    """
    c = console()
    while True:
        raw = Prompt.ask("[bold]Photos folder[/] [dim](drag a folder here or paste its path · q to quit)[/]", console=c)
        if not raw.strip():
            continue
        if raw.strip().lower() in ("q", "quit", "exit"):
            return None
        path = clean_path(raw)
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            error(f"That is not a folder: {path}")
            continue
        try:
            with c.status(f"[{ACCENT}]Scanning for photos…") as status:
                def found(n):
                    status.update(f"[{ACCENT}]Scanning for photos… [bold]{n:,}[/] found")
                images = scan(path, recursive, on_progress=found)
                deeper = scan(path, True, on_progress=found) if not images and not recursive else []
        except OSError as exc:
            error(f"Cannot read that folder: {exc}")
            continue
        if deeper and Confirm.ask(f"No photos here, but {len(deeper)} in subfolders. Include subfolders?",
                                  default=True, console=c):
            images, recursive = deeper, True
        if not images:
            error("No supported photos found (jpg, jpeg, png, bmp, tif, tiff).")
            continue
        _show_found(path, images, recursive)
        return path, images, recursive


def _total_size(images, status):
    """Sum photo sizes in parallel (stat calls are slow on network drives), updating ``status``."""
    def size(img):
        try:
            return img.stat().st_size
        except OSError:
            return 0

    total = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, s in enumerate(pool.map(size, images), 1):
            total += s
            if i % 250 == 0:
                status.update(f"[{ACCENT}]Measuring photos… [bold]{i:,}[/]/{len(images):,}")
    return total


def _show_found(path, images, recursive):
    """Summarise the discovered photos in a panel."""
    with console().status(f"[{ACCENT}]Measuring photos…") as status:
        total = _total_size(images, status)
    g = _grid()
    g.add_row("Folder", str(path) + ("  [dim](+ subfolders)[/]" if recursive else ""))
    g.add_row("Photos", f"[bold green]{len(images):,}[/]  [dim]({fmt_size(total)})[/]")
    g.add_row("First", images[0].name)
    g.add_row("Last", images[-1].name)
    console().print(Panel(g, title="[green]✔ Found photos[/]", title_align="left",
                          border_style="green", box=box.ROUNDED, padding=(0, 2)))


def ask_speed(n):
    """Show speed presets with the resulting video length for ``n`` photos; return fps."""
    c = console()
    table = Table(box=box.SIMPLE_HEAD, border_style="bright_black", padding=(0, 2))
    table.add_column("#", style=f"bold {ACCENT}", justify="center")
    table.add_column("Speed")
    table.add_column("Rate", style="dim")
    table.add_column("Video length", justify="right")
    for key, name, fps in (("1", "Slow", 10), ("2", "Normal", 24), ("3", "Fast", 60)):
        table.add_row(key, name, f"{fps} fps", fmt_duration(n / fps))
    table.add_row("4", "Custom length", "you pick the seconds", "")
    table.add_row("5", "Custom rate", "you pick the fps", "")
    c.print(table)
    choice = Prompt.ask("Choose a speed", choices=["1", "2", "3", "4", "5"], default="2", console=c)
    if choice in ("1", "2", "3"):
        fps = {"1": 10, "2": 24, "3": 60}[choice]
    elif choice == "4":
        while True:
            seconds = FloatPrompt.ask("Video length in seconds", default=float(max(1, round(n / 24))), console=c)
            if seconds > 0:
                break
            error("Enter a number greater than 0.")
        fps = min(MAX_FPS, max(1, round(n / seconds)))
    else:
        while True:
            fps = IntPrompt.ask("Frames per second", default=24, console=c)
            if 1 <= fps <= MAX_FPS:
                break
            error(f"Enter a number between 1 and {MAX_FPS}.")
    info(f"{fps} fps  ·  video length [bold]{fmt_duration(n / fps)}[/]")
    return fps


def ask_options(default_dir, ffmpeg_ok, timestamp=False, crf=None, output_name=None, output_dir=None):
    """Ask for date overlay, compression, file name and destination; return the choices as a dict."""
    c = console()
    ts = Confirm.ask("Overlay each photo's date and time?", default=timestamp, console=c)
    new_crf = None
    if ffmpeg_ok and Confirm.ask("Compress to a smaller H.264 file?", default=True, console=c):
        new_crf = DEFAULT_CRF if crf is None else crf
    name = Prompt.ask("File name [dim](Enter = automatic)[/]", default="", show_default=False, console=c)
    out = Prompt.ask("Save to", default=str(output_dir or default_dir), console=c)
    return {"timestamp": ts, "crf": new_crf, "output_name": name.strip() or None, "output_dir": clean_path(out)}


def confirm_plan(rows):
    """Show the (label, value) plan in a panel and ask whether to start."""
    c = console()
    g = _grid()
    for label, value in rows:
        g.add_row(label, value)
    c.print()
    c.print(Panel(g, title=f"[bold {ACCENT}]Ready to render[/]", title_align="left",
                  border_style=ACCENT, box=box.ROUNDED, padding=(0, 2)))
    return Confirm.ask("Start rendering?", default=True, console=c)


# --------------------------------------------------------------------------- #
# Progress and results
# --------------------------------------------------------------------------- #

class RunProgress:
    """Live multi-phase progress. Use as ``with RunProgress() as p:`` and pass ``p``
    as the ``on_event`` callback of ``engine.generate_timelapse``.

    Only ONE live line is ever drawn, and it is short enough never to wrap. (A
    multi-row or wide live area is corrupted when the window is resized, because
    the terminal re-wraps rows the program believes it already drew.) When a phase
    ends, a single permanent summary line is printed above the live line, so the
    user still sees every step that completed.

    Events: "start" (total), "stage" (text, total, unit), "frame" / "progress" (done).
    """

    LIVE_WIDTH = 78  # the live line never exceeds this many columns

    def __enter__(self):
        self._task = None
        self._text = ""
        self._total = 0
        self._done = 0
        self._unit = "frames"
        self._started = time.monotonic()
        self._progress = Progress(
            SpinnerColumn(style=ACCENT),
            TextColumn("[bold]{task.description}", table_column=Column(width=20, no_wrap=True, overflow="ellipsis")),
            BarColumn(bar_width=14, complete_style=ACCENT, finished_style="green"),
            TaskProgressColumn(),
            TextColumn("{task.fields[count]}", table_column=Column(width=17, justify="right", no_wrap=True)),
            TimeElapsedColumn(), TimeRemainingColumn(),
            console=console(), expand=False)
        self._progress.start()
        # Visible immediately: reading the first photo can take a while on slow storage.
        self._begin("Reading first photo", None, "frames")
        return self

    def __exit__(self, exc_type, *rest):
        if exc_type is None:
            self._finish_phase()
        self._progress.stop()
        return False

    def _finish_phase(self):
        """Replace the live line of the current phase with a permanent summary line."""
        if self._task is None:
            return
        elapsed = fmt_duration(time.monotonic() - self._started)
        detail = ""
        if self._unit == "bytes" and self._total:
            detail = f"  [dim]{fmt_size(self._total)}[/]"
        elif self._total:
            detail = f"  [dim]{self._total:,} frames[/]"
        self._progress.remove_task(self._task)
        self._task = None
        self._progress.console.print(f"[green]✔[/] {self._text}{detail}  [dim]{elapsed}[/]")

    def _begin(self, text, total, unit):
        """Close the previous phase and start a new live line."""
        self._finish_phase()
        self._text, self._total, self._unit, self._started = text, total or 0, unit, time.monotonic()
        self._task = self._progress.add_task(text, total=total, count="")

    def _update(self, done):
        if self._unit == "bytes":
            count = f"{fmt_size(done)} / {fmt_size(self._total)}"
        else:
            count = f"{done:,}/{self._total:,}"
        self._progress.update(self._task, completed=done, count=count)

    def __call__(self, event, **kw):
        if event == "start":
            self._begin("Building video", kw["total"], "frames")
        elif event == "stage":
            self._begin(kw["text"], kw.get("total"), kw.get("unit", "frames"))
        elif event in ("frame", "progress") and self._task is not None:
            self._update(kw["done"])


def show_result(ok, output_path, stats, fps):
    """Show a success panel built from ``stats``, or a failure panel."""
    c = console()
    c.print()
    c.bell()  # audible/taskbar notification that the long job has ended
    if not ok:
        c.print(Panel("No video was created. Details are shown above.\n"
                      "[dim]Run with --verbose for more detail, or --check to test this computer.[/]",
                      title="[bold red]✖ Failed[/]", title_align="left", border_style="red",
                      box=box.ROUNDED, padding=(0, 2)))
        return
    written = stats.get("written", 0)
    g = _grid()
    g.add_row("File", f"[bold]{output_path}[/]")
    g.add_row("Size", fmt_size(stats.get("size_bytes", 0)) + ("  [dim](H.264)[/]" if stats.get("compressed") else ""))
    g.add_row("Length", f"{fmt_duration(written / fps)}  [dim]({written:,} frames at {fps:g} fps)[/]")
    if stats.get("resolution"):
        g.add_row("Resolution", "%d × %d" % stats["resolution"])
    skipped = stats.get("skipped_names", [])
    if skipped:
        g.add_row("Skipped", f"[yellow]{len(skipped)} unreadable photo(s)[/]")
    g.add_row("Took", fmt_duration(stats.get("elapsed", 0)))
    c.print(Panel(g, title="[bold green]✔ Timelapse ready[/]", title_align="left",
                  border_style="green", box=box.ROUNDED, padding=(1, 2)))
    for name in skipped[:5]:
        c.print(f"  [yellow]![/] [dim]skipped {name}[/]")
    if len(skipped) > 5:
        c.print(f"  [dim]… and {len(skipped) - 5} more[/]")


def show_check(rows):
    """rows: list of (label, value, ok)."""
    c = console()
    g = Table.grid(padding=(0, 2))
    g.add_column(justify="center")
    g.add_column(style="dim", justify="right")
    g.add_column()
    for label, value, ok in rows:
        g.add_row("[green]✔[/]" if ok else "[red]✖[/]", label, str(value))
    all_ok = all(ok for _, _, ok in rows)
    c.print(Panel(g, title="[bold]Diagnostics[/]", title_align="left",
                  border_style="green" if all_ok else "red", box=box.ROUNDED, padding=(1, 2)))


# --------------------------------------------------------------------------- #
# Small actions
# --------------------------------------------------------------------------- #

def open_folder(path):
    """Open a folder in the system file manager (Windows, macOS, Linux)."""
    try:
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        console().print("[dim]Could not open the folder automatically.[/]")


def offer_open(folder):
    """Offer to open the output folder."""
    if Confirm.ask("Open the output folder?", default=False, console=console()):
        open_folder(folder)


def ask_again():
    """Ask whether to make another timelapse."""
    return Confirm.ask("Make another timelapse?", default=False, console=console())


def pause():
    """Wait for Enter so a double-clicked window stays readable."""
    console().input("[dim]Press Enter to close…[/]")
