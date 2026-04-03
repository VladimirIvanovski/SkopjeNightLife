"""
User-submitted Instagram username → scrape (RapidAPI + Cloudinary) → Postgres upsert
→ Gemini per-post captions (DB only) → North Macedonia check → nightlife accept / fallbacks.

Suggest flow does not read or write cloudinary_catalog.json; gallery reads from Postgres.

Called from gallery_app POST /suggest and worker_suggest. Requires RAPIDAPI_KEY, Cloudinary env, GEMINI_API_KEY, DATABASE_URL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_SHARED = ROOT / "back-end" / "database-adding-content" / ".env"
SCRAPING_DIR = ROOT / "back-end" / "scraping"
AI_DIR = ROOT / "back-end" / "AI-Summarization"


def _ensure_import_paths() -> None:
    for p in (SCRAPING_DIR, AI_DIR):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def normalize_suggest_username_input(s: str) -> str:
    """Strip leading @ (any number), NBSP; lowercase — IG handles are case-insensitive."""
    t = (s or "").replace("\u00a0", " ").strip()
    while t.startswith("@"):
        t = t[1:].lstrip()
    return t.lower().strip()


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
        delete_account_and_posts,
        fetch_posts_for_username,
        upsert_account_and_posts,
        username_in_catalog,
    )

    if not ip_hash:
        return {
            "ok": False,
            "message": "Не можеме да ја потврдиме сесијата. Обиди се повторно подоцна.",
            "username": None,
            "did_block": False,
        }

    _load_env()
    _ensure_import_paths()

    from analyze_captions_gemini import (  # type: ignore  # noqa: E402
        analyze_account_in_north_macedonia,
        analyze_profile_metadata_for_suggest,
        analyze_sample_captions_for_suggest,
        load_api_key,
        run_for_username_db,
    )
    from google import genai  # type: ignore  # noqa: E402
    from scrape_rapidapi_cloudinary import (  # type: ignore  # noqa: E402
        load_cloudinary,
        scrape_user_to_cloudinary,
        slugify_username,
    )

    handle = slugify_username(normalize_suggest_username_input(raw_username))
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

    _log.debug("scrape start handle=%r", handle)
    try:
        block = scrape_user_to_cloudinary(handle, api_key)
    except Exception:
        _log.exception("scrape failed handle=%r", handle)
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
    if not isinstance(prof, dict):
        prof = {}
    prof = dict(prof)
    prof["display_handle"] = handle
    canonical = slugify_username(str(prof.get("username") or handle)).lower()
    _log.debug("scrape ok canonical=%r (display_handle=%r)", canonical, handle)

    if username_in_catalog(canonical):
        return {
            "ok": False,
            "message": f"@{canonical} веќе е во каталогот — не треба повторно да се додава.",
            "username": canonical,
            "did_block": False,
        }

    try:
        upsert_account_and_posts(canonical, prof, block.get("posts") or [])
    except Exception:
        _log.exception("upsert_account_and_posts failed canonical=%r", canonical)
        return {
            "ok": False,
            "message": "Базата не е достапна (DATABASE_URL). Обиди се подоцна.",
            "username": canonical,
            "did_block": False,
        }

    def _rollback_db_user() -> None:
        try:
            delete_account_and_posts(canonical)
        except Exception:
            _log.exception("rollback delete_account_and_posts failed canonical=%r", canonical)

    try:
        gkey = load_api_key()
        client = genai.Client(api_key=gkey)
    except ValueError as e:
        _rollback_db_user()
        return {
            "ok": False,
            "message": f"Gemini не е достапен на серверот: {e}",
            "username": canonical,
            "did_block": False,
        }

    try:
        _log.debug("gemini DB run start filter=%r", canonical)
        run_for_username_db(canonical, force=True, throttle_sec=0.25)
        _log.debug("gemini DB run done filter=%r", canonical)
    except Exception:
        _log.exception("gemini run_for_username_db failed filter=%r", canonical)
        _rollback_db_user()
        return {
            "ok": False,
            "message": "Анализа на објавите не успеа. Обиди се подоцна или контактирај нè.",
            "username": canonical,
            "did_block": False,
        }

    posts = fetch_posts_for_username(canonical)

    def _recent_sample_captions(n: int = 3) -> list[str]:
        """Same ordering as weekly --mk-only: newest posts first."""
        out: list[str] = []
        for post in sorted(
            posts,
            key=lambda x: (x.get("timestamp") or "") or "",
            reverse=True,
        ):
            if not isinstance(post, dict):
                continue
            c = (post.get("caption") or "").strip()
            if c:
                out.append(c)
            if len(out) >= n:
                break
        return out

    SUGGEST_REJECT_NOT_MK = (
        "Профилот не изгледа локално за Скопје / Северна Македонија (или нема доволно докази во "
        "името или објавите). Следните 12 часа не можеш да предложиш друг профил."
    )
    sample_caps = _recent_sample_captions(3)
    try:
        is_mk = analyze_account_in_north_macedonia(
            client,
            username=canonical,
            full_name=str(prof.get("full_name") or "").strip() or None,
            sample_captions=sample_caps,
        )
    except Exception:
        _log.exception("analyze_account_in_north_macedonia failed canonical=%r", canonical)
        is_mk = False
    if not is_mk:
        _log.warning(
            "suggest rejected not_mk canonical=%r sample_caption_count=%s",
            canonical,
            len(sample_caps),
        )
        _rollback_db_user()
        return {
            "ok": False,
            "message": SUGGEST_REJECT_NOT_MK,
            "username": canonical,
            "did_block": False,
        }

    def _post_qualifies_for_suggest(ca: dict) -> bool:
        if ca.get("listing_type") == "nightlife_event":
            return True
        if ca.get("event_date") or ca.get("event_day"):
            return True
        perf = ca.get("performers") or []
        if isinstance(perf, list) and len(perf) > 0:
            return True
        return False

    nightlife_posts = 0
    for post in posts:
        ca = post.get("caption_analysis")
        if isinstance(ca, dict) and _post_qualifies_for_suggest(ca):
            nightlife_posts += 1

    SUGGEST_REJECT_MSG = (
        "Овој профил не одговара на критериумите за ноќен излегување / бар / кафана / настани "
        "(објави или профил). Следните 12 часа не можеш да предложиш друг профил."
    )

    if nightlife_posts == 0:
        accepted = False
        if sample_caps:
            try:
                accepted = analyze_sample_captions_for_suggest(client, sample_caps)
            except Exception:
                _log.exception("analyze_sample_captions_for_suggest failed canonical=%r", canonical)
                accepted = False
        if not accepted:
            try:
                accepted = analyze_profile_metadata_for_suggest(
                    client,
                    username=canonical,
                    full_name=str(prof.get("full_name") or "").strip() or None,
                )
            except Exception:
                _log.exception("analyze_profile_metadata_for_suggest failed canonical=%r", canonical)
                accepted = False
        if not accepted:
            _log.warning(
                "suggest rejected canonical=%r nightlife_posts=0 sample_caption_count=%s",
                canonical,
                len(sample_caps),
            )
            _rollback_db_user()
            return {
                "ok": False,
                "message": SUGGEST_REJECT_MSG,
                "username": canonical,
                "did_block": False,
            }

    _log.debug("suggest accept canonical=%r (Postgres only, no JSON sync)", canonical)
    return {
        "ok": True,
        "message": "Додадено.",
        "username": canonical,
        "did_block": False,
    }
