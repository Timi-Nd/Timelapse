"""Build a standalone executable for the current OS (PyInstaller cannot cross-compile).

Run on Windows to get dist/timelapse.exe, on Linux to get dist/timelapse.
The result bundles Python, OpenCV and ffmpeg, so the target machine needs nothing installed.
"""
import subprocess
import sys


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "timelapse",
        "--collect-all", "imageio_ffmpeg",
        "--collect-all", "cv2",
        "--collect-all", "rich",
        "main.py",
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
