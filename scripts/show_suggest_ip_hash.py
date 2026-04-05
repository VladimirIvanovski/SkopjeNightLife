"""
Print suggest ip_hash exactly like gallery_app (SHA256 of the client IP string).

No extra deps — same logic as suggest_pipeline.client_ip_hash.

Local Flask (no proxy): usually remote_addr is 127.0.0.1.
Railway / nginx: set --xff to what X-Forwarded-For would be (first IP is used).

  python scripts/show_suggest_ip_hash.py
  python scripts/show_suggest_ip_hash.py 192.168.1.10
  python scripts/show_suggest_ip_hash.py --xff "203.0.113.45, 10.0.0.1"
"""

from __future__ import annotations

import argparse
import hashlib


def client_ip_hash(remote_addr: str | None, x_forwarded_for: str | None) -> str:
    """Mirror of suggest_pipeline.client_ip_hash."""
    ip = ""
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    if not ip:
        ip = (remote_addr or "").strip()
    if not ip:
        return ""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description="Compute NightLife MK suggest ip_hash.")
    p.add_argument(
        "remote_addr",
        nargs="?",
        default="127.0.0.1",
        help="request.remote_addr (default: 127.0.0.1 for local dev)",
    )
    p.add_argument(
        "--xff",
        metavar="HEADER",
        help="X-Forwarded-For; first IP wins (same as gallery_app when header is set)",
    )
    args = p.parse_args()

    if args.xff:
        ip_used = args.xff.split(",")[0].strip()
        h = client_ip_hash(None, args.xff)
    else:
        ip_used = (args.remote_addr or "").strip()
        h = client_ip_hash(args.remote_addr, None)

    print("ip_used:", ip_used or "(empty → ip_hash would be empty)")
    print("ip_hash: ", h or "(empty)")


if __name__ == "__main__":
    main()
