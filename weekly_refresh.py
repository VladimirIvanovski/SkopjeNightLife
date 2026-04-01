"""
Weekly refresh runner (Railway Cron friendly).

Goal: every Sunday, rescrape ALL usernames and refresh the site data.

Pipeline:
  1) RapidAPI scrape + Cloudinary upload (FORCE all usernames)
  2) Gemini caption analysis (only missing by default; set FORCE_GEMINI=1 to re-analyze all)
  3) Sync SQLite DB from cloudinary_catalog.json

Run:
  python weekly_refresh.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "back-end" / "data"
LOCK_PATH = DATA_DIR / "weekly_refresh.lock"


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True)


def _acquire_lock() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        raise RuntimeError("Refresh already running (lock exists).")


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    _acquire_lock()
    try:
        py = sys.executable

        # 1) Force rescrape all usernames
        _run(
            [
                py,
                str(ROOT / "back-end" / "scraping" / "scrape_rapidapi_cloudinary.py"),
                "--force",
            ]
        )

        # 2) Gemini: analyze missing only (default) or force all if env says so
        gem_force = os.environ.get("FORCE_GEMINI", "").strip() in {"1", "true", "True", "YES", "yes"}
        gem_args = [
            py,
            str(ROOT / "back-end" / "AI-Summarization" / "analyze_captions_gemini.py"),
        ]
        if gem_force:
            gem_args.append("--force")
        _run(gem_args)

        # 3) Sync DB from JSON
        _run([py, str(ROOT / "catalog_db.py")])
    finally:
        _release_lock()


if __name__ == "__main__":
    main()

