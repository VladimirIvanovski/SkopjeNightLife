"""
Postgres store for NightLife catalog — filterable posts; full post blobs as JSONB.

URL date filters use caption_analysis.event_date (AI), not Instagram posted_at.

Sync from cloudinary_catalog.json:
  python catalog_db.py
Or: gallery_app can import JSON on first start when DB is empty.
"""

from __future__ import annotations

import json
import random
import re
import os
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent
CATALOG_JSON_PATH = ROOT / "back-end" / "data" / "cloudinary_catalog.json"
SCRAPE_USERNAMES_PATH = ROOT / "back-end" / "data" / "scrape_usernames.txt"

# Railway Postgres: set DATABASE_URL (postgres://...).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# After a rejected /suggest, same IP hash cannot submit again for this long.
SUGGEST_REJECT_COOLDOWN = timedelta(hours=24)
SUGGEST_MAX_SUBMITS_PER_24H_PER_IP = 3

# Lowercase Instagram usernames hidden from feed, /api/events, user picker, and /u/<name> (404).
HIDDEN_FROM_FEED_USERNAMES: frozenset[str] = frozenset({"equilibriumdaynight"})

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
  visible BOOLEAN NOT NULL DEFAULT FALSE
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
"""


def init_schema(conn) -> None:
    conn.execute(SCHEMA)


def post_visible_on_site(post: dict) -> bool:
    if not (post.get("caption") or "").strip():
        return False
    ca = post.get("caption_analysis")
    if isinstance(ca, dict) and ca.get("listing_type") == "not_nightlife":
        return False
    return True


def pick_media_for_post(post: dict) -> dict | None:
    for m in post.get("media") or []:
        if m.get("secure_url") and m.get("resource_type") != "video":
            return m
    for m in post.get("media") or []:
        if m.get("secure_url"):
            return m
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
    ca = post.get("caption_analysis")
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
    ca = post.get("caption_analysis")
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
    ca = post.get("caption_analysis")
    if not isinstance(ca, dict):
        return None, None
    raw = ca.get("ticket_price_mkd")
    try:
        price = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        price = None
    lt = ca.get("listing_type")
    return price, (str(lt) if lt is not None else None)


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (Railway Postgres).")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    init_schema(conn)
    return conn


def load_catalog_json() -> dict:
    if not CATALOG_JSON_PATH.is_file():
        return {"by_username": {}}
    with open(CATALOG_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


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
                conn.execute(
                    """INSERT INTO posts (username, post_index, post_json, posted_at, posted_date,
                       event_date, event_date_end, ticket_price_mkd, listing_type, performer_roles, visible)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (username, post_index) DO UPDATE SET
                         post_json = EXCLUDED.post_json,
                         posted_at = EXCLUDED.posted_at,
                         posted_date = EXCLUDED.posted_date,
                         event_date = EXCLUDED.event_date,
                         event_date_end = EXCLUDED.event_date_end,
                         ticket_price_mkd = EXCLUDED.ticket_price_mkd,
                         listing_type = EXCLUDED.listing_type,
                         performer_roles = EXCLUDED.performer_roles,
                         visible = EXCLUDED.visible
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
                    ),
                )
                pc += 1
        conn.commit()
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
    if username.strip().lower() in HIDDEN_FROM_FEED_USERNAMES:
        return False
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT 1 FROM accounts WHERE username = %s LIMIT 1", (username,)
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
    if SCRAPE_USERNAMES_PATH.is_file():
        for line in SCRAPE_USERNAMES_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            cell = s.split("|")[0].strip().lstrip("@").lower()
            if cell == un_lower:
                return True
    return False


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


def suggest_submit_is_blocked(ip_hash: str) -> bool:
    """True if this IP hash was rejected within the last SUGGEST_REJECT_COOLDOWN window."""
    if not ip_hash:
        return False
    with closing(get_connection()) as conn:
        r = conn.execute(
            "SELECT created_at FROM suggest_submit_blocks WHERE ip_hash = %s LIMIT 1",
            (ip_hash,),
        ).fetchone()
        if not r:
            return False
        created = r["created_at"] if isinstance(r.get("created_at"), datetime) else _parse_block_created_at(r.get("created_at"))
        if created is None:
            return True
        return datetime.now(timezone.utc) - created < SUGGEST_REJECT_COOLDOWN


def suggest_submit_block_ip(ip_hash: str) -> None:
    """Record rejection time; refreshes cooldown window if the same hash is blocked again."""
    if not ip_hash:
        return
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO suggest_submit_blocks (ip_hash, created_at)
            VALUES (%s, %s)
            ON CONFLICT(ip_hash) DO UPDATE SET created_at = excluded.created_at
            """,
            (ip_hash, now),
        )
        conn.commit()


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


def suggest_rate_limited(ip_hash: str) -> bool:
    """True if this IP exceeded SUGGEST_MAX_SUBMITS_PER_24H_PER_IP in last 24 hours."""
    if not ip_hash:
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM suggest_submit_log WHERE ip_hash = %s AND created_at >= %s",
            (ip_hash, since),
        ).fetchone()
        c = int(row["c"]) if row else 0
        return c >= int(SUGGEST_MAX_SUBMITS_PER_24H_PER_IP)


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
    """Atomically claim one queued job and mark it running."""
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as conn:
        conn.execute("BEGIN")
        r = conn.execute(
            """
            SELECT id, username_raw, ip_hash
            FROM suggest_jobs
            WHERE status = 'queued'
            ORDER BY id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ).fetchone()
        if not r:
            conn.execute("COMMIT")
            return None
        job_id = int(r["id"])
        conn.execute(
            "UPDATE suggest_jobs SET status = 'running', updated_at = %s WHERE id = %s",
            (now, job_id),
        )
        conn.execute("COMMIT")
        return {"id": job_id, "username_raw": r["username_raw"], "ip_hash": r["ip_hash"]}


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
        "WHERE p.visible = TRUE" + ds + hs + " ORDER BY LOWER(p.username), p.posted_at DESC"
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


def _username_clause(username: str | None) -> tuple[str, list[Any]]:
    if not username or not str(username).strip():
        return "", []
    return " AND p.username = %s", [username.strip()]


def _search_clause(search: str | None) -> tuple[str, list[Any]]:
    """Substring match: username, profile full_name, or post JSON (performers / caption)."""
    if not search or not str(search).strip():
        return "", []
    raw = " ".join(str(search).strip().split())
    if len(raw) > 120:
        raw = raw[:120]
    for ch in ("%", "_"):
        raw = raw.replace(ch, "")
    if not raw:
        return "", []
    pat = f"%{raw.lower()}%"
    return (
        " AND (LOWER(p.username) LIKE %s "
        "OR LOWER(COALESCE(a.profile_json->>'full_name', '')) LIKE %s "
        "OR LOWER(p.post_json::text) LIKE %s)",
        [pat, pat, pat],
    )


def _role_clause(performer_role: str | None) -> tuple[str, list[Any]]:
    """Match a role inside JSONB array stored in performer_roles."""
    if not performer_role or performer_role not in _VALID_ROLES:
        return "", []
    return (
        " AND p.performer_roles IS NOT NULL AND p.performer_roles @> %s"
    ), [Jsonb([performer_role])]


def _where_visible_and_filters(
    date_from: date | None,
    date_to: date | None,
    has_price: bool | None,
    weekday: str | None,
    username: str | None,
    performer_role: str | None = None,
    search: str | None = None,
) -> tuple[str, list[Any]]:
    ds, dargs = _date_clause(date_from, date_to)
    ps, pargs = _has_price_clause(has_price)
    ws, wargs = _weekday_clause(weekday)
    us, uargs = _username_clause(username)
    rs, rargs = _role_clause(performer_role)
    ss, sargs = _search_clause(search)
    hs, hargs = _hidden_exclude_posts_sql("p")
    return (
        " WHERE p.visible = TRUE" + ds + ps + ws + us + rs + ss + hs,
        dargs + pargs + wargs + uargs + rargs + sargs + hargs,
    )


# With date/price filters: chronological event_date, then lowest price (nulls last).
_ORDER_FLAT = (
    " ORDER BY "
    "CASE WHEN p.event_date IS NULL THEN 1 ELSE 0 END ASC, "
    "p.event_date ASC, "
    "CASE WHEN p.ticket_price_mkd IS NULL THEN 1 ELSE 0 END ASC, "
    "p.ticket_price_mkd ASC"
)

# No filters: newest Instagram post first (posted_at from scrape).
_ORDER_BY_POSTED = (
    " ORDER BY "
    "CASE WHEN p.posted_at IS NULL THEN 1 ELSE 0 END ASC, "
    "p.posted_at DESC"
)


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
    """All visible posts whose AI event_date overlaps [date_from, date_to] (same rules as date filter)."""
    where_sql, args = _where_visible_and_filters(
        date_from, date_to, None, None, None, None, None
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
        post = json.loads(r["post_json"])
        media = pick_media_for_post(post)
        if not media:
            continue
        prof = json.loads(r["profile_json"] or "{}")
        un = r["username"]
        display_name = prof.get("full_name") or un.replace(".", " ").title()
        out.append(
            {
                "username": un,
                "display_name": display_name,
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
) -> int:
    where_sql, args = _where_visible_and_filters(
        date_from, date_to, has_price, weekday, username, performer_role, search
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
) -> tuple[list[dict], int]:
    """
    Flat list of visible posts with media.
    If filters_active: sort by event_date then price.
    Else: sort by Instagram posted_at (newest first).
    Each entry: username, display_name, post, media (single picked image/video).
    """
    where_sql, args = _where_visible_and_filters(
        date_from, date_to, has_price, weekday, username, performer_role, search
    )
    total = count_flat_events(
        date_from, date_to, has_price, weekday, username, performer_role, search
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
        display_name = prof.get("full_name") or un.replace(".", " ").title()
        out.append(
            {
                "username": un,
                "display_name": display_name,
                "post": post,
                "media": media,
            }
        )
    return out, total


if __name__ == "__main__":
    import sys

    if not CATALOG_JSON_PATH.is_file():
        print(f"Missing {CATALOG_JSON_PATH}", file=sys.stderr)
        sys.exit(1)
    ac, pc = sync_from_json()
    print(f"Synced {CATALOG_JSON_PATH} -> Postgres (DATABASE_URL)")
    print(f"Accounts: {ac}  Posts: {pc}")
