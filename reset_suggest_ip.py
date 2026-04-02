"""
Reset suggest cooldown/limits for a given IP (for testing).

This deletes:
- suggest_submit_log rows for that ip_hash (quota window)
- suggest_jobs rows for that ip_hash (pending/running/done)
- suggest_submit_blocks row for that ip_hash (legacy)

Usage (PowerShell):
  python reset_suggest_ip.py 127.0.0.1

DATABASE_URL is read from the environment or project-root .env (see catalog_db).
"""

from __future__ import annotations

import hashlib
import sys

import psycopg

from catalog_db import DATABASE_URL


def ip_hash(ip: str) -> str:
    ip = (ip or "").strip()
    return hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else ""


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python reset_suggest_ip.py <ip>", file=sys.stderr)
        sys.exit(2)
    url = DATABASE_URL
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(2)
    h = ip_hash(sys.argv[1])
    if not h:
        print("Invalid IP", file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suggest_submit_log WHERE ip_hash = %s", (h,))
            cur.execute("DELETE FROM suggest_jobs WHERE ip_hash = %s", (h,))
            cur.execute("DELETE FROM suggest_submit_blocks WHERE ip_hash = %s", (h,))
        conn.commit()
    print("OK: reset for ip_hash", h[:10] + "…")


if __name__ == "__main__":
    main()

