#!/usr/bin/env python3
"""Organize files into category folders based on extension with dry-run and undo."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Categories
CATEGORIES = {
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".epub",
                  ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".odp", ".ods"},
    "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
                  ".tiff", ".heic", ".ico", ".raw"},
    "Videos":    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "Audio":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"},
    "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "Code":      {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go",
                  ".rs", ".rb", ".php", ".html", ".css", ".json", ".xml", ".yml",
                  ".yaml", ".sh", ".sql", ".ipynb", ".r"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk"},
}
UNKNOWN_CATEGORY = "Others"

EXTENSION_MAP = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload"}
IGNORED_FOLDERS = {"node_modules", "__pycache__", "venv", "env"}

STATE_DIR = ".organizer"
SELF = Path(__file__).resolve()

log = logging.getLogger("organizer")


# Data models
@dataclass
class Move:
    src: str
    dst: str
    category: str
    size: int = 0
    renamed: bool = False


@dataclass
class Report:
    root: str
    dry_run: bool = False
    scanned: int = 0
    moves: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    errors: list = field(default_factory=list)
    created_dirs: list = field(default_factory=list)
    seconds: float = 0.0
    manifest: str = ""

    @property
    def bytes_moved(self) -> int:
        return sum(m.size for m in self.moves)

    @property
    def renamed(self) -> int:
        return sum(1 for m in self.moves if m.renamed)

    @property
    def by_category(self) -> Counter:
        return Counter(m.category for m in self.moves)


# Helpers
def categorize(name: str) -> str:
    return EXTENSION_MAP.get(Path(name).suffix.lower(), UNKNOWN_CATEGORY)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def candidate_names(dest: Path):
    """Yield dest, then 'name (1).ext', 'name (2).ext', ..."""
    yield dest
    n = 1
    while True:
        yield dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1


def plan_destination(dest: Path, taken: set) -> Path:
    """Pick a free name for a dry run (also avoids names planned earlier in the run)."""
    for candidate in candidate_names(dest):
        if candidate not in taken and not os.path.lexists(candidate):
            taken.add(candidate)
            return candidate


def reserve_destination(dest: Path) -> Path:
    """Atomically create an empty placeholder with a free name and return it.

    O_EXCL makes "check the name is free" and "claim it" one operation, so a
    file that appears at the same moment can never be overwritten.
    """
    for candidate in candidate_names(dest):
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return candidate
        except FileExistsError:
            continue


def describe_error(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "file disappeared before it could be moved"
    return f"{type(exc).__name__}: {exc.strerror or exc}"


# Scanner
def _is_project_folder(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def scan_files(root: Path, recursive: bool, report: Report):
    """Yield every file entry that is a candidate for moving (uses os.scandir).

    Records skip reasons and unreadable folders in `report`.
    """
    category_dirs = set(CATEGORIES) | {UNKNOWN_CATEGORY}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as exc:
            report.errors.append((str(current), f"cannot read folder: {describe_error(exc)}"))
            log.warning("ERROR  cannot read folder %s (%s)", current, describe_error(exc))
            continue

        for entry in entries:
            try:
                if entry.name == STATE_DIR and current == root:
                    continue                      # our own log / undo records: not "hidden clutter"
                if entry.name.startswith("."):
                    report.skipped["hidden"] += 1
                elif entry.is_symlink():
                    report.skipped["symlink"] += 1
                elif entry.is_dir(follow_symlinks=False):
                    nested_ok = (
                        recursive
                        and not (current == root and entry.name in category_dirs)
                        and entry.name not in IGNORED_FOLDERS
                        and not _is_project_folder(entry.path)
                    )
                    if nested_ok:
                        stack.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False):
                    report.skipped["not a regular file"] += 1
                elif entry.name == SELF.name and Path(entry.path).resolve() == SELF:
                    report.skipped["this script"] += 1
                elif Path(entry.name).suffix.lower() in PARTIAL_SUFFIXES:
                    report.skipped["in-progress download"] += 1
                else:
                    report.scanned += 1
                    yield entry
            except OSError as exc:
                report.errors.append((entry.path, describe_error(exc)))
                log.warning("ERROR  %s (%s)", entry.path, describe_error(exc))


# Organizer
def organize(root, *, dry_run: bool = False, recursive: bool = False,
             skip_unknown: bool = False) -> Report:
    """Sort the files in `root` into category folders and return a Report."""
    root = Path(root)
    report = Report(root=str(root), dry_run=dry_run)
    started = time.perf_counter()
    taken: set = set()

    for entry in scan_files(root, recursive, report):
        category = categorize(entry.name)
        if category == UNKNOWN_CATEGORY and skip_unknown:
            report.skipped["unknown type"] += 1
            continue

        src = Path(entry.path)
        dest_dir = root / category
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0

        if dry_run:
            dst = plan_destination(dest_dir / entry.name, taken)
            report.moves.append(Move(str(src), str(dst), category, size, dst.name != entry.name))
            log.info("WOULD MOVE  %s -> %s/%s", _rel(src, root), category, dst.name)
            continue

        dst = None
        try:
            if not dest_dir.exists():
                dest_dir.mkdir()
                report.created_dirs.append(str(dest_dir))
                log.info("CREATE  folder %s/", category)
            dst = reserve_destination(dest_dir / entry.name)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            reason = describe_error(exc)
            if dst is not None:                 # undo our placeholder / partial copy
                try:
                    dst.unlink()
                except OSError:
                    pass
            report.errors.append((str(src), reason))
            log.warning("ERROR  %s (%s)", _rel(src, root), reason)
            continue

        renamed = dst.name != entry.name
        report.moves.append(Move(str(src), str(dst), category, size, renamed))
        log.info("MOVE  %s -> %s/%s%s", _rel(src, root), category, dst.name,
                 "  (renamed: name already taken)" if renamed else "")

    report.seconds = time.perf_counter() - started
    if report.moves and not dry_run:
        report.manifest = _write_manifest(root, report)
    return report


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# Undo
def _write_manifest(root: Path, report: Report) -> str:
    """Save every move so the run can be reversed with --undo."""
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    path = state / f"moves_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
    path.write_text(json.dumps({
        "created": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "created_dirs": report.created_dirs,
        "moves": [{"src": m.src, "dst": m.dst} for m in report.moves],
    }, indent=2), encoding="utf-8")
    return str(path)


@dataclass
class UndoReport:
    restored: int = 0
    missing: int = 0
    errors: list = field(default_factory=list)
    manifest: str = ""


def undo_last(root) -> UndoReport | None:
    """Reverse the most recent organize run. Returns None if there is nothing to undo."""
    root = Path(root)
    manifests = sorted((root / STATE_DIR).glob("moves_*.json"))
    if not manifests:
        return None
    manifest = manifests[-1]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    result = UndoReport(manifest=str(manifest))

    for move in reversed(data["moves"]):
        src, dst = Path(move["src"]), Path(move["dst"])
        if not dst.exists():
            result.missing += 1
            log.warning("MISSING  %s (moved or deleted since the run)", dst.name)
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            target = reserve_destination(src)   # never overwrite something now at src
            try:
                shutil.move(str(dst), str(target))
            except OSError:
                target.unlink(missing_ok=True)
                raise
        except OSError as exc:
            result.errors.append((str(dst), describe_error(exc)))
            log.warning("ERROR  cannot restore %s (%s)", dst.name, describe_error(exc))
            continue
        result.restored += 1
        log.info("RESTORE  %s/%s -> %s", dst.parent.name, dst.name, _rel(target, root))

    for folder in data.get("created_dirs", []):        # remove folders we created, if empty
        try:
            Path(folder).rmdir()
            log.info("REMOVE  empty folder %s/", Path(folder).name)
        except OSError:
            pass

    manifest.rename(manifest.with_suffix(".undone"))    # so it cannot be undone twice
    return result


# Reporting
def print_summary(report: Report, log_path: str = "") -> None:
    bar = "=" * 60
    title = "FILE ORGANIZER SUMMARY" + ("  (DRY RUN - nothing was moved)" if report.dry_run else "")
    moved_word = "Would move" if report.dry_run else "Files moved"
    print(f"\n{bar}\n {title}\n{bar}")
    print(f" Folder         : {report.root}")
    print(f" Files scanned  : {report.scanned}")
    print(f" {moved_word:<14} : {len(report.moves)}  ({human_size(report.bytes_moved)})")
    print(f" Renamed        : {report.renamed}  (name already taken)")
    skipped_total = sum(report.skipped.values())
    detail = ", ".join(f"{k} {v}" for k, v in sorted(report.skipped.items()))
    print(f" Skipped        : {skipped_total}" + (f"  ({detail})" if detail else ""))
    print(f" Errors         : {len(report.errors)}")
    if report.by_category:
        print("-" * 60)
        print(f" {'Category':<16}{'Files':>6}")
        for category, count in sorted(report.by_category.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f" {category:<16}{count:>6}")
    if report.errors:
        print("-" * 60)
        print(" Problems (these files were left where they were):")
        for path, reason in report.errors:
            print(f"   - {_rel(Path(path), Path(report.root))}: {reason}")
    print("-" * 60)
    print(f" Time: {report.seconds:.2f} s" + (f"   Log: {log_path}" if log_path else ""))
    if report.manifest:
        print(" Undo with:  python organizer.py \"%s\" --undo" % report.root)
    print(bar)


def print_undo_summary(result: UndoReport, root: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n UNDO SUMMARY\n{bar}")
    print(f" Folder   : {root}")
    print(f" Restored : {result.restored}")
    print(f" Missing  : {result.missing}  (already moved or deleted)")
    print(f" Errors   : {len(result.errors)}")
    for path, reason in result.errors:
        print(f"   - {path}: {reason}")
    print(bar)


# CLI
def close_logging() -> None:
    """Flush and close all handlers attached to the logger."""
    for h in list(log.handlers):
        h.close()
    log.handlers.clear()


def setup_logging(root: Path, quiet: bool, write_file: bool) -> str:
    """Console output plus (optionally) a timestamped log file. Returns the file path."""
    log.setLevel(logging.INFO)
    close_logging()
    log.propagate = False
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING if quiet else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    log_path = ""
    if write_file:
        try:
            state = root / STATE_DIR
            state.mkdir(exist_ok=True)
            log_path = str(state / "organizer.log")
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
            log.addHandler(handler)
        except OSError:
            log_path = ""
    return log_path


def validate_root(raw: str, force: bool) -> Path:
    """Resolve the target folder and refuse anything unsafe to organize."""
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Error: '{raw}' is not a folder.")
    if root == Path(root.anchor):
        raise SystemExit("Error: refusing to organize a filesystem root.")
    if root == Path.home().resolve() and not force:
        raise SystemExit("Error: refusing to organize your whole home folder. "
                         "Pick a sub-folder such as Downloads, or pass --force.")
    return root


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Organize a cluttered folder into category sub-folders.",
    )
    p.add_argument("folder", nargs="?", default=str(Path.home() / "Downloads"),
                   help="folder to organize (default: your Downloads folder)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="show what would happen without moving anything")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="also organize files inside sub-folders (moved to the top-level "
                        "category folders; sub-folder structure is not preserved)")
    p.add_argument("--skip-unknown", action="store_true",
                   help=f"leave files with unrecognised extensions in place "
                        f"instead of moving them to '{UNKNOWN_CATEGORY}'")
    p.add_argument("-u", "--undo", action="store_true",
                   help="reverse the most recent run in this folder")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print only warnings and the summary")
    p.add_argument("--force", action="store_true",
                   help="allow organizing your home folder itself")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = validate_root(args.folder, args.force)

    try:
        if args.undo:
            log_path = setup_logging(root, args.quiet, write_file=True)
            result = undo_last(root)
            if result is None:
                print(f"Nothing to undo in {root}.")
                return 0
            print_undo_summary(result, str(root))
            return 1 if result.errors else 0

        log_path = setup_logging(root, args.quiet, write_file=not args.dry_run)
        log.info("Organizing %s%s", root, "  [dry run]" if args.dry_run else "")
        report = organize(root, dry_run=args.dry_run, recursive=args.recursive,
                          skip_unknown=args.skip_unknown)
        print_summary(report, log_path)
        return 1 if report.errors else 0
    finally:
        close_logging()


if __name__ == "__main__":
    sys.exit(main())
