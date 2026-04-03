"""
Weekly refresh runner (Railway Cron friendly).

Goal: rescrape every account in Postgres, re-run Gemini on all posts, sync DB.

Pipeline:
  1) RapidAPI + Cloudinary: --force --from-db (all rows in `accounts`)
  2) Gemini: --force --mk-only (re-analyze posts + North Macedonia filter + sync to DB)
  3) Extra sync: `python catalog_db.py` (safety net; step 2 already calls sync_from_json)

Env (Railway / cron):
  DATABASE_URL or DATABASE_PUBLIC_URL — same Postgres as the app
  RAPIDAPI_KEY, GEMINI_API_KEY, Cloudinary vars

Optional:
  FORCE_GEMINI=0 — omit --force on Gemini (only fill missing caption_analysis); default is full re-run
  MK_FILTER=0 — omit --mk-only (do not run Macedonia account filter on weekly run)

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


def _env_for_subprocess() -> dict[str, str]:
    env = os.environ.copy()
    root = str(ROOT)
    existing = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"
    return env


def _truthy(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _run(args: list[str]) -> None:
    subprocess.run(
        args,
        check=True,
        cwd=str(ROOT),
        env=_env_for_subprocess(),
    )


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

        # 1) Force rescrape all accounts from Postgres
        _run(
            [
                py,
                str(ROOT / "back-end" / "scraping" / "scrape_rapidapi_cloudinary.py"),
                "--force",
                "--from-db",
            ]
        )

        # 2) Gemini: full re-analysis by default; optional Macedonia filter
        gem_args = [
            py,
            str(ROOT / "back-end" / "AI-Summarization" / "analyze_captions_gemini.py"),
        ]
        if _truthy("FORCE_GEMINI", default=True):
            gem_args.append("--force")
        if _truthy("MK_FILTER", default=True):
            gem_args.append("--mk-only")
        _run(gem_args)

        # 3) Sync DB from JSON (redundant if Gemini ran global sync; keeps cron idempotent)
        _run([py, str(ROOT / "catalog_db.py")])
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
