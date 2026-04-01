"""
User-submitted Instagram username → scrape (RapidAPI + Cloudinary) → Gemini captions + bio check
→ merge catalog, sync DB, append scrape_usernames.txt.

Called from gallery_app POST /suggest. Requires RAPIDAPI_KEY, Cloudinary env, GEMINI_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BACKEND_DATA = ROOT / "back-end" / "data"
CATALOG_PATH = BACKEND_DATA / "cloudinary_catalog.json"
ENV_SHARED = ROOT / "back-end" / "database-adding-content" / ".env"
SCRAPING_DIR = ROOT / "back-end" / "scraping"
AI_DIR = ROOT / "back-end" / "AI-Summarization"


def _ensure_import_paths() -> None:
    for p in (SCRAPING_DIR, AI_DIR):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def client_ip_hash(remote_addr: str | None, x_forwarded_for: str | None) -> str:
    ip = ""
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    if not ip:
        ip = (remote_addr or "").strip()
    if not ip:
        return ""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def _load_env() -> None:
    if ENV_SHARED.is_file():
        load_dotenv(ENV_SHARED)
    load_dotenv(SCRAPING_DIR / ".env")
    load_dotenv(AI_DIR / ".env")
    load_dotenv(ROOT / ".env")


def process_user_suggestion(
    raw_username: str,
    ip_hash: str,
) -> dict:
    """
    Returns dict: ok (bool), message (str), username (str|None), did_block (bool).
    On rejected suggestion (not nightlife), IP is blocked via catalog_db.
    """
    from catalog_db import (
        suggest_submit_block_ip,
        suggest_submit_is_blocked,
        sync_from_json,
        username_in_catalog,
    )

    if not ip_hash:
        return {
            "ok": False,
            "message": "Не можеме да ја потврдиме сесијата. Обиди се повторно подоцна.",
            "username": None,
            "did_block": False,
        }

    if suggest_submit_is_blocked(ip_hash):
        return {
            "ok": False,
            "message": (
                "Не можеш да предложиш нов профил 24 часа откако претходната проверка не помина. "
                "Обиди се подоцна или контактирај нè преку Контакт."
            ),
            "username": None,
            "did_block": True,
        }

    _load_env()
    _ensure_import_paths()

    from analyze_captions_gemini import (  # type: ignore  # noqa: E402
        analyze_biography_nightlife,
        load_api_key,
        run as gemini_run,
    )
    from google import genai  # type: ignore  # noqa: E402
    from scrape_rapidapi_cloudinary import (  # type: ignore  # noqa: E402
        SCRAPE_USERNAMES_TXT,
        ScrapeRow,
        build_posts_flat,
        load_cloudinary,
        load_existing_catalog,
        load_scrape_rows,
        save_scrape_rows,
        scrape_user_to_cloudinary,
        slugify_username,
    )

    handle = slugify_username((raw_username or "").strip().lstrip("@"))
    handle = handle.lower()
    if not handle:
        return {
            "ok": False,
            "message": "Внеси валидно корисничко име (без @ е во ред).",
            "username": None,
            "did_block": False,
        }

    if username_in_catalog(handle):
        return {
            "ok": False,
            "message": f"@{handle} веќе е во каталогот — не треба повторно да се додава.",
            "username": handle,
            "did_block": False,
        }

    api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not api_key:
        return {
            "ok": False,
            "message": "Серверот нема конфигуриран RapidAPI клуч. Обиди се подоцна.",
            "username": handle,
            "did_block": False,
        }

    try:
        load_cloudinary()
    except SystemExit:
        return {
            "ok": False,
            "message": "Cloudinary не е конфигуриран на серверот.",
            "username": handle,
            "did_block": False,
        }

    try:
        block = scrape_user_to_cloudinary(handle, api_key)
    except Exception:
        return {
            "ok": False,
            "message": (
                "Не успеавме да го преземеме профилот (Instagram/RapidAPI). "
                "Провери го корисничкото име и обиди се подоцна."
            ),
            "username": handle,
            "did_block": False,
        }

    prof = block.get("profile") or {}
    canonical = slugify_username(str(prof.get("username") or handle)).lower()

    if username_in_catalog(canonical):
        return {
            "ok": False,
            "message": f"@{canonical} веќе е во каталогот — не треба повторно да се додава.",
            "username": canonical,
            "did_block": False,
        }

    catalog = load_existing_catalog(CATALOG_PATH)
    by_u = catalog.get("by_username") or {}
    if not isinstance(by_u, dict):
        by_u = {}
    by_u[canonical] = {
        "profile": prof,
        "posts": block["posts"],
        "last_scraped_at": block["last_scraped_at"],
    }
    catalog["by_username"] = by_u
    catalog["posts_flat"] = build_posts_flat(by_u)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    def _rollback_catalog_user() -> None:
        cf = load_existing_catalog(CATALOG_PATH)
        bu = dict(cf.get("by_username") or {})
        bu.pop(canonical, None)
        cf["by_username"] = bu
        cf["posts_flat"] = build_posts_flat(bu)
        with open(CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(cf, f, indent=2, ensure_ascii=False)
        sync_from_json()

    try:
        gkey = load_api_key()
        client = genai.Client(api_key=gkey)
    except ValueError as e:
        _rollback_catalog_user()
        return {
            "ok": False,
            "message": f"Gemini не е достапен на серверот: {e}",
            "username": canonical,
            "did_block": False,
        }

    try:
        gemini_run(
            CATALOG_PATH.resolve(),
            force=True,
            throttle_sec=0.25,
            username_filter=canonical,
        )
    except Exception:
        _rollback_catalog_user()
        return {
            "ok": False,
            "message": "Анализа на објавите не успеа. Обиди се подоцна или контактирај нè.",
            "username": canonical,
            "did_block": False,
        }

    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)
    block_data = (catalog.get("by_username") or {}).get(canonical) or {}
    posts = block_data.get("posts") or []

    nightlife_posts = 0
    for post in posts:
        ca = post.get("caption_analysis")
        if isinstance(ca, dict) and ca.get("listing_type") == "nightlife_event":
            nightlife_posts += 1

    bio = (
        str(prof.get("bio") or prof.get("biography") or "").strip()
    )

    if nightlife_posts == 0:
        try:
            bio_ok = analyze_biography_nightlife(client, bio)
        except Exception:
            bio_ok = False
        if not bio_ok:
            suggest_submit_block_ip(ip_hash)
            by_u = dict(catalog.get("by_username") or {})
            by_u.pop(canonical, None)
            catalog["by_username"] = by_u
            catalog["posts_flat"] = build_posts_flat(by_u)
            with open(CATALOG_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2, ensure_ascii=False)
            sync_from_json()
            return {
                "ok": False,
                "message": (
                    "Овој профил не изгледа како ноќен клуб/бар/настани — нема доволно докази "
                    "во објавите или во биографијата. Следните 24 часа не можеш да предложиш друг профил од оваа мрежа."
                ),
                "username": canonical,
                "did_block": True,
            }

    rows = load_scrape_rows(SCRAPE_USERNAMES_TXT)
    if not any(r.username.lower() == canonical.lower() for r in rows):
        rows.append(ScrapeRow(username=canonical))
        save_scrape_rows(SCRAPE_USERNAMES_TXT, rows)

    sync_from_json()

    return {
        "ok": True,
        "message": f"@{canonical} е додаден во NightLife Skopje. Ви благодариме!",
        "username": canonical,
        "did_block": False,
    }
