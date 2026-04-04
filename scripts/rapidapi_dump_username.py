"""Dump raw RapidAPI (instagram120) /posts JSON for one username.

Usage (repo root):
  python scripts/rapidapi_dump_username.py
  python scripts/rapidapi_dump_username.py mkc_skopje
  python scripts/rapidapi_dump_username.py mkc_skopje my_dump.json

Default username: mkc_skopje. Default output: ./rapidapi_dump_<username>.json

Env: RAPIDAPI_KEY — loads .env from repo root (and common back-end paths).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

RAPIDAPI_HOST = "instagram120.p.rapidapi.com"
POSTS_URL = f"https://{RAPIDAPI_HOST}/api/instagram/posts"

DEFAULT_USERNAME = "mkc_skopje"


def main() -> None:
    username = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USERNAME).strip()
    out_arg = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None

    for p in (
        ROOT / ".env",
        ROOT / "back-end" / "database-adding-content" / ".env",
        ROOT / "back-end" / "scraping" / ".env",
    ):
        if p.is_file():
            load_dotenv(p)
    load_dotenv()

    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        print("Set RAPIDAPI_KEY in .env or environment.", file=sys.stderr)
        sys.exit(1)

    r = requests.post(
        POSTS_URL,
        json={"username": username, "maxId": ""},
        headers={
            "Content-Type": "application/json",
            "x-rapidapi-host": RAPIDAPI_HOST,
            "x-rapidapi-key": api_key,
        },
        timeout=90,
    )
    print(f"HTTP {r.status_code}  username={username!r}", file=sys.stderr)
    if not r.ok:
        print(r.text[:2000], file=sys.stderr)
        r.raise_for_status()

    data = r.json()
    out_path = out_arg
    if out_path is None:
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-", ".") else "_" for ch in username)
        out_path = (Path.cwd() / f"rapidapi_dump_{safe}.json").resolve()
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
