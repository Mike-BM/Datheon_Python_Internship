# File Organizer - Task 3

A Python CLI tool that sorts files in a folder (such as `Downloads`) into organized category subfolders based on file extensions.

```
Downloads/                         Downloads/
├── report.pdf                     ├── Documents/   report.pdf, notes.txt, budget.xlsx
├── photo.JPG                      ├── Images/      photo.JPG, vacation.png, logo.svg
├── movie.mp4          ──────►     ├── Videos/      movie.mp4, clip.mkv
├── backup.zip                     ├── Archives/    backup.zip, archive.tar.gz
├── script.py                      ├── Code/        script.py, index.html, config.json
└── setup.exe                      └── Installers/  setup.exe
```

## Quick Start

```bash
# Optional: generate a sample folder with test files
python make_demo.py

# Preview changes without moving anything
python organizer.py demo_folder --dry-run

# Run the organizer
python organizer.py demo_folder

# Undo the last organize run
python organizer.py demo_folder --undo
```

## Features

- **Extension-based categorization**: Sorts into `Documents`, `Images`, `Videos`, `Audio`, `Archives`, `Code`, `Installers`, and `Others`.
- **Collision safe**: Never overwrites existing files. If a filename is already taken, it appends a counter (e.g. `report (1).pdf`).
- **Preview & rollback**: Supports `--dry-run` to preview actions and `--undo` to restore files to their previous locations.
- **Skip protection**: Leaves hidden files (`.`), symlinks, active downloads (`.crdownload`, `.part`), and `.git` / `node_modules` untouched.
- **Fault tolerant**: Logs permission or disk errors and continues processing remaining files.

## Options

| Option | Description |
|---|---|
| `folder` | Target folder to organize (defaults to `~/Downloads`) |
| `-n`, `--dry-run` | Show planned moves without touching any files |
| `-r`, `--recursive` | Process nested folders and move files to top-level category dirs |
| `--skip-unknown` | Leave unrecognized extensions in place instead of moving to `Others` |
| `-u`, `--undo` | Revert the most recent run in the target folder |
| `-q`, `--quiet` | Output warnings and summary only |
| `--force` | Allow organizing the user home directory directly |

## Configuration

Category mappings are defined in the `CATEGORIES` dictionary in `organizer.py`:

```python
CATEGORIES = {
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".xlsx", ...},
    "Images":    {".jpg", ".jpeg", ".png", ".gif", ...},
    ...
}
```

New extensions or categories can be added directly to this dictionary.

## Running Tests

Install pytest and run:

```bash
pytest -v
```

The test suite covers file categorization, collision handling, dry runs, recursive scanning, permission error handling, and undo rollbacks.

## Project Structure

```
Task 3/
├── organizer.py          Main script
├── make_demo.py          Demo folder generator
├── README.md
└── tests/
    └── test_organizer.py Pytest test suite
```
