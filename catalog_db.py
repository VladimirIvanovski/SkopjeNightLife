"""
Postgres store for NightLife catalog — filterable posts; full post blobs as JSONB.

URL date filters use caption_analysis.event_date (AI), not Instagram posted_at.

Sync from cloudinary_catalog.json:
  python catalog_db.py
Recompute visible flags from existing post_json (e.g. Railway DB after rule change):
  python catalog_db.py --refresh-visible
Recompute venue_category column (filters / old caption_analysis without venue_category):
  python catalog_db.py --refresh-venue
Dump Postgres back into cloudinary_catalog.json (backup; avoids losing DB-only users on sync):
  python catalog_db.py --export-json
  python catalog_db.py --export-json path/to/backup.json
Warning: plain ``sync_from_json`` TRUNCATES posts/accounts and reloads only from the JSON file.
If that file omits a user (e.g. skopje_event_center only on Railway), that user disappears from the DB.
Or: gallery_app can import JSON on first start when DB is empty.
"""

from __future__ import annotations

import json
import random
import re
import os
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.errors import DeadlockDetected
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "back-end" / "database-adding-content" / ".env")

CATALOG_JSON_PATH = ROOT / "back-end" / "data" / "cloudinary_catalog.json"
SCRAPE_USERNAMES_PATH = ROOT / "back-end" / "data" / "scrape_usernames.txt"

# Railway: private URL is DATABASE_URL; public (for local) is often DATABASE_PUBLIC_URL.
def _database_url_from_env() -> str:
    for key in ("DATABASE_URL", "DATABASE_PUBLIC_URL"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return ""


DATABASE_URL = _database_url_from_env()

# Suggest rules:
# - max 2 suggestions per 12 hours per IP hash
# - cannot suggest another while one is pending
SUGGEST_WINDOW = timedelta(hours=12)
SUGGEST_MAX_PER_WINDOW = 2
SUGGEST_GLOBAL_MONTHLY_LIMIT = 350

# Lowercase Instagram usernames hidden from feed, /api/events, user picker, and /u/<name> (404).
HIDDEN_FROM_FEED_USERNAMES: frozenset[str] = frozenset({"equilibriumdaynight"})

# Gemini caption_analysis.venue_category — URL filter ?venue= must be one of these (lowercase).
VENUE_CATEGORY_FILTER_VALUES: frozenset[str] = frozenset(
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  username TEXT PRIMARY KEY,
  profile_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS posts (
  id BIGSERIAL PRIMARY KEY,
  username TEXT NOT NULL REFERENCES accounts(username) ON DELETE CASCADE,
  post_index INTEGER,
  post_json JSONB NOT NULL,
  posted_at TIMESTAMPTZ,
  posted_date DATE,
  event_date DATE,
  event_date_end DATE,
  ticket_price_mkd NUMERIC,
  listing_type TEXT,
  performer_roles JSONB,
  visible BOOLEAN NOT NULL DEFAULT FALSE,
  venue_category TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_posts_user_idx ON posts(username, post_index);
CREATE INDEX IF NOT EXISTS idx_posts_username ON posts(username);
CREATE INDEX IF NOT EXISTS idx_posts_posted_date ON posts(posted_date);
CREATE INDEX IF NOT EXISTS idx_posts_visible ON posts(visible);
CREATE INDEX IF NOT EXISTS idx_posts_ticket ON posts(ticket_price_mkd);
CREATE INDEX IF NOT EXISTS idx_posts_event_date ON posts(event_date);

CREATE TABLE IF NOT EXISTS suggest_submit_blocks (
  ip_hash TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS suggest_submit_log (
  id BIGSERIAL PRIMARY KEY,
  ip_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggest_submit_log_ip ON suggest_submit_log(ip_hash);

CREATE TABLE IF NOT EXISTS suggest_jobs (
  id BIGSERIAL PRIMARY KEY,
  username_raw TEXT NOT NULL,
  ip_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  ok BOOLEAN,
  message TEXT,
  canonical_username TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggest_jobs_status ON suggest_jobs(status);
CREATE INDEX IF NOT EXISTS idx_suggest_jobs_ip ON suggest_jobs(ip_hash);

CREATE TABLE IF NOT EXISTS suggest_global_monthly (
  ym TEXT PRIMARY KEY, -- YYYY-MM
  used INTEGER NOT NULL DEFAULT 0
);
"""


def init_schema(conn) -> None:
    conn.execute(SCHEMA)
    conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS venue_category TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_venue_category ON posts(venue_category)"
    )


def _caption_analysis_dict(post: dict) -> dict | None:
    """caption_analysis is normally a dict; some DB round-trips store it as a JSON string."""
    ca = post.get("caption_analysis")
    if isinstance(ca, dict):
        return ca
    if isinstance(ca, str) and ca.strip():
        try:
            parsed = json.loads(ca)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _listing_type_from_post(post: dict) -> str | None:
    """Prefer caption_analysis.listing_type; some blobs may repeat listing_type at post root."""
    ca = _caption_analysis_dict(post)
    if isinstance(ca, dict):
        lt = ca.get("listing_type")
        if lt is not None and str(lt).strip():
            return str(lt).strip().lower()
    lt = post.get("listing_type")
    if lt is not None and str(lt).strip():
        return str(lt).strip().lower()
    return None


def post_visible_on_site(post: dict) -> bool:
    """Feed: show only posts where Gemini classified listing_type as nightlife_event."""
    return _listing_type_from_post(post) == "nightlife_event"


def resolved_venue_category_slug(post: dict, username: str) -> str | None:
    """
    Slug for ?venue= filter and posts.venue_category column.
    Uses caption_analysis.venue_category when set and not unknown; else heuristics
    (kafana/кафана, saloon, nightclub in handle, …) so old DB rows without that JSON field still filter.
    """
    ca = _caption_analysis_dict(post) or {}
    if ca.get("listing_type") == "not_nightlife":
        return None
    raw = (ca.get("venue_category") or "").strip().lower()
    if raw in VENUE_CATEGORY_FILTER_VALUES and raw != "unknown":
        return raw
    un = (username or "").lower()
    cap = (post.get("caption") or "").lower() if isinstance(post, dict) else ""
    loc = (ca.get("location") or "").lower()
    blob = f"{un} {cap} {loc}"
    if "saloon" in blob:
        return "bar_pub"
    if re.search(r"\b(pub|паб|irish)\b", blob, re.I):
        return "bar_pub"
    if "kafana" in blob or "кафана" in blob:
        return "kafana"
    if "lounge" in blob or "rooftop" in blob:
        return "lounge_rooftop"
    if "mkc" in blob or "филхармонија" in blob or "universal hall" in blob:
        return "concert_venue"
    if "nightclub" in un:
        return "nightclub"
    if re.search(r"\bdisco\b|\bдискотека\b", blob, re.I):
        return "nightclub"
    return None


def pick_media_for_post(post: dict) -> dict | None:
    """First usable image URL for the card (secure_url or url); then video."""

    def _url(m: dict) -> str:
        return (m.get("secure_url") or m.get("url") or "").strip()

    for m in post.get("media") or []:
        if not isinstance(m, dict):
            continue
        if _url(m) and m.get("resource_type") != "video":
            out = dict(m)
            if not out.get("secure_url"):
                out["secure_url"] = _url(m)
            return out
    for m in post.get("media") or []:
        if not isinstance(m, dict):
            continue
        if _url(m):
            out = dict(m)
            if not out.get("secure_url"):
                out["secure_url"] = _url(m)
            return out
    return None


def _post_ts(post: dict) -> datetime:
    ts = (post or {}).get("timestamp") or ""
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _posted_date_str(post: dict) -> str | None:
    dt = _post_ts(post)
    if dt == datetime.min.replace(tzinfo=timezone.utc):
        return None
    return dt.date().isoformat()


def _event_date_range_from_post(post: dict) -> tuple[str | None, str | None]:
    """Gemini caption_analysis.event_date: YYYY-MM-DD or multiple in one string."""
    ca = _caption_analysis_dict(post)
    if not isinstance(ca, dict):
        return None, None
    raw = ca.get("event_date")
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    found = sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", s)))
    valid: list[str] = []
    for d in found:
        try:
            date.fromisoformat(d)
            valid.append(d)
        except ValueError:
            continue
    if not valid:
        return None, None
    if len(valid) == 1:
        return valid[0], None
    return valid[0], valid[-1]


_VALID_ROLES = frozenset({"dj", "singer", "live_band", "artist", "mc", "unknown"})


def _performer_roles_json(post: dict) -> str | None:
    """JSON array of unique performer roles from caption_analysis."""
    ca = _caption_analysis_dict(post)
    if not isinstance(ca, dict):
        return None
    roles: list[str] = []
    seen: set[str] = set()
    for perf in ca.get("performers") or []:
        if not isinstance(perf, dict):
            continue
        r = perf.get("role")
        if r in _VALID_ROLES and r and r not in seen:
            seen.add(r)
            roles.append(r)
    if not roles:
        return None
    return json.dumps(roles, ensure_ascii=False)


def _ticket_and_listing(post: dict) -> tuple[float | None, str | None]:
    ca = _caption_analysis_dict(post) or {}
    raw = ca.get("ticket_price_mkd")
    try:
        price = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        price = None
    lt = _listing_type_from_post(post)
    return price, (lt if lt is not None else None)


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "No Postgres URL: set DATABASE_URL or DATABASE_PUBLIC_URL (e.g. in .env)."
        )
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    init_schema(conn)
    # End implicit txn from DDL so clients never stack BEGIN (avoids "transaction already in progress").
    conn.commit()
    return conn


def load_catalog_json() -> dict:
    if not CATALOG_JSON_PATH.is_file():
        return {"by_username": {}}
    with open(CATALOG_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def upsert_post_row(conn, username: str, post: dict) -> None:
    """INSERT or UPDATE one post row (same columns as sync_from_json)."""
    if not isinstance(post, dict):
        return
    ts_dt = _post_ts(post)
    posted_at = None if ts_dt.year <= 1 else ts_dt
    pdate_s = _posted_date_str(post)
    pdate = date.fromisoformat(pdate_s) if pdate_s else None
    ev_start_s, ev_end_s = _event_date_range_from_post(post)
    ev_start = date.fromisoformat(ev_start_s) if ev_start_s else None
    ev_end = date.fromisoformat(ev_end_s) if ev_end_s else None
    ticket, listing = _ticket_and_listing(post)
    proles_s = _performer_roles_json(post)
    proles = Jsonb(json.loads(proles_s)) if proles_s else None
    vis = bool(post_visible_on_site(post))
    vcat = resolved_venue_category_slug(post, username)
    conn.execute(
        """INSERT INTO posts (username, post_index, post_json, posted_at, posted_date,
           event_date, event_date_end, ticket_price_mkd, listing_type, performer_roles, visible, venue_category)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (username, post_index) DO UPDATE SET
             post_json = EXCLUDED.post_json,
             posted_at = EXCLUDED.posted_at,
             posted_date = EXCLUDED.posted_date,
             event_date = EXCLUDED.event_date,
             event_date_end = EXCLUDED.event_date_end,
             ticket_price_mkd = EXCLUDED.ticket_price_mkd,
             listing_type = EXCLUDED.listing_type,
             performer_roles = EXCLUDED.performer_roles,
             visible = EXCLUDED.visible,
             venue_category = EXCLUDED.venue_category
           """,
        (
            username,
            post.get("post_index"),
            Jsonb(post),
            posted_at,
            pdate,
            ev_start,
            ev_end,
            ticket,
            listing,
            proles,
            vis,
            vcat,
        ),
    )


def upsert_account_and_posts(username: str, profile: dict, posts: list[dict]) -> None:
    """Merge one user into Postgres without truncating other accounts (suggest / DB-only ingest)."""
    with closing(get_connection()) as conn:
        conn.execute(
            "INSERT INTO accounts (username, profile_json) VALUES (%s, %s) "
            "ON CONFLICT (username) DO UPDATE SET profile_json = EXCLUDED.profile_json",
            (username, Jsonb(profile)),
        )
        for post in posts or []:
            upsert_post_row(conn, username, post)
        conn.commit()


def delete_account_and_posts(username: str) -> None:
    """Remove one user and their posts (rollback failed suggest)."""
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM posts WHERE LOWER(username) = LOWER(%s)", (username,))
        conn.execute("DELETE FROM accounts WHERE LOWER(username) = LOWER(%s)", (username,))
        conn.commit()


def fetch_posts_for_username(username: str) -> list[dict]:
    """Post dicts from DB in post_index order (caption_analysis from Gemini may be present)."""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT post_json FROM posts WHERE LOWER(username) = LOWER(%s) "
            "ORDER BY post_index NULLS LAST",
            (username,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        pj = r["post_json"]
        if isinstance(pj, str):
            pj = json.loads(pj)
        if isinstance(pj, dict):
            out.append(pj)
    return out


def sync_from_json(catalog: dict | None = None) -> tuple[int, int]:
    """Replace DB from catalog dict or from JSON file. Returns (account_count, post_count)."""
    if catalog is None:
        catalog = load_catalog_json()
    by_u = catalog.get("by_username") or {}
    if not isinstance(by_u, dict):
        by_u = {}

    with closing(get_connection()) as conn:
        conn.execute("TRUNCATE TABLE posts RESTART IDENTITY CASCADE")
        conn.execute("TRUNCATE TABLE accounts RESTART IDENTITY CASCADE")
        ac = 0
        pc = 0
        for username in sorted(by_u.keys()):
            block = by_u[username] or {}
            profile = block.get("profile") or {}
            conn.execute(
                "INSERT INTO accounts (username, profile_json) VALUES (%s, %s)",
                (username, Jsonb(profile)),
            )
            ac += 1
            for post in block.get("posts") or []:
                if not isinstance(post, dict):
                    continue
                upsert_post_row(conn, username, post)
                pc += 1
        conn.commit()
    return ac, pc


def refresh_posts_venue_category_from_db() -> int:
    """Recompute posts.venue_category from post_json + username (fixes filters for old analyze runs)."""
    n = 0
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT id, username, post_json FROM posts"
        ).fetchall()
        for r in rows:
            pj = r["post_json"]
            if isinstance(pj, str):
                pj = json.loads(pj)
            if not isinstance(pj, dict):
                continue
            slug = resolved_venue_category_slug(pj, str(r["username"] or ""))
            conn.execute(
                "UPDATE posts SET venue_category = %s WHERE id = %s",
                (slug, r["id"]),
            )
            n += 1
        conn.commit()
    return n


def refresh_posts_visible_from_db() -> int:
    """
    Recompute posts.visible from post_json only (no catalog JSON file).
    Use on Railway/local after changing post_visible_on_site or if visible is stale.
    """
    n = 0
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT id, post_json FROM posts").fetchall()
        for r in rows:
            pj = r["post_json"]
            if isinstance(pj, str):
                pj = json.loads(pj)
            if not isinstance(pj, dict):
                continue
            vis = post_visible_on_site(pj)
            conn.execute(
                "UPDATE posts SET visible = %s WHERE id = %s",
                (vis, r["id"]),
            )
            n += 1
        conn.commit()
    return n


def _posts_flat_from_by_username(by_u: dict) -> list[dict]:
    """Same shape as scrape_rapidapi_cloudinary.build_posts_flat (for catalog JSON)."""
    posts_flat: list[dict] = []
    for un in sorted(by_u.keys()):
        block = by_u[un]
        for p in block.get("posts") or []:
            if not isinstance(p, dict):
                continue
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


def export_db_to_catalog_json(out_path: Path | None = None) -> tuple[int, int]:
    """
    Write cloudinary_catalog.json from Postgres (accounts + posts.post_json).

    Use when the DB (e.g. Railway) has users that are missing from your local JSON —
    then commit or back up that file. Otherwise a plain ``sync_from_json`` from an
    incomplete local JSON TRUNCATES the DB and drops those accounts.
    """
    path = out_path or CATALOG_JSON_PATH
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    by_u: dict[str, dict] = {}
    ac = 0
    pc = 0
    with closing(get_connection()) as conn:
        acc_rows = conn.execute(
            "SELECT username, profile_json FROM accounts ORDER BY username"
        ).fetchall()
        for ar in acc_rows:
            un = ar["username"]
            prof = ar["profile_json"]
            if isinstance(prof, str):
                prof = json.loads(prof)
            if not isinstance(prof, dict):
                prof = {}
            post_rows = conn.execute(
                "SELECT post_json FROM posts WHERE username = %s "
                "ORDER BY post_index NULLS LAST",
                (un,),
            ).fetchall()
            plist: list[dict] = []
            for pr in post_rows:
                pj = pr["post_json"]
                if isinstance(pj, str):
                    pj = json.loads(pj)
                if isinstance(pj, dict):
                    plist.append(pj)
                    pc += 1
            by_u[un] = {
                "profile": prof,
                "posts": plist,
                "last_scraped_at": now_iso,
            }
            ac += 1
    catalog: dict[str, Any] = {
        "by_username": by_u,
        "posts_flat": _posts_flat_from_by_username(by_u),
        "gemini_last_saved_at": now_iso,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return ac, pc


def ensure_database() -> None:
    """Create schema and import seed JSON when DB is empty."""
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()
        n = int(row["c"]) if row else 0
    if n == 0 and CATALOG_JSON_PATH.is_file():
        sync_from_json()


def _hidden_lower_tuple() -> tuple[str, ...]:
    return tuple(sorted(HIDDEN_FROM_FEED_USERNAMES))


def _hidden_exclude_posts_sql(alias: str = "p") -> tuple[str, list[Any]]:
    """SQL fragment: exclude hidden usernames (case-insensitive)."""
    if not HIDDEN_FROM_FEED_USERNAMES:
        return "", []
    col = f"LOWER({alias}.username)"
    vals = list(_hidden_lower_tuple())
    ph = ",".join(["%s"] * len(vals))
    return (f" AND {col} NOT IN ({ph})", vals)


def _hidden_accounts_where() -> tuple[str, list[Any]]:
    if not HIDDEN_FROM_FEED_USERNAMES:
        return "", []
    vals = list(_hidden_lower_tuple())
    ph = ",".join(["%s"] * len(vals))
    return (f"WHERE LOWER(username) NOT IN ({ph})", vals)


def list_usernames() -> list[str]:
    hw, hargs = _hidden_accounts_where()
    sql = "SELECT username FROM accounts"
    if hw:
        sql += " " + hw
    sql += " ORDER BY username"
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, hargs).fetchall()
        return [r["username"] for r in rows]


def account_exists(username: str) -> bool:
    u = username.strip()
    if u.lower() in HIDDEN_FROM_FEED_USERNAMES:
        return False
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT 1 FROM accounts WHERE LOWER(username) = LOWER(%s) LIMIT 1", (u,)
        ).fetchone()
        return r is not None


def username_in_catalog(username: str) -> bool:
    """
    True if this Instagram handle is already tracked (DB, JSON catalog, or scrape list).
    Used before running RapidAPI/Gemini for user suggestions.
    """
    raw = (username or "").strip().lstrip("@")
    if not raw:
        return False
    un_lower = raw.lower()
    if un_lower in HIDDEN_FROM_FEED_USERNAMES:
        return True
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT 1 FROM accounts WHERE lower(username) = %s LIMIT 1",
            (un_lower,),
        ).fetchone()
        if r:
            return True
    cat = load_catalog_json()
    for k in (cat.get("by_username") or {}):
        if str(k).strip().lower() == un_lower:
            return True
    return False


def _build_posts_flat_local(by_u: dict) -> list[dict]:
    """Mirror of scrape_rapidapi_cloudinary.build_posts_flat (avoid import cycle)."""
    posts_flat: list[dict] = []
    for un in sorted((by_u or {}).keys()):
        block = by_u[un] or {}
        for p in block.get("posts") or []:
            if not isinstance(p, dict):
                continue
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


SCRAPE_USERNAMES_PATH = ROOT / "back-end" / "data" / "scrape_usernames.txt"


def remove_username_from_catalog_and_sync(username: str) -> bool:
    """Remove one handle from cloudinary_catalog.json and run full Postgres sync. Returns True if removed."""
    raw = (username or "").strip().lstrip("@")
    if not raw:
        return False
    un_lower = raw.lower()
    cat = load_catalog_json()
    by_u = dict(cat.get("by_username") or {})
    key_to_remove = None
    for k in list(by_u.keys()):
        if str(k).strip().lower() == un_lower:
            key_to_remove = k
            break
    if not key_to_remove:
        return False
    del by_u[key_to_remove]
    cat["by_username"] = by_u
    cat["posts_flat"] = _build_posts_flat_local(by_u)
    CATALOG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
    sync_from_json(cat)
    return True


def delete_suggest_jobs_for_username(username: str) -> int:
    """Delete suggest_jobs rows for this raw username (case-insensitive). Returns rowcount."""
    raw = (username or "").strip().lstrip("@")
    if not raw:
        return 0
    un_lower = raw.lower()
    with closing(get_connection()) as conn:
        cur = conn.execute(
            "DELETE FROM suggest_jobs WHERE lower(trim(username_raw)) = %s",
            (un_lower,),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def remove_username_from_scrape_usernames_file(username: str) -> bool:
    """Remove the line for this username from scrape_usernames.txt if present. Returns True if a line was removed."""
    raw = (username or "").strip().lstrip("@")
    if not raw or not SCRAPE_USERNAMES_PATH.is_file():
        return False
    un_lower = raw.lower()
    lines = SCRAPE_USERNAMES_PATH.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    removed = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped.strip():
            out.append(line)
            continue
        first = stripped.split("|", 1)[0].strip().lower()
        if first == un_lower:
            removed = True
            continue
        out.append(line)
    if not removed:
        return False
    SCRAPE_USERNAMES_PATH.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return True


def clear_stuck_suggest_user(username: str) -> dict[str, bool | int]:
    """Remove username from catalog JSON+DB, suggest_jobs, and scrape_usernames line (admin / retry after bad suggest)."""
    removed_cat = remove_username_from_catalog_and_sync(username)
    deleted_jobs = delete_suggest_jobs_for_username(username)
    removed_txt = remove_username_from_scrape_usernames_file(username)
    return {
        "removed_from_catalog": removed_cat,
        "deleted_suggest_jobs": deleted_jobs,
        "removed_from_scrape_list": removed_txt,
    }


def sync_accounts_from_scrape_usernames() -> int:
    """
    One-off admin helper: ensure every username in back-end/data/scrape_usernames.txt
    exists as a row in accounts (empty profile_json). Returns number of inserts.
    """
    if not SCRAPE_USERNAMES_PATH.is_file():
        return 0
    rows = []
    for line in SCRAPE_USERNAMES_PATH.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        username = raw.split("|", 1)[0].strip().lstrip("@")
        if username:
            rows.append(username)
    if not rows:
        return 0
    inserted = 0
    with closing(get_connection()) as conn:
        for username in sorted(set(rows)):
            cur = conn.execute(
                """
                INSERT INTO accounts (username, profile_json)
                VALUES (%s, '{}'::jsonb)
                ON CONFLICT (username) DO NOTHING
                """,
                (username,),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    return inserted


def _parse_block_created_at(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# Queued forever = worker down / never started.
# "running" with very old updated_at = worker crashed after claim (updated_at not heartbeated).
_STALE_QUEUED_MINUTES = 120
_STALE_RUNNING_HOURS = 6
_STALE_FAIL_MSG = "Обработката траеше предолго. Обиди се повторно."

# Serialize stale repair across multiple Railway workers (same UPDATE was deadlocking).
_ADV_LOCK_REPAIR_K1 = 54821
_ADV_LOCK_REPAIR_K2 = 92017


def suggest_repair_stale_jobs() -> int:
    """Fail stale queued jobs; clear zombie running jobs (worker crash).

    Advisory xact lock so only one session runs this UPDATE at a time (avoids suggest_jobs deadlocks).
    Retries on deadlock if another client still uses an older repair query.
    """
    now = datetime.now(timezone.utc)
    cutoff_q = now - timedelta(minutes=_STALE_QUEUED_MINUTES)
    cutoff_run = now - timedelta(hours=_STALE_RUNNING_HOURS)
    sql = """
    UPDATE suggest_jobs
    SET status = 'done', ok = false, message = %s, updated_at = %s
    WHERE (status = 'queued' AND created_at < %s)
       OR (status = 'running' AND updated_at < %s)
    """
    params = (_STALE_FAIL_MSG, now, cutoff_q, cutoff_run)
    for attempt in range(5):
        try:
            with closing(get_connection()) as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_ADV_LOCK_REPAIR_K1, _ADV_LOCK_REPAIR_K2),
                )
                cur = conn.execute(sql, params)
                n = cur.rowcount if cur.rowcount is not None else 0
                conn.commit()
                return int(n)
        except DeadlockDetected:
            if attempt >= 4:
                raise
            time.sleep(0.02 + random.random() * 0.06)
    return 0


def clear_all_suggest_jobs() -> int:
    """DELETE all rows from suggest_jobs (admin / local debug). Returns deleted count."""
    with closing(get_connection()) as conn:
        cur = conn.execute("DELETE FROM suggest_jobs")
        conn.commit()
        n = cur.rowcount if cur.rowcount is not None else 0
    return n


def suggest_jobs_open_summary() -> str:
    """Short line for worker logs: queued vs running counts (running jobs are not re-claimed)."""
    with closing(get_connection()) as conn:
        qrow = conn.execute(
            "SELECT COUNT(*) AS c FROM suggest_jobs WHERE status = 'queued'"
        ).fetchone()
        rrow = conn.execute(
            "SELECT COUNT(*) AS c FROM suggest_jobs WHERE status = 'running'"
        ).fetchone()
        nq = int(qrow["c"]) if qrow else 0
        nr = int(rrow["c"]) if rrow else 0
        rows = conn.execute(
            """
            SELECT id, username_raw
            FROM suggest_jobs
            WHERE status = 'running'
            ORDER BY id ASC
            LIMIT 8
            """
        ).fetchall()
    parts = [f"queued={nq}", f"running={nr}"]
    if rows:
        bits = [f"{r['id']}:{(r['username_raw'] or '')[:20]}" for r in rows]
        parts.append("running_jobs=" + ",".join(bits))
    return " | ".join(parts)


def suggest_get_active_job_for_ip(ip_hash: str) -> dict | None:
    """Most recent queued/running job for this IP, or None."""
    if not ip_hash:
        return None
    with closing(get_connection()) as conn:
        r = conn.execute(
            """
            SELECT id, username_raw, ip_hash, status, ok, message, canonical_username, created_at, updated_at
            FROM suggest_jobs
            WHERE ip_hash = %s AND status IN ('queued', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (ip_hash,),
        ).fetchone()
        return dict(r) if r else None


def suggest_has_pending_job(ip_hash: str) -> bool:
    if not ip_hash:
        return False
    with closing(get_connection()) as conn:
        r = conn.execute(
            """
            SELECT 1
            FROM suggest_jobs
            WHERE ip_hash = %s AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (ip_hash,),
        ).fetchone()
        return r is not None


def suggest_log_submission(ip_hash: str) -> None:
    if not ip_hash:
        return
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn:
        conn.execute(
            "INSERT INTO suggest_submit_log (ip_hash, created_at) VALUES (%s, %s)",
            (ip_hash, now),
        )
        conn.commit()


def suggest_window_info(ip_hash: str) -> tuple[int, datetime | None]:
    """Returns (count_in_window, reset_at_utc). reset_at is oldest_attempt+SUGGEST_WINDOW."""
    if not ip_hash:
        return 0, None
    since = datetime.now(timezone.utc) - SUGGEST_WINDOW
    with closing(get_connection()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c, MIN(created_at) AS oldest
            FROM suggest_submit_log
            WHERE ip_hash = %s AND created_at >= %s
            """,
            (ip_hash, since),
        ).fetchone()
        c = int(row["c"]) if row else 0
        oldest = row["oldest"] if row else None
        if not oldest or not isinstance(oldest, datetime):
            return c, None
        return c, oldest + SUGGEST_WINDOW


def suggest_job_create(username_raw: str, ip_hash: str) -> int:
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn:
        r = conn.execute(
            """
            INSERT INTO suggest_jobs (username_raw, ip_hash, status, created_at, updated_at)
            VALUES (%s, %s, 'queued', %s, %s)
            RETURNING id
            """,
            (username_raw, ip_hash, now, now),
        ).fetchone()
        conn.commit()
        return int(r["id"])


def suggest_job_get(job_id: int) -> dict | None:
    with closing(get_connection()) as conn:
        r = conn.execute(
            """
            SELECT id, username_raw, ip_hash, status, ok, message, canonical_username, created_at, updated_at
            FROM suggest_jobs
            WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
        return dict(r) if r else None


def suggest_job_claim_next() -> dict | None:
    """Atomically claim one queued job and mark it running.

    Single UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) avoids a deadlock
    window between two statements (SELECT then UPDATE) when multiple workers
    or repair run concurrently.
    """
    now = datetime.now(timezone.utc)
    sql = """
    WITH c AS (
      SELECT id
      FROM suggest_jobs
      WHERE status = 'queued'
      ORDER BY id ASC
      LIMIT 1
      FOR UPDATE SKIP LOCKED
    )
    UPDATE suggest_jobs AS j
    SET status = 'running', updated_at = %s
    FROM c
    WHERE j.id = c.id
    RETURNING j.id, j.username_raw, j.ip_hash
    """
    for attempt in range(5):
        try:
            with closing(get_connection()) as conn:
                r = conn.execute(sql, (now,)).fetchone()
                conn.commit()
                if not r:
                    return None
                return {
                    "id": int(r["id"]),
                    "username_raw": r["username_raw"],
                    "ip_hash": r["ip_hash"],
                }
        except DeadlockDetected:
            if attempt >= 4:
                raise
            time.sleep(0.02 + random.random() * 0.06)
    return None


def suggest_job_finish(
    job_id: int,
    ok: bool,
    message: str,
    canonical_username: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn:
        conn.execute(
            """
            UPDATE suggest_jobs
            SET status = 'done', ok = %s, message = %s, canonical_username = %s, updated_at = %s
            WHERE id = %s
            """,
            (bool(ok), (message or "")[:800], canonical_username, now, job_id),
        )
        conn.commit()


def _ym_utc(now: datetime | None = None) -> str:
    n = now or datetime.now(timezone.utc)
    return f"{n.year:04d}-{n.month:02d}"


def suggest_global_monthly_remaining() -> int:
    ym = _ym_utc()
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT used FROM suggest_global_monthly WHERE ym = %s",
            (ym,),
        ).fetchone()
        used = int(r["used"]) if r and r.get("used") is not None else 0
        return max(0, int(SUGGEST_GLOBAL_MONTHLY_LIMIT) - used)


def suggest_global_monthly_try_consume() -> bool:
    """Atomically consume 1 from monthly allowance. Returns True if allowed."""
    ym = _ym_utc()
    with closing(get_connection()) as conn:
        conn.execute("BEGIN")
        r = conn.execute(
            """
            INSERT INTO suggest_global_monthly (ym, used)
            VALUES (%s, 1)
            ON CONFLICT (ym) DO UPDATE
            SET used = suggest_global_monthly.used + 1
            WHERE suggest_global_monthly.used < %s
            RETURNING used
            """,
            (ym, int(SUGGEST_GLOBAL_MONTHLY_LIMIT)),
        ).fetchone()
        if not r:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True


def _has_price_clause(has_price: bool | None) -> tuple[str, list[Any]]:
    """When True, only posts with a numeric ticket price."""
    if has_price is not True:
        return "", []
    return " AND p.ticket_price_mkd IS NOT NULL", []


# SQLite strftime('%%w'): 0=Sunday .. 6=Saturday
_WEEKDAY_SQL: dict[str, str] = {
    "monday": "1",
    "tuesday": "2",
    "wednesday": "3",
    "thursday": "4",
    "friday": "5",
    "saturday": "6",
    "sunday": "0",
}


def _weekday_clause(weekday: str | None) -> tuple[str, list[Any]]:
    """Filter by calendar weekday of AI event_date (first YYYY-MM-DD in column)."""
    if not weekday:
        return "", []
    w = str(weekday).strip().lower()
    if w not in _WEEKDAY_SQL:
        return "", []
    # Postgres: EXTRACT(DOW) => 0=Sunday..6=Saturday
    return (
        " AND p.event_date IS NOT NULL AND EXTRACT(DOW FROM p.event_date) = %s",
        [int(_WEEKDAY_SQL[w])],
    )


def _date_clause(
    date_from: date | None, date_to: date | None
) -> tuple[str, list[Any]]:
    """Filter by AI event_date (caption_analysis): overlap with [date_from, date_to]."""
    cond: list[str] = []
    args: list[Any] = []
    if date_from is not None:
        cond.append(
            "(p.event_date IS NOT NULL AND COALESCE(p.event_date_end, p.event_date) >= %s)"
        )
        args.append(date_from)
    if date_to is not None:
        cond.append("(p.event_date IS NOT NULL AND p.event_date <= %s)")
        args.append(date_to)
    if not cond:
        return "", []
    return " AND " + " AND ".join(cond), args


# Feed: hide Gemini-tagged menu/non-event posts (column mirrors caption_analysis.listing_type on upsert).
_EXCLUDE_NOT_NIGHTLIFE_SQL = " AND p.listing_type IS DISTINCT FROM 'not_nightlife'"


def user_blocks_from_db(
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """Same shape as gallery_app.user_blocks_from_catalog."""
    ds, dargs = _date_clause(date_from, date_to)
    hs, hargs = _hidden_exclude_posts_sql("p")
    sql = (
        "SELECT p.username, p.post_json, a.profile_json FROM posts p "
        "JOIN accounts a ON a.username = p.username "
        "WHERE TRUE" + _EXCLUDE_NOT_NIGHTLIFE_SQL + ds + hs + " ORDER BY LOWER(p.username), p.posted_at DESC"
    )
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, dargs + hargs).fetchall()

    grouped: dict[str, list[dict]] = {}
    profiles: dict[str, dict] = {}
    for r in rows:
        u = r["username"]
        post = r["post_json"] if isinstance(r.get("post_json"), dict) else json.loads(r["post_json"])
        media = pick_media_for_post(post)
        if not media:
            continue
        if u not in profiles:
            prof = r["profile_json"] if isinstance(r.get("profile_json"), dict) else json.loads(r["profile_json"] or "{}")
            profiles[u] = prof
        grouped.setdefault(u, []).append({"post": post, "media": media})

    out: list[dict] = []
    for username in sorted(grouped.keys(), key=str.casefold):
        entries = grouped[username]
        entries.sort(key=lambda x: _post_ts(x["post"]), reverse=True)
        prof = profiles.get(username) or {}
        display_name = prof.get("full_name") or username.replace(".", " ").title()
        out.append(
            {
                "username": username,
                "display_name": display_name,
                "entries": entries,
            }
        )
    return out


def _profile_display_handle(prof: dict, username: str) -> str:
    """Handle for @ label / instagram.com/{handle}/ — list or user-submitted name, else DB username."""
    dh = prof.get("display_handle")
    if dh is not None:
        s = str(dh).strip().lstrip("@")
        if s:
            return s
    return username


def _username_clause(username: str | None) -> tuple[str, list[Any]]:
    if not username or not str(username).strip():
        return "", []
    return " AND LOWER(p.username) = LOWER(%s)", [username.strip()]


def _search_clause(search: str | None) -> tuple[str, list[Any]]:
    """Substring match: username (handles @…), profile name/handle, caption + AI JSON (опис)."""
    if not search or not str(search).strip():
        return "", []
    raw = " ".join(str(search).strip().split())
    if len(raw) > 120:
        raw = raw[:120]
    for ch in ("%", "_"):
        raw = raw.replace(ch, "")
    if not raw:
        return "", []
    low = raw.lower()
    # Usernames in DB have no '@'; strip @ so "@club" matches club…
    user_needle = low.replace("@", "").strip()
    if not user_needle:
        return "", []
    pat_text = f"%{low}%"
    pat_user = f"%{user_needle}%"
    return (
        " AND (LOWER(p.username) LIKE %s "
        "OR LOWER(COALESCE(a.profile_json->>'full_name', '')) LIKE %s "
        "OR LOWER(COALESCE(a.profile_json->>'display_handle', '')) LIKE %s "
        "OR LOWER(p.post_json::text) LIKE %s)",
        [pat_user, pat_text, pat_text, pat_text],
    )


def _role_clause(performer_role: str | None) -> tuple[str, list[Any]]:
    """Match a role inside JSONB array stored in performer_roles."""
    if not performer_role or performer_role not in _VALID_ROLES:
        return "", []
    return (
        " AND p.performer_roles IS NOT NULL AND p.performer_roles @> %s"
    ), [Jsonb([performer_role])]


def _venue_category_json_sql_value() -> str:
    """Extract venue_category from JSON only (fallback if posts.venue_category column is null)."""
    return """(
      CASE
        WHEN TRIM(COALESCE(p.post_json#>>'{caption_analysis,venue_category}', '')) <> ''
          THEN LOWER(TRIM(p.post_json#>>'{caption_analysis,venue_category}'))
        WHEN TRIM(COALESCE(p.post_json#>>'{caption_analysis}', '')) <> ''
          THEN LOWER(TRIM((p.post_json#>>'{caption_analysis}')::jsonb->>'venue_category'))
        ELSE LOWER(TRIM(COALESCE(p.post_json->>'venue_category', '')))
      END
    )"""


def _venue_category_clause(venue_category: str | None) -> tuple[str, list[Any]]:
    """Match posts.venue_category (filled at upsert) or JSON path as fallback."""
    if not venue_category:
        return "", []
    v = str(venue_category).strip().lower()
    if v not in VENUE_CATEGORY_FILTER_VALUES:
        return "", []
    json_expr = _venue_category_json_sql_value()
    return (
        " AND (LOWER(TRIM(COALESCE(p.venue_category, ''))) = %s OR "
        + json_expr.strip()
        + " = %s)",
        [v, v],
    )


def _where_visible_and_filters(
    date_from: date | None,
    date_to: date | None,
    has_price: bool | None,
    weekday: str | None,
    username: str | None,
    performer_role: str | None = None,
    search: str | None = None,
    venue_category: str | None = None,
) -> tuple[str, list[Any]]:
    ds, dargs = _date_clause(date_from, date_to)
    ps, pargs = _has_price_clause(has_price)
    ws, wargs = _weekday_clause(weekday)
    us, uargs = _username_clause(username)
    rs, rargs = _role_clause(performer_role)
    vs, vargs = _venue_category_clause(venue_category)
    ss, sargs = _search_clause(search)
    hs, hargs = _hidden_exclude_posts_sql("p")
    # Hide not_nightlife (posts.listing_type); NULL / nightlife_event still show.
    return (
        " WHERE TRUE" + _EXCLUDE_NOT_NIGHTLIFE_SQL + ds + ps + ws + us + rs + vs + ss + hs,
        dargs + pargs + wargs + uargs + rargs + vargs + sargs + hargs,
    )


# Global ordering for flat event feeds:
# 1) Posts with AI event_date today or in the future: soonest date first, then like_count (post_json).
# 2) Bottom: past events (Поминат), no event_date, or “only when posted” style cards (no usable future date).
_ORDER_FLAT = (
    " ORDER BY "
    "CASE "
    "  WHEN p.event_date IS NOT NULL AND p.event_date >= CURRENT_DATE THEN 0 "
    "  ELSE 1 "
    "END ASC, "
    "CASE "
    "  WHEN p.event_date IS NOT NULL AND p.event_date >= CURRENT_DATE THEN p.event_date "
    "END ASC NULLS LAST, "
    "CASE "
    "  WHEN p.event_date IS NOT NULL AND p.event_date >= CURRENT_DATE "
    "  THEN COALESCE((NULLIF(TRIM(p.post_json->>'like_count'), ''))::bigint, 0) "
    "END DESC NULLS LAST, "
    "CASE WHEN p.posted_at IS NULL THEN 1 ELSE 0 END ASC, "
    "p.posted_at DESC"
)

# For now, the main feed (no filters) uses the same ordering as filtered views.
_ORDER_BY_POSTED = _ORDER_FLAT


def first_event_date_from_post(post: dict) -> date | None:
    """First YYYY-MM-DD from caption_analysis.event_date, if parseable."""
    start, _ = _event_date_range_from_post(post)
    if not start:
        return None
    try:
        return date.fromisoformat(start[:10])
    except ValueError:
        return None


def fetch_flat_events_for_weekend_range(date_from: date, date_to: date) -> list[dict]:
    """Posts whose AI event_date overlaps [date_from, date_to] (same rules as date filter)."""
    where_sql, args = _where_visible_and_filters(
        date_from, date_to, None, None, None, None, None, None
    )
    sql = (
        "SELECT p.username, p.post_json, a.profile_json FROM posts p "
        "JOIN accounts a ON a.username = p.username"
        + where_sql
        + _ORDER_FLAT
    )
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, args).fetchall()
    out: list[dict] = []
    for r in rows:
        post = r["post_json"] if isinstance(r.get("post_json"), dict) else json.loads(r["post_json"])
        media = pick_media_for_post(post)
        if not media:
            continue
        prof = r["profile_json"] if isinstance(r.get("profile_json"), dict) else json.loads(r["profile_json"] or "{}")
        un = r["username"]
        fn = prof.get("full_name")
        profile_full_name = str(fn).strip() if fn else None
        display_name = profile_full_name or un.replace(".", " ").title()
        ig_handle = _profile_display_handle(prof, un)
        out.append(
            {
                "username": un,
                "instagram_handle": ig_handle,
                "display_name": display_name,
                "profile_full_name": profile_full_name,
                "post": post,
                "media": media,
            }
        )
    return out


def random_weekend_surprise_entries(
    fri: date, sun: date, k: int = 3
) -> list[dict]:
    """
    Up to k random events whose first AI event_date falls on Fri–Sun within [fri, sun]
    (closest weekend window in local time — caller computes fri/sun).
    """
    pool = fetch_flat_events_for_weekend_range(fri, sun)
    filtered: list[dict] = []
    for e in pool:
        d = first_event_date_from_post(e["post"])
        if d is None:
            continue
        if fri <= d <= sun:
            filtered.append(e)
    if not filtered:
        return []
    if len(filtered) <= k:
        return filtered
    return random.sample(filtered, k)


def count_flat_events(
    date_from: date | None = None,
    date_to: date | None = None,
    has_price: bool | None = None,
    weekday: str | None = None,
    username: str | None = None,
    performer_role: str | None = None,
    search: str | None = None,
    venue_category: str | None = None,
) -> int:
    where_sql, args = _where_visible_and_filters(
        date_from,
        date_to,
        has_price,
        weekday,
        username,
        performer_role,
        search,
        venue_category,
    )
    sql = (
        "SELECT COUNT(*) AS c FROM posts p JOIN accounts a ON a.username = p.username"
        + where_sql
    )
    with closing(get_connection()) as conn:
        row = conn.execute(sql, args).fetchone()
        return int(row["c"]) if row else 0


def fetch_flat_events_filtered(
    date_from: date | None = None,
    date_to: date | None = None,
    has_price: bool | None = None,
    weekday: str | None = None,
    username: str | None = None,
    offset: int = 0,
    limit: int = 8,
    filters_active: bool = False,
    performer_role: str | None = None,
    search: str | None = None,
    venue_category: str | None = None,
) -> tuple[list[dict], int]:
    """
    Flat list of posts with at least one image/video URL; excludes listing_type = not_nightlife.
    Sort: upcoming/today with event_date first (soonest date), then like_count;
    past dates and missing event_date last (Поминат / posted-only).
    Each entry: username, instagram_handle, display_name, profile_full_name, post, media.
    """
    where_sql, args = _where_visible_and_filters(
        date_from,
        date_to,
        has_price,
        weekday,
        username,
        performer_role,
        search,
        venue_category,
    )
    total = count_flat_events(
        date_from,
        date_to,
        has_price,
        weekday,
        username,
        performer_role,
        search,
        venue_category,
    )
    order_sql = _ORDER_FLAT if filters_active else _ORDER_BY_POSTED
    sql = (
        "SELECT p.username, p.post_json, a.profile_json FROM posts p "
        "JOIN accounts a ON a.username = p.username"
        + where_sql
        + order_sql
        + " LIMIT %s OFFSET %s"
    )
    lim_args = list(args) + [limit, offset]
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, lim_args).fetchall()

    out: list[dict] = []
    for r in rows:
        post = r["post_json"] if isinstance(r.get("post_json"), dict) else json.loads(r["post_json"])
        media = pick_media_for_post(post)
        if not media:
            continue
        prof = r["profile_json"] if isinstance(r.get("profile_json"), dict) else json.loads(r["profile_json"] or "{}")
        un = r["username"]
        fn = prof.get("full_name")
        profile_full_name = str(fn).strip() if fn else None
        display_name = profile_full_name or un.replace(".", " ").title()
        ig_handle = _profile_display_handle(prof, un)
        out.append(
            {
                "username": un,
                "instagram_handle": ig_handle,
                "display_name": display_name,
                "profile_full_name": profile_full_name,
                "post": post,
                "media": media,
            }
        )
    return out, total


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--refresh-visible":
        n = refresh_posts_visible_from_db()
        print(f"Recomputed visible on {n} post row(s) -> Postgres (DATABASE_URL)")
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--refresh-venue":
        n = refresh_posts_venue_category_from_db()
        print(f"Recomputed venue_category on {n} post row(s) -> Postgres (DATABASE_URL)")
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--export-json":
        out = Path(sys.argv[2]).resolve() if len(sys.argv) >= 3 else CATALOG_JSON_PATH
        ac, pc = export_db_to_catalog_json(out)
        print(f"Exported {ac} accounts, {pc} posts -> {out}")
        sys.exit(0)

    if len(sys.argv) >= 3 and sys.argv[1] == "remove-user":
        u = sys.argv[2].strip()
        r = clear_stuck_suggest_user(u)
        print(
            f"removed_from_catalog={r['removed_from_catalog']} "
            f"deleted_suggest_jobs={r['deleted_suggest_jobs']} "
            f"removed_from_scrape_list={r['removed_from_scrape_list']}"
        )
        sys.exit(0)

    if not CATALOG_JSON_PATH.is_file():
        print(f"Missing {CATALOG_JSON_PATH}", file=sys.stderr)
        sys.exit(1)
    ac, pc = sync_from_json()
    print(f"Synced {CATALOG_JSON_PATH} -> Postgres (DATABASE_URL)")
    print(f"Accounts: {ac}  Posts: {pc}")
