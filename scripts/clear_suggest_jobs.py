"""Delete all rows from suggest_jobs (unblocks stuck pending UI).

Usage (project root, same DATABASE_URL as gallery + worker):
  .venv\\Scripts\\python scripts/clear_suggest_jobs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog_db import clear_all_suggest_jobs  # noqa: E402


def main() -> None:
    n = clear_all_suggest_jobs()
    print(f"Deleted {n} suggest job(s).")


if __name__ == "__main__":
    main()
