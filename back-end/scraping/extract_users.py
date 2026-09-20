"""
Scrape Instagram (Apify), upload media to Cloudinary (no local copies), write catalog JSON to back-end/data/cloudinary_catalog.json.

Skips users already present in the output catalog (same -o path): no Apify run, no uploads, data reused.
Use --force to re-scrape everyone.

Env:
  APIFY_API_TOKEN — Apify token (required)
  Cloudinary: load from ../database-adding-content/.env (CLOUDINARY_URL or CLOUD_NAME + API_KEY + API_SECRET)
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader
import requests
from apify_client import ApifyClient
from dotenv import load_dotenv

SCRAPING_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRAPING_DIR.parent
ENV_PATH = BACKEND_ROOT / "database-adding-content" / ".env"

API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
client = ApifyClient(API_TOKEN)

USERNAMES = [
    "club.pure.skopje",
    "nastani_sk",
    "cabaretclique",
    "equilibriumdaynight",
    "viewcoffeebar"
]

# Only keep first 7 posts; videos/reels are excluded from uploads (we use profile photo instead).
POSTS_LIMIT = 7
INSTAGRAM_ACTOR = "shu8hvrXbJbY3Eb9W"
DEFAULT_CATALOG = BACKEND_ROOT / "data" / "cloudinary_catalog.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "X-IG-App-ID": "936619743392459",
}
DOWNLOAD_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
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
    # Load .env before Cloudinary reads os.environ (Config() ran at import with empty env).
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

    name = (
        os.environ.get("CLOUDINARY_CLOUD_NAME")
        or os.environ.get("CLOUD_NAME")
        or os.environ.get("cloud_name")
    )
    key = (
        os.environ.get("CLOUDINARY_API_KEY")
        or os.environ.get("API_KEY")
        or os.environ.get("cloud_api_key")
    )
    secret = (
        os.environ.get("CLOUDINARY_API_SECRET")
        or os.environ.get("API_SECRET")
        or os.environ.get("cloud_api_secret")
        or os.environ.get("cloud_api")
    )
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


def fetch_profile(username: str) -> dict:
    print("Fetching profile info...")
    r = requests.get(
        f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
        headers=HEADERS,
        timeout=30,
    )
    if r.status_code != 200:
        print(f"X Could not fetch profile: {r.status_code}")
        return {}
    user = r.json()["data"]["user"]
    data = {
        "username": user["username"],
        "full_name": user["full_name"],
        "bio": user["biography"],
        "followers": user["edge_followed_by"]["count"],
        "following": user["edge_follow"]["count"],
        "posts_count": user["edge_owner_to_timeline_media"]["count"],
        "is_verified": user["is_verified"],
        "external_url": user["external_url"],
        "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
    }
    print(f"OK Profile: {data['full_name']} ({data['followers']} followers)")
    return data


def iter_media_slides_images_only(item: dict):
    children = item.get("childPosts") or []
    if children:
        for ch in children:
            d = ch.get("displayUrl") or ch.get("display_url")
            if d:
                yield d, "jpg"
        return

    imgs = item.get("images") or []
    if imgs:
        for url in imgs:
            if url:
                yield url, "jpg"
        return

    d = item.get("displayUrl") or item.get("display_url")
    if d:
        yield d, "jpg"


def post_meta(item: dict, index: int) -> dict:
    vurl = item.get("videoUrl") or item.get("video_url")
    typ = item.get("type")
    is_vid = item.get("isVideo")
    if is_vid is None:
        is_vid = bool(vurl) or (typ == "Video")
    return {
        "post_index": index,
        "id": item.get("id"),
        "short_code": item.get("shortCode"),
        "type": typ,
        "caption": item.get("caption"),
        "timestamp": item.get("timestamp"),
        "likes": item.get("likesCount"),
        "comments_count": item.get("commentsCount"),
        "location": item.get("locationName"),
        "instagram_url": item.get("url"),
        "hashtags": item.get("hashtags") or [],
        "mentions": item.get("mentions") or [],
        "owner_username": item.get("ownerUsername"),
        "owner_full_name": item.get("ownerFullName"),
        "owner_profile_pic_url": item.get("ownerProfilePicUrl") or item.get("owner_profile_pic_url"),
        "dimensions": {
            "width": item.get("dimensionsWidth"),
            "height": item.get("dimensionsHeight"),
        },
        "is_video": is_vid,
        "video_view_count": item.get("videoViewCount"),
        "video_duration_sec": item.get("videoDuration"),
        "alt": item.get("alt"),
        "media": [],
    }


def _is_video_item(item: dict) -> bool:
    vurl = item.get("videoUrl") or item.get("video_url")
    typ = item.get("type")
    is_vid = item.get("isVideo")
    if is_vid is None:
        is_vid = bool(vurl) or (typ == "Video")
    return bool(is_vid)


def fetch_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=120)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  X Download failed: {e}")
        return None


def upload_bytes(
    data: bytes, username: str, public_id: str, resource_type: str
) -> dict | None:
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
            "duration": res.get("duration"),
            "bytes": res.get("bytes"),
        }
    except Exception as e:
        print(f"  X Cloudinary upload failed: {e}")
        return None


def scrape_user_to_cloudinary(username: str) -> dict:
    profile_data = fetch_profile(username)

    print(f"\nScraping last {POSTS_LIMIT} posts via Apify ({username})...")
    run_input = {
        "directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "posts",
        "resultsLimit": POSTS_LIMIT,
        "addParentData": False,
    }
    run = client.actor(INSTAGRAM_ACTOR).call(run_input=run_input)

    posts_out = []
    for i, item in enumerate(client.dataset(run["defaultDatasetId"]).iterate_items()):
        post = post_meta(item, i + 1)
        if _is_video_item(item):
            # Exclude reels/videos: use the profile photo as a placeholder image if possible.
            pic = (
                (profile_data or {}).get("profile_pic_url")
                or post.get("owner_profile_pic_url")
                or (item.get("ownerProfilePicUrl") or item.get("owner_profile_pic_url"))
            )
            if pic:
                print(f"Fetch + upload post {i + 1} (video -> profile photo)...")
                raw = fetch_bytes(pic)
                if raw:
                    up = upload_bytes(raw, username, f"post_{i + 1}", "image")
                    if up:
                        up["slide_index"] = 1
                        post["media"].append(up)
            posts_out.append(post)
            continue

        slides = list(iter_media_slides_images_only(item))
        if not slides:
            print(f"Post {i + 1}: no image URLs in payload")
            posts_out.append(post)
            continue

        for sidx, (media_url, ext) in enumerate(slides, start=1):
            suffix = f"_{sidx}" if len(slides) > 1 else ""
            pid = f"post_{i + 1}{suffix}"
            rt = "image"
            kind = "image"
            label = f"post {i + 1} ({kind}"
            label += f" slide {sidx}" if len(slides) > 1 else ""
            label += ")"
            print(f"Fetch + upload {label}...")
            raw = fetch_bytes(media_url)
            if not raw:
                continue
            up = upload_bytes(raw, username, pid, rt)
            if up:
                up["slide_index"] = sidx
                post["media"].append(up)
                print(f"  OK {up.get('secure_url', '')[:64]}...")

        posts_out.append(post)

    return {"username": username, "profile": profile_data, "posts": posts_out}


def build_catalog(users_payload: list[dict]) -> dict:
    by_username = {u["username"]: {"profile": u["profile"], "posts": u["posts"]} for u in users_payload}
    posts_flat = []
    for u in users_payload:
        un = u["username"]
        for p in u["posts"]:
            row = {
                "username": un,
                "post_index": p["post_index"],
                "caption": p.get("caption"),
                "timestamp": p.get("timestamp"),
                "instagram_url": p.get("instagram_url"),
                "is_video": p.get("is_video"),
                "media": p.get("media") or [],
                "caption_analysis": p.get("caption_analysis"),
            }
            posts_flat.append(row)
    return {
        "by_username": by_username,
        "posts_flat": posts_flat,
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


def payload_from_stored(username: str, block: dict) -> dict:
    return {
        "username": username,
        "profile": block.get("profile") or {},
        "posts": block.get("posts") or [],
    }


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Scrape Instagram and upload media to Cloudinary.")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"Catalog JSON path (default: {DEFAULT_CATALOG})",
    )
    p.add_argument(
        "usernames",
        nargs="*",
        help=f"Instagram usernames (default: {USERNAMES})",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-scrape all users even if they already exist in the catalog file.",
    )
    args = p.parse_args()

    out = args.output.resolve()
    existing = load_existing_catalog(out)
    known = existing.get("by_username") or {}

    users = [slugify_username(u) for u in (args.usernames or USERNAMES)]

    need_scrape = [u for u in users if args.force or u not in known]
    if need_scrape:
        load_cloudinary()

    all_users = []
    skipped = 0
    for u in users:
        print("\n" + "=" * 60)
        print(f"USER: {u}")
        print("=" * 60)
        if not args.force and u in known:
            print(f"Skip (already in catalog): {u} — no Apify, no Cloudinary")
            all_users.append(payload_from_stored(u, known[u]))
            skipped += 1
            continue
        all_users.append(scrape_user_to_cloudinary(u))

    catalog = build_catalog(all_users)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Catalog: {out}")
    print(f"Skipped (already scraped): {skipped}  |  Fresh scrape: {len(users) - skipped}")
    print("Lookup: catalog['by_username'][username]['posts'][i]['caption'] and ['media'][*]['secure_url']")
    print("Or scan catalog['posts_flat'] for all captions + media.")


if __name__ == "__main__":
    main()
