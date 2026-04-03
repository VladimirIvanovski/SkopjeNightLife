"""
Print raw JSON from the same RapidAPI /posts call as scrape_rapidapi_cloudinary.py.

Usage (from repo root):
  python back-end/scraping/debug_rapidapi_posts.py
  python back-end/scraping/debug_rapidapi_posts.py club.pure.skopje
  python back-end/scraping/debug_rapidapi_posts.py club.pure.skopje rapidapi_dump.json

Env: RAPIDAPI_KEY (same as scraper; load from .env if present).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRAPING_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRAPING_DIR.parent
ENV_PATH = BACKEND_ROOT / "database-adding-content" / ".env"

RAPIDAPI_HOST = "instagram120.p.rapidapi.com"
POSTS_URL = f"https://{RAPIDAPI_HOST}/api/instagram/posts"

DEFAULT_USERNAME = "club.pure.skopje"


def main() -> None:
    username = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USERNAME).strip()
    out_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH)
    else:
        load_dotenv(SCRAPING_DIR / ".env")
        load_dotenv()

    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        print("Set RAPIDAPI_KEY in environment or .env", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": api_key,
    }
    r = requests.post(
        POSTS_URL,
        json={"username": username, "maxId": ""},
        headers=headers,
        timeout=90,
    )
    print(f"HTTP {r.status_code}  username={username!r}\n", file=sys.stderr)
    try:
        r.raise_for_status()
    except Exception as e:
        print(r.text[:2000], file=sys.stderr)
        raise e

    data = r.json()
    if out_path is None:
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in username)
        out_path = (Path.cwd() / f"rapidapi_dump_{safe}.json").resolve()
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
