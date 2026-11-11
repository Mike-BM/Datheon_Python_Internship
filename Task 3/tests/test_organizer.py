"""Tests for organizer.py. Run with:  python -m pytest -v"""

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import organizer as org  # noqa: E402


# Helpers
@pytest.fixture(autouse=True)
def close_log_handlers():
    yield
    for handler in list(org.log.handlers):
        handler.close()
        org.log.removeHandler(handler)


def make(root: Path, tree: dict) -> Path:
    """Create files from {relative path: content}."""
    for rel, content in tree.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return root


def snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts[0] == org.STATE_DIR:
            continue
        rel_key = rel.as_posix()
        if p.is_symlink():
            snap[rel_key] = ("->", os.readlink(p))
        elif p.is_dir():
            snap[rel_key + "/"] = None
        else:
            snap[rel_key] = p.read_bytes()
    return snap


def files_only(root: Path) -> dict:
    return {k: v for k, v in snapshot(root).items() if not k.endswith("/")}


MESSY = {
    "report.pdf": "pdf-1", "notes.txt": "n", "photo.JPG": "jpg", "pic.png": "png",
    "movie.mp4": "mp4", "backup.zip": "zip", "script.py": "py", "song.mp3": "mp3",
    "setup.exe": "exe", "weird.xyz": "?", "README": "no extension",
}


# Categorization
def test_core_extension_mappings():
    assert org.EXTENSION_MAP[".pdf"] == "Documents"
    assert org.EXTENSION_MAP[".jpg"] == "Images"
    assert org.EXTENSION_MAP[".png"] == "Images"
    assert org.EXTENSION_MAP[".zip"] == "Archives"
    assert {"Documents", "Images", "Videos", "Archives", "Code"} <= set(org.CATEGORIES)


def test_no_extension_is_in_two_categories():
    assert len(org.EXTENSION_MAP) == sum(len(v) for v in org.CATEGORIES.values())


@pytest.mark.parametrize("name, expected", [
    ("a.pdf", "Documents"), ("A.PDF", "Documents"), ("photo.JpG", "Images"),
    ("movie.MKV", "Videos"), ("x.tar.gz", "Archives"), ("main.py", "Code"),
    ("README", "Others"), ("file.unknownext", "Others"), (".bashrc", "Others"),
    ("archive.pdf.zip", "Archives"),
])
def test_categorize(name, expected):
    assert org.categorize(name) == expected


@pytest.mark.parametrize("size, text", [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"),
                                        (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.0 GB")])
def test_human_size(size, text):
    assert org.human_size(size) == text


# Organizing
def test_files_go_to_the_right_folders_with_content_intact(tmp_path):
    make(tmp_path, MESSY)
    report = org.organize(tmp_path)
    assert report.errors == [] and len(report.moves) == len(MESSY)
    expected = {
        "Documents/report.pdf": b"pdf-1", "Documents/notes.txt": b"n",
        "Images/photo.JPG": b"jpg", "Images/pic.png": b"png",
        "Videos/movie.mp4": b"mp4", "Archives/backup.zip": b"zip",
        "Code/script.py": b"py", "Audio/song.mp3": b"mp3",
        "Installers/setup.exe": b"exe", "Others/weird.xyz": b"?",
        "Others/README": b"no extension",
    }
    assert files_only(tmp_path) == expected


def test_category_folders_are_created_only_when_needed(tmp_path):
    make(tmp_path, {"a.pdf": "x", "b.txt": "y"})
    org.organize(tmp_path)
    folders = {p.name for p in tmp_path.iterdir() if p.is_dir() and p.name != org.STATE_DIR}
    assert folders == {"Documents"}


def test_second_run_is_a_no_op(tmp_path):
    make(tmp_path, MESSY)
    org.organize(tmp_path)
    before = snapshot(tmp_path)
    again = org.organize(tmp_path)
    assert again.moves == [] and again.scanned == 0
    assert snapshot(tmp_path) == before


def test_skip_unknown_leaves_unrecognised_files_in_place(tmp_path):
    make(tmp_path, {"a.pdf": "x", "weird.xyz": "?", "README": "r"})
    report = org.organize(tmp_path, skip_unknown=True)
    assert (tmp_path / "weird.xyz").exists() and (tmp_path / "README").exists()
    assert not (tmp_path / "Others").exists()
    assert report.skipped["unknown type"] == 2


# Collisions
def test_existing_file_is_never_overwritten(tmp_path):
    make(tmp_path, {"Documents/report.pdf": "OLD", "report.pdf": "NEW"})
    report = org.organize(tmp_path)
    assert (tmp_path / "Documents/report.pdf").read_bytes() == b"OLD"
    assert (tmp_path / "Documents/report (1).pdf").read_bytes() == b"NEW"
    assert report.renamed == 1


def test_repeated_collisions_count_upwards(tmp_path):
    make(tmp_path, {"Documents/a.pdf": "0", "Documents/a (1).pdf": "1", "a.pdf": "2"})
    org.organize(tmp_path)
    assert (tmp_path / "Documents/a (2).pdf").read_bytes() == b"2"


def test_same_name_from_two_subfolders_keeps_both(tmp_path):
    make(tmp_path, {"x/a.pdf": "from-x", "y/a.pdf": "from-y", "a.pdf": "top"})
    org.organize(tmp_path, recursive=True)
    contents = sorted(p.read_bytes() for p in (tmp_path / "Documents").iterdir())
    assert contents == [b"from-x", b"from-y", b"top"]


def test_reserve_destination_never_returns_a_taken_name(tmp_path):
    (tmp_path / "f.txt").write_text("a")
    first = org.reserve_destination(tmp_path / "f.txt")
    second = org.reserve_destination(tmp_path / "f.txt")
    assert {first.name, second.name} == {"f (1).txt", "f (2).txt"}
    assert (tmp_path / "f.txt").read_text() == "a"


def test_plan_destination_avoids_names_planned_earlier(tmp_path):
    taken = set()
    names = [org.plan_destination(tmp_path / "a.pdf", taken).name for _ in range(3)]
    assert names == ["a.pdf", "a (1).pdf", "a (2).pdf"]


# Dry run
def test_dry_run_changes_nothing(tmp_path):
    make(tmp_path, MESSY)
    before = snapshot(tmp_path)
    report = org.organize(tmp_path, dry_run=True)
    assert snapshot(tmp_path) == before
    assert not (tmp_path / org.STATE_DIR).exists()
    assert len(report.moves) == len(MESSY) and report.manifest == ""


def test_dry_run_plan_matches_the_real_run(tmp_path):
    make(tmp_path, {**MESSY, "Documents/report.pdf": "OLD", "x/notes.txt": "dup"})
    planned = org.organize(tmp_path, dry_run=True, recursive=True)
    actual = org.organize(tmp_path, recursive=True)
    assert [(m.src, m.dst) for m in planned.moves] == [(m.src, m.dst) for m in actual.moves]


# Skip rules
def test_hidden_symlink_partial_and_folders_are_left_alone(tmp_path):
    make(tmp_path, {".secret": "s", "video.mp4.crdownload": "p", "x.part": "p",
                    "real.pdf": "r", "folder/inside.pdf": "i"})
    has_symlink = True
    try:
        os.symlink("real.pdf", tmp_path / "link.pdf")
    except OSError:
        has_symlink = False
    report = org.organize(tmp_path)
    assert (tmp_path / ".secret").exists()
    assert (tmp_path / "video.mp4.crdownload").exists() and (tmp_path / "x.part").exists()
    if has_symlink:
        assert os.path.islink(tmp_path / "link.pdf")
        assert report.skipped == {"hidden": 1, "in-progress download": 2, "symlink": 1}
    else:
        assert report.skipped == {"hidden": 1, "in-progress download": 2}
    assert (tmp_path / "folder/inside.pdf").exists()          # not recursive by default
    assert [m.category for m in report.moves] == ["Documents"]


def test_state_folder_is_not_counted_as_hidden_clutter(tmp_path):
    make(tmp_path, {"a.pdf": "x"})
    (tmp_path / org.STATE_DIR).mkdir()
    assert org.organize(tmp_path).skipped["hidden"] == 0


def test_the_script_itself_is_never_moved(tmp_path, monkeypatch):
    me = make(tmp_path, {"organizer.py": "print('hi')"}) / "organizer.py"
    monkeypatch.setattr(org, "SELF", me.resolve())
    report = org.organize(tmp_path)
    assert me.exists() and report.skipped["this script"] == 1


# Recursion
def test_recursive_sorts_nested_files_but_respects_projects(tmp_path):
    make(tmp_path, {"old/a.pdf": "a", "old/deep/b.png": "b",
                    "proj/main.py": "code", "proj/.git/HEAD": "ref",
                    "node_modules/pkg/index.js": "js", "Documents/keep.pdf": "keep"})
    org.organize(tmp_path, recursive=True)
    assert (tmp_path / "Documents/a.pdf").exists()
    assert (tmp_path / "Images/b.png").exists()
    assert (tmp_path / "proj/main.py").exists()                 # git project untouched
    assert (tmp_path / "node_modules/pkg/index.js").exists()    # ignored folder untouched
    assert (tmp_path / "Documents/keep.pdf").read_bytes() == b"keep"


# Error handling
def test_permission_error_is_reported_and_the_run_continues(tmp_path, monkeypatch):
    make(tmp_path, {"locked.pdf": "L", "ok1.pdf": "1", "ok2.png": "2"})
    real_move = shutil.move

    def flaky(src, dst, *a, **k):
        if Path(src).name == "locked.pdf":
            raise PermissionError(13, "Permission denied")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "move", flaky)
    report = org.organize(tmp_path)
    assert [Path(p).name for p, _ in report.errors] == ["locked.pdf"]
    assert "permission denied" in report.errors[0][1]
    assert len(report.moves) == 2
    assert (tmp_path / "locked.pdf").read_bytes() == b"L"       # source untouched
    assert not (tmp_path / "Documents/locked.pdf").exists()     # no placeholder left behind


def test_vanished_file_is_reported(tmp_path, monkeypatch):
    make(tmp_path, {"gone.pdf": "g", "ok.pdf": "o"})
    real_move = shutil.move

    def vanish(src, dst, *a, **k):
        if Path(src).name == "gone.pdf":
            raise FileNotFoundError(2, "No such file")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "move", vanish)
    report = org.organize(tmp_path)
    assert "disappeared" in report.errors[0][1] and len(report.moves) == 1


def test_unreadable_subfolder_is_reported_and_others_still_sorted(tmp_path, monkeypatch):
    make(tmp_path, {"secret/a.pdf": "a", "open/b.pdf": "b", "top.pdf": "t"})
    real_scandir = os.scandir

    def guarded(path="."):
        if Path(path).name == "secret":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded)
    report = org.organize(tmp_path, recursive=True)
    assert any("cannot read folder" in reason for _, reason in report.errors)
    assert (tmp_path / "Documents/b.pdf").exists() and (tmp_path / "Documents/top.pdf").exists()
    assert (tmp_path / "secret/a.pdf").exists()


def test_other_os_errors_do_not_stop_the_run(tmp_path, monkeypatch):
    make(tmp_path, {"a.pdf": "a", "b.pdf": "b"})
    real_move = shutil.move

    def full_disk(src, dst, *a, **k):
        if Path(src).name == "a.pdf":
            raise OSError(28, "No space left on device")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "move", full_disk)
    report = org.organize(tmp_path)
    assert len(report.moves) == 1 and len(report.errors) == 1
    assert "No space left" in report.errors[0][1]


# Undo
def test_undo_restores_the_exact_original_state(tmp_path):
    make(tmp_path, {**MESSY, "old/deep/extra.pdf": "deep", "Documents/report.pdf": "OLD"})
    before = snapshot(tmp_path)
    org.organize(tmp_path, recursive=True)
    assert snapshot(tmp_path) != before
    result = org.undo_last(tmp_path)
    assert result.errors == [] and result.missing == 0
    assert snapshot(tmp_path) == before          # same names, same content, folders removed again


def test_undo_keeps_folders_that_existed_before(tmp_path):
    make(tmp_path, {"Documents/old.pdf": "o", "new.pdf": "n"})
    org.organize(tmp_path)
    org.undo_last(tmp_path)
    assert (tmp_path / "Documents/old.pdf").exists() and (tmp_path / "new.pdf").exists()


def test_undo_cannot_run_twice_and_reports_nothing_to_undo(tmp_path):
    make(tmp_path, {"a.pdf": "a"})
    assert org.undo_last(tmp_path) is None
    org.organize(tmp_path)
    assert org.undo_last(tmp_path) is not None
    assert org.undo_last(tmp_path) is None


def test_undo_undoes_runs_one_at_a_time_newest_first(tmp_path):
    make(tmp_path, {"a.pdf": "a"})
    org.organize(tmp_path)
    make(tmp_path, {"b.png": "b"})
    org.organize(tmp_path)
    org.undo_last(tmp_path)
    assert (tmp_path / "b.png").exists() and (tmp_path / "Documents/a.pdf").exists()
    org.undo_last(tmp_path)
    assert (tmp_path / "a.pdf").exists()


def test_undo_never_overwrites_a_file_that_took_the_old_place(tmp_path):
    make(tmp_path, {"a.pdf": "ORIGINAL"})
    org.organize(tmp_path)
    make(tmp_path, {"a.pdf": "NEWCOMER"})        # something new now sits at the old location
    org.undo_last(tmp_path)
    assert (tmp_path / "a.pdf").read_bytes() == b"NEWCOMER"
    assert (tmp_path / "a (1).pdf").read_bytes() == b"ORIGINAL"


def test_undo_copes_with_files_deleted_since(tmp_path):
    make(tmp_path, {"a.pdf": "a", "b.pdf": "b"})
    org.organize(tmp_path)
    (tmp_path / "Documents/a.pdf").unlink()
    result = org.undo_last(tmp_path)
    assert result.restored == 1 and result.missing == 1
    assert (tmp_path / "b.pdf").exists()


def test_no_manifest_when_nothing_was_moved(tmp_path):
    make(tmp_path, {".hidden": "h"})
    assert org.organize(tmp_path).manifest == ""
    assert not (tmp_path / org.STATE_DIR).exists()


# CLI
def test_main_success_prints_summary_and_returns_zero(tmp_path, capsys):
    make(tmp_path, MESSY)
    assert org.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "FILE ORGANIZER SUMMARY" in out
    assert f"Files moved    : {len(MESSY)}" in out
    assert "Documents" in out and "Undo with" in out
    assert (tmp_path / org.STATE_DIR / "organizer.log").stat().st_size > 0


def test_main_returns_one_when_a_file_fails(tmp_path, monkeypatch, capsys):
    make(tmp_path, {"a.pdf": "a"})
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert org.main([str(tmp_path)]) == 1
    assert "Problems" in capsys.readouterr().out


def test_main_dry_run_label_and_no_log_file(tmp_path, capsys):
    make(tmp_path, {"a.pdf": "a"})
    assert org.main([str(tmp_path), "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert (tmp_path / "a.pdf").exists() and not (tmp_path / org.STATE_DIR).exists()


def test_main_quiet_hides_per_file_lines(tmp_path, capsys):
    make(tmp_path, {"a.pdf": "a"})
    org.main([str(tmp_path), "-q"])
    out = capsys.readouterr().out
    assert "MOVE  a.pdf" not in out and "SUMMARY" in out


def test_main_undo_message_when_nothing_to_undo(tmp_path, capsys):
    assert org.main([str(tmp_path), "--undo"]) == 0
    assert "Nothing to undo" in capsys.readouterr().out


def test_main_undo_round_trip(tmp_path, capsys):
    make(tmp_path, MESSY)
    before = snapshot(tmp_path)
    org.main([str(tmp_path)])
    assert org.main([str(tmp_path), "--undo"]) == 0
    assert "UNDO SUMMARY" in capsys.readouterr().out
    assert snapshot(tmp_path) == before


def test_refuses_missing_folder_and_filesystem_root(tmp_path):
    with pytest.raises(SystemExit):
        org.validate_root(str(tmp_path / "does-not-exist"), force=False)
    with pytest.raises(SystemExit):
        org.validate_root(Path(tmp_path.anchor).as_posix(), force=True)


def test_refuses_home_folder_unless_forced(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(SystemExit):
        org.validate_root(str(tmp_path), force=False)
    assert org.validate_root(str(tmp_path), force=True) == tmp_path.resolve()
