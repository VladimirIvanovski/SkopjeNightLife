"""Remove one account and all its posts from Postgres and cloudinary_catalog.json.

Usage (project root):
  .venv\\Scripts\\python scripts/remove_account.py full____circle
  .venv\\Scripts\\python scripts/remove_account.py --list-like full circle
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRAPING = ROOT / "back-end" / "scraping"
if str(SCRAPING) not in sys.path:
    sys.path.insert(0, str(SCRAPING))

from scrape_rapidapi_cloudinary import build_posts_flat  # noqa: E402

sys.path.insert(0, str(ROOT))
from catalog_db import CATALOG_JSON_PATH, SCRAPE_USERNAMES_PATH, get_connection  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Remove account + posts from DB and catalog JSON.")
    p.add_argument(
        "username",
        nargs="?",
        default="",
        help="Exact accounts.username to delete",
    )
    p.add_argument(
        "--list-like",
        metavar="SUBSTR",
        nargs="+",
        help="List usernames containing all given substrings (case-insensitive), then exit",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print only; do not delete",
    )
    p.add_argument(
        "--skip-json",
        action="store_true",
        help="Only delete from Postgres",
    )
    args = p.parse_args()

    if args.list_like:
        parts = [s.lower() for s in args.list_like if s.strip()]
        if not parts:
            sys.exit(2)
        with get_connection() as conn:
            rows = conn.execute("SELECT username FROM accounts ORDER BY username").fetchall()
        names = [r["username"] for r in rows]
        for u in names:
            low = u.lower()
            if all(p in low for p in parts):
                print(u)
        return

    un = (args.username or "").strip()
    if not un:
        p.error("pass username or use --list-like")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE username = %s LIMIT 1", (un,)
        ).fetchone()
        if not row:
            print(f"No account in DB: {un!r}", file=sys.stderr)
            print("Hint: .venv\\Scripts\\python scripts/remove_account.py --list-like full circle", file=sys.stderr)
            sys.exit(1)
        n_posts = conn.execute(
            "SELECT COUNT(*) AS c FROM posts WHERE username = %s", (un,)
        ).fetchone()
        pc = int(n_posts["c"]) if n_posts else 0
        print(f"Found {un!r} with {pc} post(s).")

    if args.dry_run:
        print("Dry run: no changes.")
        return

    with get_connection() as conn:
        conn.execute("DELETE FROM accounts WHERE username = %s", (un,))
        conn.commit()
    print("Deleted from Postgres (posts CASCADE).")

    if args.skip_json:
        return

    if not CATALOG_JSON_PATH.is_file():
        print(f"No {CATALOG_JSON_PATH}; skip JSON.")
        return

    with open(CATALOG_JSON_PATH, encoding="utf-8") as f:
        cat = json.load(f)
    by_u = cat.get("by_username") or {}
    if not isinstance(by_u, dict):
        by_u = {}
    if un not in by_u:
        print(f"Key {un!r} not in catalog JSON; JSON unchanged.")
        return
    del by_u[un]
    cat["by_username"] = by_u
    cat["posts_flat"] = build_posts_flat(by_u)

    with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
    print(f"Removed {un!r} from catalog JSON; rebuilt posts_flat.")

    if SCRAPE_USERNAMES_PATH.is_file():
        lines = SCRAPE_USERNAMES_PATH.read_text(encoding="utf-8").splitlines()
        out_lines: list[str] = []
        removed_txt = 0
        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                out_lines.append(line)
                continue
            first = raw.split("|")[0].strip()
            if first.lower() == un.lower():
                removed_txt += 1
                continue
            out_lines.append(line)
        if removed_txt:
            SCRAPE_USERNAMES_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            print(f"Removed {removed_txt} row(s) from scrape_usernames.txt.")


if __name__ == "__main__":
    main()
