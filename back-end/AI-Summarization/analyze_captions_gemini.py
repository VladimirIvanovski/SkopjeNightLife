"""
Analyze Instagram captions with Gemini → structured JSON merged into cloudinary_catalog.json.

Output: each post may have ``caption_analysis`` (schema_version 3). The gallery hides posts
where ``listing_type`` is ``not_nightlife`` (food posts, generic promos, etc.).

Env: GEMINI_API_KEY — in back-end/database-adding-content/.env or environment.

Run (repo root):
  python back-end/AI-Summarization/analyze_captions_gemini.py
  python back-end/AI-Summarization/analyze_captions_gemini.py --force
  python back-end/AI-Summarization/analyze_captions_gemini.py -u bistro.komedija

With DATABASE_URL set, ``-u`` also upserts that account into Postgres (the site reads the DB, not only JSON).

Re-analyze posts that already have caption_analysis: use --force (otherwise they are skipped).
Limit to one account: ``-u`` / ``--username`` (case-insensitive).

--- Example (nightlife_event) ---

"caption_analysis": {
  "schema_version": 3,
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
  "reservations_url": null,
  "location": "PURE Club, Градски парк, Skopje",
  "city_mk": "Скопје",
  "venue_category": "nightclub",
  "listing_type": "nightlife_event",
  "not_nightlife_label": null
}

--- Example (not_nightlife — hidden on site) ---

"caption_analysis": {
  "schema_version": 3,
  "ticket_price_mkd": null,
  "ticket_price_raw": null,
  "performers": [],
  "day_of_week": null,
  "event_date": null,
  "start_time": null,
  "end_time": null,
  "reservations_phone": null,
  "reservations_has_info": null,
  "reservations_url": null,
  "location": null,
  "city_mk": null,
  "venue_category": "unknown",
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
from contextlib import closing
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from psycopg.errors import DeadlockDetected
from pydantic import BaseModel, ConfigDict, Field

# Venue type for the event (best-effort; one value per post).
VenueCategory = Literal[
    "nightclub",
    "bar_pub",
    "kafana",
    "cafe",
    "restaurant",
    "concert_venue",
    "lounge_rooftop",
    "festival_outdoor",
    "hotel_resort",
    "other_venue",
    "unknown",
]

_VALID_VENUE: frozenset[str] = frozenset(
    (
        "nightclub",
        "bar_pub",
        "kafana",
        "cafe",
        "restaurant",
        "concert_venue",
        "lounge_rooftop",
        "festival_outdoor",
        "hotel_resort",
        "other_venue",
        "unknown",
    )
)

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

    schema_version: int = Field(default=3, description="Always 3")

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
    reservations_url: str | None = Field(
        default=None,
        description=(
            "Single http(s) URL for online reservations/tickets/booking (Eventbrite, form, venue site). "
            "Never put phone numbers or tel: links here — use reservations_phone for phones. null if none."
        ),
    )

    location: str | None = Field(
        default=None,
        description="Venue name and area/city if stated",
    )
    city_mk: str | None = Field(
        default=None,
        description=(
            "City in North Macedonia (Cyrillic: Скопје, Битола, …). If location text includes a MK city in Latin or "
            "Cyrillic (e.g. 'Saloon, Skopje'), set the Cyrillic form. Required for nightlife_event in MK when "
            "location or caption names the city; avoid null then."
        ),
    )
    venue_category: VenueCategory | None = Field(
        default=None,
        description=(
            "Venue type: nightclub; bar_pub (saloon, pub, bar); kafana; cafe; restaurant; concert_venue; "
            "lounge_rooftop; festival_outdoor; hotel_resort; other_venue; unknown. For nightlife_event, prefer a "
            "specific category over unknown when the venue kind is inferable."
        ),
    )

    listing_type: Literal["nightlife_event", "not_nightlife"] = Field(
        description=(
            "nightlife_event: club/party/concert/night out at a venue; bar/pub evening, live music or DJ at a pub, "
            "themed night at the venue. "
            "not_nightlife: food/menu-only, memes, staff photos, unrelated branding, giveaways not tied to a night event"
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
- reservations_phone: extract phone for reservations same as before (digits). reservations_has_info true if they say 'резервации' etc. but no number. Do not duplicate a phone URL into reservations_url.
- reservations_url: only a full http:// or https:// link for booking, tickets, Google Form, Eventbrite, venue reservation page. null if no such link. Never use tel: or sms: here.
- location: venue + neighborhood/city when stated (free text).
- city_mk: municipality/city in North Macedonia (Cyrillic preferred: Скопје, Битола, Охрид, …). For listing_type nightlife_event inside MK: set this whenever the city is implied — e.g. location says "Saloon, Skopje" or "Venue, Скопје" → city_mk must be "Скопје" (not null). Same for other MK cities in Latin or Cyrillic in location/caption/hashtags. null only if clearly outside MK or no basis.
- venue_category: for nightlife_event at a real venue, pick the best enum (nightclub, bar_pub, kafana, …). Do not use unknown when the post is clearly a club night, bar gig, or venue party — infer from venue type words (saloon, pub, дискотека, кафана, MKC, …) and tone. unknown only when there is no reasonable guess.
- listing_type: nightlife_event for club nights, parties, live/DJ nights, pub/bar evenings, themed nights (e.g. St. Patrick's at the venue), quiz or live music at a bar/pub. not_nightlife for menus-only, coffee, unrelated ads, reposts with no event.
- not_nightlife_label: required when listing_type is not_nightlife (short snake_case reason); null for nightlife_event."""


class BioNightlifeAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_nightlife_venue: bool = Field(
        description=(
            "True if the biography describes a bar, pub, Irish pub, sports bar, nightclub, club, "
            "lounge, cabaret, or venue that hosts DJ nights, live music, parties, or evening events. "
            "False for personal blogs, generic shops, daytime-only cafés with no events, unrelated brands."
        ),
    )


BIO_SYSTEM = """You classify Instagram profile biographies (Macedonian, English, or mixed).
Reply as JSON only. Decide if the bio clearly indicates a venue where people go out in the evening
or at night: bar, pub, Irish pub, sports bar, nightclub, club, lounge, cabaret, or a place that hosts
DJs, live bands, parties, quiz/themed nights, St. Patrick's or similar celebrations at the venue.
True for pubs/bars even if they also serve food, when the account is clearly the venue (not a food blogger).
False for personal accounts, influencers, retail, pure takeaway/delivery with no venue, memes, or unrelated text."""


class SuggestCaptionsAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_nightlife_related: bool = Field(
        description=(
            "True if these captions (same account) indicate going out, social nights, or event-style "
            "promotion: nightclubs, bars, pubs, kafana (кафана), live music, DJs, parties, themed nights, "
            "concerts at a venue, festivals, ticketed events, evening reservations, random one-off events "
            "at a venue. False if only food menus, product ads, staff selfies, or unrelated content."
        ),
    )


SUGGEST_CAPTIONS_SYSTEM = """You receive up to 3 Instagram post captions from ONE account (Macedonian, English, or mixed).
Reply as JSON only.

Decide if the account fits a NIGHTLIFE / GOING-OUT / SOCIAL EVENTS scope for a city aggregator:
- Nightclubs, bars, pubs, Irish/sports pubs, lounges, kafana (traditional tavern with music/evenings)
- Venues posting about parties, live bands, DJs, themed nights, St. Patrick's / similar celebrations at the venue
- Promoters or venues posting ticketed events, weekend lineups, reservations for the evening
- Any clearly social or entertainment event where people gather (not private birthdays unless promoted as venue event)

Answer true when at least one caption clearly supports this. Answer false when captions are only daily menus,
coffee, generic product ads, memes, repost chains, or nothing suggests a venue or night out.

Be inclusive for bars/kafana/pubs that mix food with evening entertainment."""


PROFILE_METADATA_SUGGEST_SYSTEM = """No usable Instagram post captions were available (empty or missing). You only see:
the account username and optional profile display name (Macedonian, English, or mixed).

Reply as JSON only with the same schema. Decide if this account is LIKELY a going-out / nightlife-related venue:
bars, pubs, Irish/sports pubs, nightclubs, kafana, lounges, live music venues, or places that host evening events/parties.

True when username or full name clearly suggests such a venue (e.g. bar, pub, club, lounge, kafana, skopje, night).
True for plausible hospitality/venue names when text is short. False for personal blogs, retail shops, unrelated brands.
When uncertain between unrelated vs venue, prefer true if the handle/name hints at food+drink+evening establishment."""


class MacedoniaAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_in_north_macedonia: bool = Field(
        description=(
            "True if the Instagram account clearly represents a place, venue, promoter, or event "
            "series located in North Macedonia (e.g. Skopje, Ohrid, Bitola, Tetovo, Kumanovo). "
            "False if it is clearly about another country or has no strong evidence it is in North Macedonia."
        ),
    )


MACEDONIA_SYSTEM = """You decide if an Instagram account is in NORTH MACEDONIA.
Consider: biography, full name, city/country/location text, hashtags, and the language of the captions.

Rules:
- True ONLY when there is clear evidence that the venue/events are in North Macedonia or Macedonian cities
  (e.g. Skopje, Охрид / Ohrid, Bitola, Tetovo, Kumanovo, Prilep, Strumica, etc.).
- Strong signals: 'Skopje', 'Скопје', 'MKD', 'North Macedonia', Macedonian address/phone, Macedonian-only captions.
- False when the account is clearly for another country (e.g. 'Belgrade', 'Athens', 'Sofia', 'NYC') or generic/global.
- If you are uncertain or the text is very generic, answer false.

Reply as JSON only, matching the schema."""


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


def _normalize_reservations_url(s: str | None) -> str | None:
    u = _clean_str(s)
    if not u:
        return None
    low = u.lower()
    if low.startswith("tel:") or low.startswith("mailto:") or low.startswith("sms:"):
        return None
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    if len(u) > 2048:
        u = u[:2048]
    return u


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

    venue = _clean_str(d.get("venue_category"))
    if not venue or venue not in _VALID_VENUE:
        venue = "unknown"

    return {
        "schema_version": 3,
        "ticket_price_mkd": price_int,
        "ticket_price_raw": _clean_str(d.get("ticket_price_raw")),
        "performers": performers_out,
        "day_of_week": day,
        "event_date": ev_date,
        "start_time": st,
        "end_time": et,
        "reservations_phone": _digits_phone(_clean_str(d.get("reservations_phone"))),
        "reservations_has_info": rhi,
        "reservations_url": _normalize_reservations_url(_clean_str(d.get("reservations_url"))),
        "location": _clean_str(d.get("location")),
        "city_mk": _clean_str(d.get("city_mk")),
        "venue_category": venue,
        "listing_type": lt,
        "not_nightlife_label": not_label,
    }


def empty_analysis_no_caption() -> dict:
    """No API call; keep posts visible until analyzed (listing_type null)."""
    return {
        "schema_version": 3,
        "ticket_price_mkd": None,
        "ticket_price_raw": None,
        "performers": [],
        "day_of_week": None,
        "event_date": None,
        "start_time": None,
        "end_time": None,
        "reservations_phone": None,
        "reservations_has_info": None,
        "reservations_url": None,
        "location": None,
        "city_mk": None,
        "venue_category": "unknown",
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


def analyze_sample_captions_for_suggest(client: genai.Client, captions: list[str]) -> bool:
    """Gemini: from up to 3 post captions, does this account fit nightlife / social-events scope?"""
    caps = [str(c).strip() for c in (captions or []) if c and str(c).strip()]
    if not caps:
        return False
    caps = caps[:3]
    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    parts: list[str] = [
        "The following are Instagram post captions from the same account (newest samples first may be mixed order).",
        "",
    ]
    for i, c in enumerate(caps, 1):
        parts.append(f"--- Caption {i} ---\n{c}")
    prompt = "\n".join(parts)
    contents = types.Content(role="user", parts=[types.Part(text=prompt)])
    resp = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SUGGEST_CAPTIONS_SYSTEM,
            response_mime_type="application/json",
            response_json_schema=SuggestCaptionsAssessment.model_json_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        return False
    parsed = SuggestCaptionsAssessment.model_validate_json(raw)
    return bool(parsed.is_nightlife_related)


def analyze_profile_metadata_for_suggest(
    client: genai.Client,
    *,
    username: str,
    full_name: str | None,
) -> bool:
    """When post captions are missing/empty: judge from @handle and display name only."""
    fn = (full_name or "").strip()
    un = (username or "").strip().lstrip("@")
    if not un and not fn:
        return False
    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    lines = [
        f"Username: @{un}" if un else "Username: (missing)",
        f"Full name on profile: {fn or '(empty)'}",
    ]
    prompt = "\n".join(lines)
    contents = types.Content(role="user", parts=[types.Part(text=prompt)])
    resp = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=PROFILE_METADATA_SUGGEST_SYSTEM,
            response_mime_type="application/json",
            response_json_schema=SuggestCaptionsAssessment.model_json_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        return False
    parsed = SuggestCaptionsAssessment.model_validate_json(raw)
    return bool(parsed.is_nightlife_related)


def analyze_account_in_north_macedonia(
    client: genai.Client,
    *,
    username: str,
    full_name: str | None,
    sample_captions: list[str],
) -> bool:
    """Gemini: does this account clearly belong to North Macedonia?"""
    full_name = (full_name or "").strip()
    caps = [c.strip() for c in sample_captions if c and str(c).strip()]
    if not (full_name or caps):
        return False
    model_id = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    parts_text = [
        f"Username: @{username}",
        f"Full name: {full_name or '-'}",
    ]
    if caps:
        parts_text.append("Sample captions:")
        for c in caps[:3]:
            parts_text.append(f"- {c}")
    prompt = "\n".join(parts_text)
    contents = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )
    resp = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=MACEDONIA_SYSTEM,
            response_mime_type="application/json",
            response_json_schema=MacedoniaAssessment.model_json_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        return False
    parsed = MacedoniaAssessment.model_validate_json(raw)
    return bool(parsed.is_in_north_macedonia)


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
                    "gemini_caption_status": p.get("gemini_caption_status"),
                }
            )
    catalog["posts_flat"] = posts_flat


def _write_catalog_json(catalog_path: Path, catalog: dict) -> None:
    """Atomic write so you can open cloudinary_catalog.json mid-run and see latest posts."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    os.replace(tmp, catalog_path)


def _count_posts_to_analyze(by_u: dict, force: bool) -> int:
    n = 0
    for _, block in by_u.items():
        for post in block.get("posts") or []:
            if not force and post.get("caption_analysis") is not None:
                continue
            n += 1
    return n


def run(
    catalog_path: Path,
    force: bool,
    throttle_sec: float,
    username_filter: str | None = None,
    mk_only: bool = False,
) -> None:
    # So progress / [MK] lines show immediately in IDEs, Railway logs, and pipes (not only at exit).
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if not catalog_path.is_file():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    with open(catalog_path, encoding="utf-8") as f:
        catalog = json.load(f)

    if not mk_only:
        catalog.pop("mk_filter_log", None)

    by_u = catalog.get("by_username") or {}
    if username_filter:
        want = username_filter.strip().lower()
        by_u = {k: v for k, v in by_u.items() if k.lower() == want}
        if not by_u:
            raise ValueError(
                f"No account {username_filter!r} in catalog (check spelling)."
            )
    client = genai.Client(api_key=load_api_key())

    total_posts_all = sum(len(b.get("posts") or []) for b in by_u.values())
    total = _count_posts_to_analyze(by_u, force)
    done = 0
    skipped = 0
    errors = 0
    cur = 0

    if total > 0:
        print("Caption analysis (Gemini)…")
        if mk_only and not username_filter:
            print(
                f"  ({total} posts) MK ADDED/REMOVED lines run after captions finish — not during.",
                flush=True,
            )

    for username, block in by_u.items():
        for post in block.get("posts") or []:
            if not force and post.get("caption_analysis") is not None:
                post.setdefault("gemini_caption_status", "skipped")
                skipped += 1
                continue
            cap = (post.get("caption") or "").strip()
            cur += 1
            pct = 100.0 * cur / total if total else 100.0
            # One updating line; pad width so PowerShell / \\r does not leave junk on the line.
            msg = (
                f"Captions {pct:.1f}% ({cur}/{total}) @{username} "
                f"post {post.get('post_index')}"
            )
            print("\r" + msg.ljust(96), end="", flush=True)
            try:
                post["caption_analysis"] = analyze_caption(
                    client,
                    cap,
                    posted_at_iso=(post.get("timestamp") or None),
                    media_post=post,
                )
                post.pop("caption_analysis_error", None)
                post["gemini_caption_status"] = "ok"
                done += 1
            except Exception as e:
                errors += 1
                post["caption_analysis"] = None
                post["caption_analysis_error"] = str(e)[:500]
                post["gemini_caption_status"] = "error"
                print(
                    f"\nERR @{username} post {post.get('post_index')}: {e}",
                    file=sys.stderr,
                )
            rebuild_posts_flat(catalog)
            catalog["gemini_last_saved_at"] = datetime.now(timezone.utc).isoformat()
            _write_catalog_json(catalog_path, catalog)
            if throttle_sec > 0:
                time.sleep(throttle_sec)

    if total > 0:
        print()

    # Optionally drop accounts that are not clearly in North Macedonia.
    removed_accounts: list[str] = []
    if mk_only and username_filter:
        print(
            "Note: --mk-only is skipped when --username is set (run without -u for MK filter).",
            flush=True,
        )
    if mk_only and not username_filter:
        mk_list = list(by_u.items())
        mk_n = len(mk_list)
        if mk_n > 0:
            print("North Macedonia filter (Gemini)…", flush=True)
        catalog["mk_filter_log"] = []
        filtered_by_u: dict[str, dict] = {}
        if mk_n == 0:
            catalog.pop("mk_filter_log", None)
        for i, (username, block) in enumerate(mk_list, start=1):
            prof = (block.get("profile") or {}) if isinstance(block.get("profile"), dict) else {}
            full_name = prof.get("full_name") or prof.get("name")
            posts = block.get("posts") or []
            # Take up to 3 captions from recent posts.
            caps: list[str] = []
            for p in sorted(
                posts,
                key=lambda x: (x.get("timestamp") or "") or "",
                reverse=True,
            ):
                cap = (p.get("caption") or "").strip()
                if cap:
                    caps.append(cap)
                if len(caps) >= 3:
                    break
            try:
                keep = analyze_account_in_north_macedonia(
                    client,
                    username=username,
                    full_name=full_name,
                    sample_captions=caps,
                )
            except Exception:
                keep = False
            if keep:
                filtered_by_u[username] = block
            else:
                removed_accounts.append(username)
            pct_mk = 100.0 * i / mk_n if mk_n else 100.0
            result = "ADDED" if keep else "REMOVED"
            print(
                f"MK {pct_mk:.1f}% ({i}/{mk_n}) @{username} {result}",
                flush=True,
            )
            catalog["mk_filter_log"].append(
                {"username": username, "status": result}
            )
            catalog["gemini_last_saved_at"] = datetime.now(timezone.utc).isoformat()
            _write_catalog_json(catalog_path, catalog)
            if throttle_sec > 0:
                time.sleep(throttle_sec)
        if mk_n > 0:
            for _u, blk in filtered_by_u.items():
                blk["mk_filter_status"] = "ADDED"
            catalog["by_username"] = filtered_by_u

    rebuild_posts_flat(catalog)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    _write_catalog_json(catalog_path, catalog)

    # Sync to database (Railway/local). Full catalog replace only without -u; -u merges one account.
    if not username_filter:
        try:
            from catalog_db import sync_from_json
        except ImportError:
            pass
        else:
            sync_from_json(catalog)
    else:
        try:
            from catalog_db import DATABASE_URL, upsert_account_and_posts
        except ImportError:
            pass
        else:
            if DATABASE_URL:
                want = username_filter.strip().lower()
                full_by = catalog.get("by_username") or {}
                key: str | None = None
                block: dict | None = None
                for k, v in full_by.items():
                    if str(k).strip().lower() == want:
                        key, block = str(k), v if isinstance(v, dict) else None
                        break
                if key and block is not None:
                    upsert_account_and_posts(
                        key,
                        block.get("profile") or {},
                        block.get("posts") or [],
                    )
                    print(f"Synced @{key} -> Postgres (single account)", flush=True)

    line = (
        f"\nWrote {catalog_path} | analyzed: {done} | skipped (had analysis): {skipped} | "
        f"errors: {errors} | caption posts in catalog: {total_posts_all} | "
        f"Gemini calls: {total}"
    )
    if username_filter:
        line += f" | only @{username_filter}"
    if removed_accounts:
        line += f" | removed_non_mk_accounts: {', '.join(sorted(removed_accounts))}"
    print(line)


def run_for_username_db(
    username: str,
    force: bool,
    throttle_sec: float,
) -> None:
    """Gemini caption analysis for one user; read/write Postgres only (no cloudinary_catalog.json)."""
    from catalog_db import get_connection, upsert_post_row

    want = (username or "").strip()
    if not want:
        raise ValueError("empty username")
    client = genai.Client(api_key=load_api_key())
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT username FROM accounts WHERE LOWER(username) = LOWER(%s) LIMIT 1",
            (want,),
        ).fetchone()
        if not r:
            raise ValueError(f"No account {username!r} in database")
        un = r["username"]
        rows = conn.execute(
            "SELECT post_json FROM posts WHERE username = %s ORDER BY post_index NULLS LAST",
            (un,),
        ).fetchall()
        total = len(rows)
        done = 0
        for row in rows:
            pj = row["post_json"]
            if isinstance(pj, str):
                pj = json.loads(pj)
            if not isinstance(pj, dict):
                continue
            post = pj
            if not force and post.get("caption_analysis") is not None:
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
                post["gemini_caption_status"] = "ok"
                done += 1
            except Exception as e:
                post["caption_analysis"] = None
                post["caption_analysis_error"] = str(e)[:500]
                post["gemini_caption_status"] = "error"
                print(
                    f"\nERR @{un} post {post.get('post_index')}: {e}",
                    file=sys.stderr,
                )
            for attempt in range(8):
                try:
                    upsert_post_row(conn, un, post)
                    conn.commit()
                    break
                except DeadlockDetected:
                    conn.rollback()
                    if attempt >= 7:
                        raise
                    time.sleep(0.04 * (2 ** min(attempt, 5)))
            if throttle_sec > 0:
                time.sleep(throttle_sec)
    print(
        f"Caption analysis (Gemini, DB only) @{un} | posts={total} | analyzed={done}",
        flush=True,
    )


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
    p.add_argument(
        "--mk-only",
        action="store_true",
        help="After analysis, keep only accounts clearly in North Macedonia and sync DB.",
    )
    args = p.parse_args()
    try:
        run(
            args.output.resolve(),
            force=args.force,
            throttle_sec=args.throttle,
            username_filter=(args.username.strip() if args.username else None),
            mk_only=args.mk_only,
        )
    except (ValueError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
