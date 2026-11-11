#!/usr/bin/env python3
"""Create a sample directory with various files to test organizer.py."""

import os
import sys
from pathlib import Path

FILES = {
    "report.pdf": 52_000, "invoice_march.pdf": 31_000, "notes.txt": 800,
    "budget.xlsx": 18_000, "data.csv": 4_000,
    "photo.JPG": 250_000, "vacation.png": 410_000, "logo.svg": 3_000,
    "movie.mp4": 9_000_000, "clip.mkv": 3_500_000, "song.mp3": 4_200_000,
    "backup.zip": 1_200_000, "archive.tar.gz": 800_000,
    "script.py": 1_500, "index.html": 2_200, "config.json": 600,
    "setup.exe": 2_000_000, "mystery.xyz": 100, "README": 300,
    ".hidden_settings": 50,
    "video.mp4.crdownload": 70_000, "partial.part": 40_000,
    "Documents/report.pdf": 1_000,
    "old_stuff/extra.pdf": 7_000, "old_stuff/pic.png": 5_000,
    "my_project/main.py": 900,
}


def make_demo(target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    for name, size in FILES.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    (target / "my_project" / ".git").mkdir(exist_ok=True)
    link = target / "link_to_report.pdf"
    if not link.is_symlink():
        try:
            os.symlink("report.pdf", link)
        except (OSError, NotImplementedError):
            pass
    return sum(1 for _ in target.rglob("*"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_folder")
    count = make_demo(folder)
    print(f"Created {count} entries in {folder}/ - now try:")
    print(f"  python organizer.py {folder} --dry-run")
