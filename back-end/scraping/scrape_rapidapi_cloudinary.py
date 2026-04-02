"""
Scrape Instagram posts via RapidAPI (instagram120), upload photos to Cloudinary.
Reels/videos use the account profile photo as a placeholder image (same as photo slots).

Env:
  RAPIDAPI_KEY — RapidAPI key for instagram120.p.rapidapi.com
  Cloudinary: ../database-adding-content/.env (CLOUDINARY_URL or CLOUD_NAME + API_KEY + API_SECRET)

Targets & rescrape schedule: back-end/data/scrape_usernames.txt (usernames + last/next UTC datetimes).
Skip until next_rescrape_due unless --force.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv

SCRAPING_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRAPING_DIR.parent
ENV_PATH = BACKEND_ROOT / "database-adding-content" / ".env"
DEFAULT_CATALOG = BACKEND_ROOT / "data" / "cloudinary_catalog.json"
SCRAPE_USERNAMES_TXT = BACKEND_ROOT / "data" / "scrape_usernames.txt"

RAPIDAPI_HOST = "instagram120.p.rapidapi.com"
POSTS_URL = f"https://{RAPIDAPI_HOST}/api/instagram/posts"

POSTS_LIMIT = 5
SCRAPE_INTERVAL_DAYS = 7

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.instagram.com/",
}


def _apply_cloudinary_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme != "cloudinary" or not p.hostname:
        return False
    key, secret, name = p.username, p.password, p.hostname
    if not all([key, secret, name]):
        return False
    cloudinary.config(cloud_name=name, api_key=key, api_secret=secret)
    return True


def load_cloudinary() -> None:
    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH)
    else:
        load_dotenv(SCRAPING_DIR / ".env")
        load_dotenv()
    cloudinary.reset_config()
    cfg = cloudinary.config()
    if getattr(cfg, "api_key", None) and getattr(cfg, "api_secret", None):
        return
    url = os.environ.get("CLOUDINARY_URL")
    if url and _apply_cloudinary_url(url):
        return
    name = os.environ.get("CLOUDINARY_CLOUD_NAME") or os.environ.get("CLOUD_NAME")
    key = os.environ.get("CLOUDINARY_API_KEY") or os.environ.get("API_KEY")
    secret = os.environ.get("CLOUDINARY_API_SECRET") or os.environ.get("API_SECRET")
    if all([name, key, secret]):
        cloudinary.config(cloud_name=name, api_key=key, api_secret=secret)
        return
    print(
        "Missing Cloudinary config. Set CLOUDINARY_URL or CLOUD_NAME + API_KEY + API_SECRET "
        f"in {ENV_PATH}",
        file=sys.stderr,
    )
    sys.exit(1)


def slugify_username(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name.strip())


@dataclass
class ScrapeRow:
    username: str
    last_scraped_at: str | None = None
    next_rescrape_due_at: str | None = None


def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def _default_scrape_rows() -> list[ScrapeRow]:
    return [
        ScrapeRow("club.pure.skopje"),
        ScrapeRow("nastani_sk"),
        ScrapeRow("cabaretclique"),
        ScrapeRow("equilibriumdaynight"),
        ScrapeRow("viewcoffeebar"),
    ]


_SCRAPE_TXT_HEADER = """# NightLife Skopje — Instagram usernames whose posts are scraped and media saved to Cloudinary.
# One row per account. Columns (pipe-separated):
#   username | last_scraped_at_utc | next_rescrape_due_utc
# Leave last/next empty until the first successful scrape. The scraper updates these automatically.
# Rescrape: allowed when current UTC time is >= next_rescrape_due (or use --force).
#
"""


def load_scrape_rows(path: Path) -> list[ScrapeRow]:
    if not path.is_file():
        rows = _default_scrape_rows()
        save_scrape_rows(path, rows)
        return rows
    rows: list[ScrapeRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) == 1:
            rows.append(ScrapeRow(username=slugify_username(parts[0])))
        elif len(parts) == 2:
            rows.append(
                ScrapeRow(
                    username=slugify_username(parts[0]),
                    last_scraped_at=parts[1] or None,
                )
            )
        else:
            rows.append(
                ScrapeRow(
                    username=slugify_username(parts[0]),
                    last_scraped_at=parts[1] or None,
                    next_rescrape_due_at=parts[2] or None,
                )
            )
    return rows if rows else _default_scrape_rows()


def save_scrape_rows(path: Path, rows: list[ScrapeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_SCRAPE_TXT_HEADER.rstrip("\n")]
    for r in rows:
        last = (r.last_scraped_at or "").strip()
        nxt = (r.next_rescrape_due_at or "").strip()
        lines.append(f"{r.username} | {last} | {nxt}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def should_skip_row(row: ScrapeRow, force: bool) -> bool:
    if force:
        return False
    last = _parse_iso_dt(row.last_scraped_at)
    if last is None:
        return False
    nxt = _parse_iso_dt(row.next_rescrape_due_at)
    if nxt is None:
        nxt = last + timedelta(days=SCRAPE_INTERVAL_DAYS)
    return datetime.now(timezone.utc) < nxt


def _profile_from_rapidapi_result(res: dict, username: str, edges: list[dict]) -> dict:
    """Profile + avatar URL from the same RapidAPI /posts payload (user, profile, or first owner)."""
    user = res.get("user")
    if not user and isinstance(res.get("profile"), dict):
        user = res.get("profile")
    if not user and edges:
        user = (edges[0].get("node") or {}).get("owner")
    if not isinstance(user, dict):
        user = {}

    pic = (
        user.get("profile_pic_url_hd")
        or user.get("profile_pic_url")
        or user.get("profile_pic_url_https")
    )
    ef = user.get("edge_followed_by") or {}
    eg = user.get("edge_follow") or {}
    em = user.get("edge_owner_to_timeline_media") or {}
    return {
        "username": user.get("username") or username,
        "full_name": user.get("full_name") or "",
        "bio": user.get("biography") or user.get("bio") or "",
        "followers": ef.get("count") if isinstance(ef, dict) else user.get("follower_count"),
        "following": eg.get("count") if isinstance(eg, dict) else user.get("following_count"),
        "posts_count": em.get("count") if isinstance(em, dict) else user.get("media_count"),
        "is_verified": bool(user.get("is_verified")),
        "external_url": user.get("external_url") or "",
        "profile_pic_url": pic,
    }


def fetch_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=120)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  X Download failed: {e}")
        return None


def upload_bytes(data: bytes, username: str, public_id: str, resource_type: str) -> dict | None:
    try:
        buf = io.BytesIO(data)
        res = cloudinary.uploader.upload(
            buf,
            folder=f"nightlife/{username}",
            public_id=public_id,
            resource_type=resource_type,
            overwrite=True,
        )
        return {
            "public_id": res.get("public_id"),
            "secure_url": res.get("secure_url"),
            "resource_type": res.get("resource_type"),
            "width": res.get("width"),
            "height": res.get("height"),
        }
    except Exception as e:
        print(f"  X Cloudinary upload failed: {e}")
        return None


def fetch_posts_rapidapi(username: str, api_key: str) -> tuple[list[dict], dict]:
    """Single RapidAPI call: timeline edges + profile (incl. profile photo) from the same JSON."""
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
    r.raise_for_status()
    data = r.json()
    res = data.get("result") or data
    edges = (res.get("edges") or [])[:POSTS_LIMIT]
    profile_data = _profile_from_rapidapi_result(res, username, edges)
    print(f"RapidAPI: {len(edges)} post edges")
    return edges, profile_data


def caption_text(node: dict) -> str | None:
    cap = node.get("caption")
    if cap is None:
        return None
    if isinstance(cap, dict):
        return cap.get("text")
    if isinstance(cap, str):
        return cap
    return None


def first_image_url_from_sidecar(node: dict) -> str | None:
    edges = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    for e in edges:
        ch = e.get("node") or {}
        if ch.get("media_type") == 2:
            continue
        cands = ch.get("image_versions2", {}).get("candidates") or []
        if cands:
            return cands[0].get("url")
    return None


def photo_cdn_url(node: dict) -> str | None:
    """First display image URL for a non-reel post (carousel: first image child, else parent)."""
    mt = node.get("media_type", 1)
    if mt == 2:
        return None
    if mt == 8:
        u = first_image_url_from_sidecar(node)
        if u:
            return u
    cands = node.get("image_versions2", {}).get("candidates") or []
    if cands:
        return cands[0].get("url")
    return None


def ts_iso(node: dict) -> str | None:
    taken = node.get("taken_at")
    if not taken:
        return None
    try:
        return datetime.fromtimestamp(int(taken), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def scrape_user_to_cloudinary(username: str, api_key: str) -> dict:
    edges, profile_data = fetch_posts_rapidapi(username, api_key)
    profile_pic = (profile_data or {}).get("profile_pic_url")

    posts_out: list[dict] = []
    for i, edge in enumerate(edges):
        node = edge.get("node") or {}
        idx = i + 1
        code = node.get("code") or ""
        ig_url = f"https://www.instagram.com/p/{code}/" if code else None
        cap = caption_text(node)
        ts = ts_iso(node)

        mt = node.get("media_type", 1)
        is_reel = mt == 2
        if is_reel:
            public_id = f"profile_photo_{idx}"
            src = profile_pic
            label = f"post {idx} (reel -> profile photo placeholder)"
        else:
            cdn_url = photo_cdn_url(node)
            public_id = f"photo_{idx}"
            src = cdn_url
            label = f"post {idx} (photo)"

        post = {
            "post_index": idx,
            "caption": cap,
            "timestamp": ts,
            "instagram_url": ig_url,
            "is_video": bool(is_reel),
            "media": [],
        }

        if not src:
            print(f"  X {label}: no image source")
            posts_out.append(post)
            continue

        print(f"Fetch + upload {label}...")
        raw = fetch_bytes(src)
        if not raw:
            posts_out.append(post)
            continue
        up = upload_bytes(raw, username, public_id, "image")
        if up:
            up["slide_index"] = 1
            if is_reel:
                up["placeholder_for_reel"] = True
            post["media"].append(up)
            print(f"  OK {up.get('secure_url', '')[:72]}...")

        posts_out.append(post)

    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "username": username,
        "profile": profile_data,
        "posts": posts_out,
        "last_scraped_at": now_iso,
    }


def load_existing_catalog(path: Path) -> dict:
    if not path.is_file():
        return {"by_username": {}, "posts_flat": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("by_username"), dict):
            return {"by_username": {}, "posts_flat": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"by_username": {}, "posts_flat": []}


def build_posts_flat(by_username: dict) -> list[dict]:
    posts_flat: list[dict] = []
    for un in sorted(by_username.keys()):
        block = by_username[un]
        for p in block.get("posts") or []:
            posts_flat.append(
                {
                    "username": un,
                    "post_index": p.get("post_index"),
                    "caption": p.get("caption"),
                    "timestamp": p.get("timestamp"),
                    "instagram_url": p.get("instagram_url"),
                    "is_video": p.get("is_video"),
                    "media": p.get("media") or [],
                    "caption_analysis": p.get("caption_analysis"),
                }
            )
    return posts_flat


def main() -> None:
    p = argparse.ArgumentParser(description="RapidAPI Instagram -> Cloudinary catalog.")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"Catalog JSON (default: {DEFAULT_CATALOG})",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Ignore next_rescrape_due and re-scrape all listed users.",
    )
    p.add_argument(
        "-l",
        "--usernames-file",
        type=Path,
        default=SCRAPE_USERNAMES_TXT,
        help=f"TXT list of usernames + scrape times (default: {SCRAPE_USERNAMES_TXT})",
    )
    args = p.parse_args()
    out = args.output.resolve()
    list_path = args.usernames_file.resolve()

    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH)
    else:
        load_dotenv(SCRAPING_DIR / ".env")
        load_dotenv()

    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        print("Set RAPIDAPI_KEY in environment or .env", file=sys.stderr)
        sys.exit(1)

    rows = load_scrape_rows(list_path)

    catalog = load_existing_catalog(out)
    by_u = catalog.get("by_username") or {}
    if not isinstance(by_u, dict):
        by_u = {}

    load_cloudinary()

    scraped = 0
    skipped = 0

    for row in rows:
        username = row.username
        print("\n" + "=" * 60)
        print(f"USER: {username}")
        print("=" * 60)
        if should_skip_row(row, args.force):
            last = _parse_iso_dt(row.last_scraped_at)
            nxt = _parse_iso_dt(row.next_rescrape_due_at)
            if nxt is None and last:
                nxt = last + timedelta(days=SCRAPE_INTERVAL_DAYS)
            due_s = nxt.isoformat() if nxt else "?"
            print(f"Skip until {due_s} (UTC). Use --force to refresh.")
            skipped += 1
            continue

        block = scrape_user_to_cloudinary(username, api_key)
        prof = block.get("profile")
        if isinstance(prof, dict):
            prof = dict(prof)
            prof["display_handle"] = username
            block["profile"] = prof
        by_u[username] = {
            "profile": block["profile"],
            "posts": block["posts"],
            "last_scraped_at": block["last_scraped_at"],
        }
        now = datetime.now(timezone.utc)
        row.last_scraped_at = block["last_scraped_at"]
        row.next_rescrape_due_at = (now + timedelta(days=SCRAPE_INTERVAL_DAYS)).isoformat()
        scraped += 1

    save_scrape_rows(list_path, rows)

    catalog["by_username"] = by_u
    catalog["posts_flat"] = build_posts_flat(by_u)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Catalog: {out}")
    print(f"Usernames list: {list_path}")
    print(f"Scraped: {scraped}  |  Skipped (not due yet): {skipped}")


if __name__ == "__main__":
    main()
