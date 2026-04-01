"""
Analyze Instagram captions with Gemini → structured JSON merged into cloudinary_catalog.json.

Output: each post may have ``caption_analysis`` (schema_version 2). The gallery hides posts
where ``listing_type`` is ``not_nightlife`` (food posts, generic promos, etc.).

Env: GEMINI_API_KEY — in back-end/database-adding-content/.env or environment.

Run (repo root):
  python back-end/AI-Summarization/analyze_captions_gemini.py
  python back-end/AI-Summarization/analyze_captions_gemini.py --force
  python back-end/AI-Summarization/analyze_captions_gemini.py -u bistro.komedija

Re-analyze posts that already have caption_analysis: use --force (otherwise they are skipped).
Limit to one account: ``-u`` / ``--username`` (case-insensitive).

--- Example (nightlife_event) ---

"caption_analysis": {
  "schema_version": 2,
  "ticket_price_mkd": 300,
  "ticket_price_raw": null,
  "performers": [
    {"name": "DJ Ficho", "role": "dj"},
    {"name": "DJ Guardian", "role": "dj"}
  ],
  "day_of_week": "saturday",
  "event_date": "2026-03-28",
  "start_time": "00:00",
  "end_time": null,
  "reservations_phone": "071317338",
  "reservations_has_info": true,
  "location": "PURE Club, Градски парк, Skopje",
  "listing_type": "nightlife_event",
  "not_nightlife_label": null
}

--- Example (not_nightlife — hidden on site) ---

"caption_analysis": {
  "schema_version": 2,
  "ticket_price_mkd": null,
  "ticket_price_raw": null,
  "performers": [],
  "day_of_week": null,
  "event_date": null,
  "start_time": null,
  "end_time": null,
  "reservations_phone": null,
  "reservations_has_info": null,
  "location": null,
  "listing_type": "not_nightlife",
  "not_nightlife_label": "food_menu"
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

AI_SUM_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = AI_SUM_DIR.parent
ENV_PATH = BACKEND_ROOT / "database-adding-content" / ".env"
DEFAULT_CATALOG = BACKEND_ROOT / "data" / "cloudinary_catalog.json"
# Override with env GEMINI_MODEL if needed (e.g. gemini-2.5-flash-preview).
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"


class Performer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Name as in caption; may include @handle")
    role: Literal["dj", "singer", "live_band", "artist", "mc", "unknown"] | None = Field(
        default=None,
        description="Best fit; unknown if unclear",
    )


class CaptionAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=2, description="Always 2")

    ticket_price_mkd: int | None = Field(
        default=None,
        description="Whole number denars if stated (e.g. 300 from '300 ден'); else null",
    )
    ticket_price_raw: str | None = Field(
        default=None,
        description="Verbatim price text if not a plain MKD integer",
    )

    performers: list[Performer] = Field(
        default_factory=list,
        description="DJs, singers, bands, featured artists",
    )

    day_of_week: str | None = Field(
        default=None,
        description="English lowercase: monday, tuesday, ... sunday",
    )
    event_date: str | None = Field(
        default=None,
        description=(
            "ISO date YYYY-MM-DD when inferable; anchor relative phrases to the post publication "
            "datetime given in the prompt (weekday-only, 'this Saturday', etc.)"
        ),
    )
    start_time: str | None = Field(
        default=None,
        description="24h HH:MM if stated",
    )
    end_time: str | None = Field(
        default=None,
        description="24h HH:MM if stated; else null",
    )

    reservations_phone: str | None = Field(
        default=None,
        description="Phone for reservations, digits/spaces; normalize spacing optional",
    )
    reservations_has_info: bool | None = Field(
        default=None,
        description="True if reservations mentioned without a clear phone",
    )

    location: str | None = Field(
        default=None,
        description="Venue name and area/city if stated",
    )

    listing_type: Literal["nightlife_event", "not_nightlife"] = Field(
        description=(
            "nightlife_event: club/party/concert/night out at a venue. "
            "not_nightlife: food/menu, memes, staff photos, unrelated branding, giveaways not tied to a night event"
        ),
    )
    not_nightlife_label: str | None = Field(
        default=None,
        description=(
            "If not_nightlife: short snake_case slug, e.g. food_menu, staff_photo, "
            "generic_promo, product_ad, unrelated"
        ),
    )


SYSTEM = """You extract structured nightlife/event data from Instagram captions.
Languages: Macedonian, English, or mixed. Reply as JSON matching the schema only.

Rules:
- Use null for missing fields. Never invent phone numbers, prices, or dates not implied by the text.
- ticket_price_mkd: integer denars only when a clear amount is given (e.g. 300 ден, 300mkd). Else null.
- ticket_price_raw: use when price is vague ('free', 'од 500', 'влез') and not a single integer MKD.
- performers: each person/act named for the night (DJ, singer, band). Role: dj | singer | live_band | artist | mc | unknown.
- day_of_week: english lowercase monday..sunday from caption (Петок→friday, Сабота→saturday).
- event_date: YYYY-MM-DD when the caption implies a date. If the user message includes "Instagram post published at", use that instant as the anchor: resolve "оваа сабота", "вечерва", "Петок" without year, "next weekend", etc. to the correct calendar date relative to that publication time (do not guess a random year). If still ambiguous, null.
- start_time / end_time: 24h HH:MM (00:00, 22:30). null if not stated.
- reservations_phone: digits from reservation lines. reservations_has_info true if they say 'резервации' etc. but no number.
- location: venue + neighborhood/city when stated.
- listing_type: nightlife_event for club nights, parties, live/DJ nights at a venue. not_nightlife for menus, coffee, unrelated ads, reposts with no event.
- not_nightlife_label: required when listing_type is not_nightlife (short snake_case reason); null for nightlife_event."""


class BioNightlifeAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_nightlife_venue: bool = Field(
        description=(
            "True if the biography describes a bar, nightclub, club, lounge, cabaret, "
            "or venue that hosts DJ/parties/night events. False for personal blogs, "
            "generic shops, food-only places with no events, unrelated brands."
        ),
    )


BIO_SYSTEM = """You classify Instagram profile biographies (Macedonian, English, or mixed).
Reply as JSON only. Decide if the bio clearly indicates a nightlife-oriented venue or business
where people go out at night (club, bar, lounge, cabaret, party venue with DJs/live music).
False for personal accounts, influencers, retail, cafés with no events, memes, or unrelated text."""


def load_api_key() -> str:
    """Raises ValueError if missing (safe when this module is imported from the web app)."""
    load_dotenv(ENV_PATH)
    load_dotenv(AI_SUM_DIR / ".env")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "Set GEMINI_API_KEY in environment or in "
            f"{ENV_PATH} (see https://ai.google.dev/gemini-api/docs/api-key)"
        )
    return key


_DAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


def _clean_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _digits_phone(s: str | None) -> str | None:
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    return d if len(d) >= 6 else _clean_str(s)


def normalize_analysis(d: dict) -> dict:
    lt = d.get("listing_type")
    if lt not in ("nightlife_event", "not_nightlife"):
        lt = "nightlife_event"

    day = _clean_str(d.get("day_of_week"))
    if day:
        day = day.lower()
        if day not in _DAYS:
            day = None

    performers_out: list[dict] = []
    for p in d.get("performers") or []:
        if not isinstance(p, dict):
            continue
        name = _clean_str(p.get("name"))
        if not name:
            continue
        role = p.get("role")
        if role not in ("dj", "singer", "live_band", "artist", "mc", "unknown", None):
            role = "unknown"
        performers_out.append({"name": name, "role": role})

    price = d.get("ticket_price_mkd")
    try:
        price_int = int(price) if price is not None else None
        if price_int is not None and price_int < 0:
            price_int = None
    except (TypeError, ValueError):
        price_int = None

    not_label = _clean_str(d.get("not_nightlife_label"))
    if lt == "nightlife_event":
        not_label = None
    elif lt == "not_nightlife" and not not_label:
        not_label = "unspecified"

    ev_date = _clean_str(d.get("event_date"))
    if ev_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", ev_date):
        ev_date = None

    def _normalize_hhmm(v) -> str | None:
        s = _clean_str(v)
        if not s:
            return None
        s = re.sub(r"\s*h\s*$", "", s, flags=re.I).strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None

    st = _normalize_hhmm(d.get("start_time"))
    et = _normalize_hhmm(d.get("end_time"))

    rhi = d.get("reservations_has_info")
    if isinstance(rhi, str):
        rhi = rhi.lower() in ("true", "1", "yes")
    elif rhi is not None and not isinstance(rhi, bool):
        rhi = None

    return {
        "schema_version": 2,
        "ticket_price_mkd": price_int,
        "ticket_price_raw": _clean_str(d.get("ticket_price_raw")),
        "performers": performers_out,
        "day_of_week": day,
        "event_date": ev_date,
        "start_time": st,
        "end_time": et,
        "reservations_phone": _digits_phone(_clean_str(d.get("reservations_phone"))),
        "reservations_has_info": rhi,
        "location": _clean_str(d.get("location")),
        "listing_type": lt,
        "not_nightlife_label": not_label,
    }


def empty_analysis_no_caption() -> dict:
    """No API call; keep posts visible until analyzed (listing_type null)."""
    return {
        "schema_version": 2,
        "ticket_price_mkd": None,
        "ticket_price_raw": None,
        "performers": [],
        "day_of_week": None,
        "event_date": None,
        "start_time": None,
        "end_time": None,
        "reservations_phone": None,
        "reservations_has_info": None,
        "location": None,
        "listing_type": None,
        "not_nightlife_label": None,
    }


def _first_photo_image_url(post: dict) -> str | None:
    """First gallery image URL (skip reel placeholders and video blobs)."""
    for m in post.get("media") or []:
        if m.get("placeholder_for_reel"):
            continue
        if m.get("resource_type") == "video":
            continue
        url = (m.get("secure_url") or "").strip()
        if url:
            return url
    return None


def _mime_for_image_url(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return ct
    u = url.lower()
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _fetch_image_bytes(url: str) -> tuple[bytes, str] | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NightLifeSkopje-Gemini/1.0"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
            if not data or len(data) > 20 * 1024 * 1024:
                return None
            mime = _mime_for_image_url(url, resp.headers.get_content_type())
            return data, mime
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _posted_at_context_block(timestamp: str | None) -> str:
    """Ground relative event dates in when the post was published (Instagram)."""
    if not timestamp or not str(timestamp).strip():
        return ""
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    cal = dt.date().isoformat()
    wk = dt.strftime("%A")
    return (
        f"Instagram post published at (ISO UTC): {dt.isoformat()}\n"
        f"Publication calendar date: {cal} ({wk}).\n"
        "Use this to choose event_date when the caption omits the year or only says a weekday / "
        "'this Saturday' / 'вечерва' / similar — pick the date that matches the caption relative "
        "to this publication time.\n\n"
    )


def analyze_caption(
    client: genai.Client,
    caption: str,
    posted_at_iso: str | None = None,
    media_post: dict | None = None,
) -> dict:
    if not caption or not caption.strip():
        return empty_analysis_no_caption()

    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    ctx = _posted_at_context_block(posted_at_iso)
    prompt = (
        f"{ctx}"
        f"Caption:\n\n{caption.strip()}\n\n"
        "Extract fields per schema. listing_type must be nightlife_event or not_nightlife."
    )

    parts: list[types.Part] = []
    # Reels (video posts): caption-only — same prompt. Photos: attach first image when fetch works.
    if media_post is not None and not media_post.get("is_video"):
        img_url = _first_photo_image_url(media_post)
        if img_url:
            fetched = _fetch_image_bytes(img_url)
            if fetched:
                raw_bytes, mime = fetched
                parts.append(
                    types.Part(
                        inline_data=types.Blob(mime_type=mime, data=raw_bytes)
                    )
                )
    parts.append(types.Part(text=prompt))
    contents = types.Content(role="user", parts=parts)

    resp = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_json_schema=CaptionAnalysis.model_json_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        raise RuntimeError("Empty model response")
    parsed = CaptionAnalysis.model_validate_json(raw)
    return normalize_analysis(parsed.model_dump())


def analyze_biography_nightlife(client: genai.Client, bio: str) -> bool:
    """Gemini: does this bio describe a nightlife venue? Empty bio → False."""
    text = (bio or "").strip()
    if not text:
        return False
    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    prompt = f"Instagram biography:\n\n{text}"
    contents = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )
    resp = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=BIO_SYSTEM,
            response_mime_type="application/json",
            response_json_schema=BioNightlifeAssessment.model_json_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        return False
    parsed = BioNightlifeAssessment.model_validate_json(raw)
    return bool(parsed.is_nightlife_venue)


def rebuild_posts_flat(catalog: dict) -> None:
    posts_flat = []
    for un, block in (catalog.get("by_username") or {}).items():
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
    catalog["posts_flat"] = posts_flat


def run(
    catalog_path: Path,
    force: bool,
    throttle_sec: float,
    username_filter: str | None = None,
) -> None:
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    with open(catalog_path, encoding="utf-8") as f:
        catalog = json.load(f)

    by_u = catalog.get("by_username") or {}
    if username_filter:
        want = username_filter.strip().lower()
        by_u = {k: v for k, v in by_u.items() if k.lower() == want}
        if not by_u:
            raise ValueError(
                f"No account {username_filter!r} in catalog (check spelling)."
            )
    client = genai.Client(api_key=load_api_key())

    total = 0
    done = 0
    skipped = 0
    errors = 0

    for username, block in by_u.items():
        for post in block.get("posts") or []:
            total += 1
            if not force and post.get("caption_analysis") is not None:
                skipped += 1
                continue
            cap = (post.get("caption") or "").strip()
            try:
                post["caption_analysis"] = analyze_caption(
                    client,
                    cap,
                    posted_at_iso=(post.get("timestamp") or None),
                    media_post=post,
                )
                post.pop("caption_analysis_error", None)
                done += 1
                print(f"OK @{username} post {post.get('post_index')}")
            except Exception as e:
                errors += 1
                post["caption_analysis"] = None
                post["caption_analysis_error"] = str(e)[:500]
                print(f"ERR @{username} post {post.get('post_index')}: {e}", file=sys.stderr)
            if throttle_sec > 0:
                time.sleep(throttle_sec)

    rebuild_posts_flat(catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    line = (
        f"\nWrote {catalog_path} | analyzed: {done} | skipped (had analysis): {skipped} | "
        f"errors: {errors} | posts seen: {total}"
    )
    if username_filter:
        line += f" | only @{username_filter}"
    print(line)


def main() -> None:
    p = argparse.ArgumentParser(description="Gemini caption analysis -> cloudinary_catalog.json")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"Catalog JSON path (default: {DEFAULT_CATALOG})",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-run Gemini for every post even if caption_analysis exists",
    )
    p.add_argument(
        "--throttle",
        type=float,
        default=0.25,
        help="Seconds to sleep between API calls (default 0.25)",
    )
    p.add_argument(
        "-u",
        "--username",
        metavar="NAME",
        help="Only analyze posts for this Instagram account (e.g. bistro.komedija)",
    )
    args = p.parse_args()
    try:
        run(
            args.output.resolve(),
            force=args.force,
            throttle_sec=args.throttle,
            username_filter=(args.username.strip() if args.username else None),
        )
    except (ValueError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
