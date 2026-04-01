"""
Night-out gallery: Skopje nightlife discovery — Cloudinary images, Gemini details, relative post age.

Data store: SQLite at back-end/data/nightlife_catalog.db. Date filter uses AI event_date
(Gemini), not Instagram post time. Ingest source: back-end/data/cloudinary_catalog.json —
run `python catalog_db.py` after scraping
to refresh the DB (or delete the .db to auto-import on next app start if JSON exists).

Run (project root):
  .venv\\Scripts\\python gallery_app.py
Open http://127.0.0.1:5001
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import os
import secrets

from flask import Flask, abort, jsonify, render_template, request, session

from catalog_db import (
    account_exists,
    ensure_database,
    fetch_flat_events_filtered,
    list_usernames,
    random_weekend_surprise_entries,
    suggest_has_pending_job,
    suggest_job_create,
    suggest_job_get,
    suggest_global_monthly_remaining,
    suggest_global_monthly_try_consume,
    suggest_log_submission,
    suggest_window_info,
    username_in_catalog,
)
from suggest_pipeline import client_ip_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
ensure_database()

PAGE_SIZE = 8

_TIME_HM = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$")
_EVENT_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def event_start_skopje(post: dict) -> datetime | None:
    """Naive/local-aware start in Europe/Skopje from AI event_date + start_time (default 21:00)."""
    ca = post.get("caption_analysis")
    if not isinstance(ca, dict):
        return None
    raw_ed = ca.get("event_date")
    if raw_ed is None:
        return None
    m = _EVENT_DAY.search(str(raw_ed).strip())
    if not m:
        return None
    try:
        d = date.fromisoformat(m.group(0))
    except ValueError:
        return None
    z = ZoneInfo("Europe/Skopje")
    for key in ("start_time", "event_start_time"):
        st = ca.get(key)
        if st is None:
            continue
        s = str(st).strip()
        if not s or s.lower() == "null":
            continue
        tm = _TIME_HM.match(s)
        if tm:
            h, mi = int(tm.group(1)), int(tm.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return datetime(d.year, d.month, d.day, h, mi, 0, tzinfo=z)
    return datetime(d.year, d.month, d.day, 21, 0, 0, tzinfo=z)


@app.template_filter("event_countdown_iso")
def event_countdown_iso_filter(post):
    dt = event_start_skopje(post) if isinstance(post, dict) else None
    return dt.isoformat() if dt else ""


def format_posted_ago(iso_ts: str | None) -> str:
    """Macedonian relative time for Instagram post timestamp (UTC)."""
    if not iso_ts or not str(iso_ts).strip():
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    sec = delta.total_seconds()
    if sec < 0:
        return "Наскоро ✨"
    mins = int(sec // 60)
    if mins < 1:
        return "Штотуку"
    if mins < 60:
        return f"Пред {mins} мин"
    h = int(sec // 3600)
    d = delta.days
    if h < 24 and d == 0:
        return f"Пред {h} ч."
    if d == 1:
        return "Вчера"
    if d < 7:
        return f"Пред {d} дена"
    if d < 30:
        w = d // 7
        if w <= 1:
            return "Пред 1 недела"
        return f"Пред {w} недели"
    if d < 365:
        mo = max(1, round(d / 30))
        return f"Пред ~{mo} мес."
    y = d // 365
    if y <= 1:
        return "Пред 1 година"
    return f"Пред {y} години"


@app.template_filter("posted_ago")
def posted_ago_filter(iso_ts):
    s = format_posted_ago(iso_ts)
    return f"{s} објавено" if s else ""


@app.template_filter("event_date_mk")
def event_date_mk_filter(value):
    """Show YYYY-MM-DD as DD.MM.YYYY (common in MK); keeps ISO sort in DB."""
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            d = date.fromisoformat(s[:10])
            return f"{d.day:02d}.{d.month:02d}.{d.year}"
        except ValueError:
            pass
    return s


def parse_date_range_args() -> tuple[date | None, date | None, str, str]:
    """Read ?date_from=&date_to= (YYYY-MM-DD). Returns (from, to, raw_from, raw_to) for forms."""
    raw_f = request.args.get("date_from", "").strip()
    raw_t = request.args.get("date_to", "").strip()
    d_from: date | None = None
    d_to: date | None = None
    if raw_f:
        try:
            d_from = date.fromisoformat(raw_f)
        except ValueError:
            pass
    if raw_t:
        try:
            d_to = date.fromisoformat(raw_t)
        except ValueError:
            pass
    if d_from is not None and d_to is not None and d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to, raw_f, raw_t


def parse_has_price_arg() -> tuple[bool | None, str]:
    """?has_price=1 → only events with ticket_price_mkd set."""
    raw = request.args.get("has_price", "").strip()
    if raw == "1":
        return True, "1"
    return None, ""


WEEKDAY_VALUES = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)


def parse_weekday_arg() -> tuple[str | None, str]:
    """?dow=monday|…|sunday — weekday of AI event_date."""
    raw = request.args.get("dow", "").strip().lower()
    if not raw or raw not in WEEKDAY_VALUES:
        return None, ""
    return raw, raw


PERFORMER_ROLE_VALUES = frozenset(
    {"dj", "singer", "live_band", "artist", "mc", "unknown"}
)


def parse_performer_role_arg() -> tuple[str | None, str]:
    """Read ?role= (Gemini performer role). Empty = any."""
    raw = request.args.get("role", "").strip().lower()
    if not raw:
        return None, ""
    if raw not in PERFORMER_ROLE_VALUES:
        return None, ""
    return raw, raw


def parse_search_arg() -> tuple[str | None, str]:
    """Read ?q= — username, venue name, or text in post JSON (performers, caption)."""
    raw = request.args.get("q", "").strip()
    if not raw:
        return None, ""
    raw = " ".join(raw.split())
    if len(raw) > 120:
        raw = raw[:120]
    return raw, raw


def filters_active(
    d_from: date | None,
    d_to: date | None,
    has_price: bool | None,
    weekday: str | None,
    role: str | None,
) -> bool:
    """True when any filter is active (URL). Search alone does not switch sort order."""
    return (
        d_from is not None
        or d_to is not None
        or has_price is True
        or (weekday is not None and weekday != "")
        or (role is not None and role != "")
    )


def closest_weekend_fri_sun() -> tuple[date, date]:
    """Current Fri–Sun window in Europe/Skopje (this weekend if today is Fri–Sun, else next)."""
    z = ZoneInfo("Europe/Skopje")
    today = datetime.now(z).date()
    wd = today.weekday()
    if wd == 4:
        fri = today
    elif wd == 5:
        fri = today - timedelta(days=1)
    elif wd == 6:
        fri = today - timedelta(days=2)
    else:
        fri = today + timedelta(days=4 - wd)
    sun = fri + timedelta(days=2)
    return fri, sun


def combined_filter_query_suffix(
    raw_f: str,
    raw_t: str,
    raw_has_price: str = "",
    raw_dow: str = "",
    raw_role: str = "",
    raw_q: str = "",
) -> str:
    parts: list[str] = []
    if raw_f.strip():
        parts.append(f"date_from={quote(raw_f.strip(), safe='')}")
    if raw_t.strip():
        parts.append(f"date_to={quote(raw_t.strip(), safe='')}")
    if raw_has_price.strip() == "1":
        parts.append("has_price=1")
    if raw_dow.strip():
        parts.append(f"dow={quote(raw_dow.strip().lower(), safe='')}")
    if raw_role.strip():
        parts.append(f"role={quote(raw_role.strip().lower(), safe='')}")
    if raw_q.strip():
        parts.append(f"q={quote(raw_q.strip(), safe='')}")
    return f"?{'&'.join(parts)}" if parts else ""


@app.route("/")
def index():
    d_from, d_to, raw_f, raw_t = parse_date_range_args()
    has_price, raw_has_price = parse_has_price_arg()
    weekday, raw_dow = parse_weekday_arg()
    role, raw_role = parse_performer_role_arg()
    search, raw_q = parse_search_arg()
    fa = filters_active(d_from, d_to, has_price, weekday, role)
    entries, total_events = fetch_flat_events_filtered(
        date_from=d_from,
        date_to=d_to,
        has_price=has_price,
        weekday=weekday,
        username=None,
        offset=0,
        limit=PAGE_SIZE,
        filters_active=fa,
        performer_role=role,
        search=search,
    )
    q_suffix = combined_filter_query_suffix(
        raw_f, raw_t, raw_has_price, raw_dow, raw_role, raw_q
    )
    has_accounts = bool(list_usernames())
    has_more = total_events > len(entries)
    return render_template(
        "home.html",
        entries=entries,
        total_events=total_events,
        has_more=has_more,
        page_size=PAGE_SIZE,
        feed_mode=False,
        date_from=raw_f,
        date_to=raw_t,
        has_price=raw_has_price,
        dow=raw_dow,
        role=raw_role,
        filter_active=bool(
            raw_f
            or raw_t
            or raw_has_price == "1"
            or raw_dow
            or raw_role
            or raw_q
        ),
        date_query_suffix=q_suffix,
        has_accounts=has_accounts,
        search_q=raw_q,
    )


@app.route("/api/events")
def api_events():
    d_from, d_to, raw_f, raw_t = parse_date_range_args()
    has_price, raw_has_price = parse_has_price_arg()
    weekday, raw_dow = parse_weekday_arg()
    role, raw_role = parse_performer_role_arg()
    search, raw_q = parse_search_arg()
    fa = filters_active(d_from, d_to, has_price, weekday, role)
    username = request.args.get("user", "").strip() or None
    offset = request.args.get("offset", 0, type=int) or 0
    limit = min(request.args.get("limit", PAGE_SIZE, type=int) or PAGE_SIZE, 50)
    entries, total_events = fetch_flat_events_filtered(
        date_from=d_from,
        date_to=d_to,
        has_price=has_price,
        weekday=weekday,
        username=username,
        offset=offset,
        limit=limit,
        filters_active=fa,
        performer_role=role,
        search=search,
    )
    q_suffix = combined_filter_query_suffix(
        raw_f, raw_t, raw_has_price, raw_dow, raw_role, raw_q
    )
    html = render_template(
        "_event_cards_fragment.html",
        entries=entries,
        date_query_suffix=q_suffix,
        feed_mode=bool(username),
    )
    has_more = offset + len(entries) < total_events
    return jsonify(
        {
            "html": html,
            "has_more": has_more,
            "next_offset": offset + len(entries),
        }
    )


@app.route("/weekend")
def weekend_surprise():
    """Three random AI-dated events for the closest Fri–Sun (Europe/Skopje)."""
    fri, sun = closest_weekend_fri_sun()
    surprise_entries = random_weekend_surprise_entries(fri, sun, 3)
    return render_template(
        "weekend.html",
        nav="weekend",
        surprise_entries=surprise_entries,
        weekend_fri=fri,
        weekend_sun=sun,
        date_query_suffix="",
        feed_mode=False,
    )


@app.route("/contact")
def contact():
    return render_template("contact.html", nav="contact")


@app.route("/suggest", methods=["GET", "POST"])
def suggest_account():
    """User-submitted Instagram username is enqueued; worker processes scrape+Gemini."""
    if request.method == "GET":
        tok = secrets.token_urlsafe(24)
        session["csrf_suggest"] = tok
        used, reset_at = suggest_window_info(
            client_ip_hash(request.remote_addr, request.headers.get("X-Forwarded-For"))
        )
        return render_template(
            "suggest.html",
            nav="suggest",
            result=None,
            form_username="",
            job_id=None,
            csrf_token=tok,
            tries_used=used,
            tries_max=2,
            reset_at_iso=reset_at.isoformat() if reset_at else "",
            global_remaining=suggest_global_monthly_remaining(),
        )

    raw = (request.form.get("username") or "").strip()
    hp = (request.form.get("website") or "").strip()
    tok = (request.form.get("csrf_token") or "").strip()

    ip_h = client_ip_hash(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
    )

    def _render_err(msg: str):
        new_tok = secrets.token_urlsafe(24)
        session["csrf_suggest"] = new_tok
        used, reset_at = suggest_window_info(ip_h)
        return render_template(
            "suggest.html",
            nav="suggest",
            result={"ok": False, "code": "err", "message": msg},
            form_username=raw,
            job_id=None,
            csrf_token=new_tok,
            tries_used=used,
            tries_max=2,
            reset_at_iso=reset_at.isoformat() if reset_at else "",
            global_remaining=suggest_global_monthly_remaining(),
        )

    if not ip_h:
        return _render_err("Не можеме да ја потврдиме сесијата. Обиди се повторно подоцна.")
    if hp:
        return _render_err("Грешка. Обиди се повторно.")
    if not tok or tok != session.get("csrf_suggest"):
        return _render_err("Сесијата истече. Освежи ја страната и обиди се повторно.")

    if suggest_has_pending_job(ip_h):
        return _render_err("Веќе имаш барање во обработка.")

    handle = raw.lstrip("@").strip()
    if not handle:
        return _render_err("Внеси валидно корисничко име.")

    if username_in_catalog(handle):
        return render_template(
            "suggest.html",
            nav="suggest",
            result={"ok": False, "code": "exists", "message": "Веќе е додадено."},
            form_username="",
            job_id=None,
            csrf_token=secrets.token_urlsafe(24),
            tries_used=0,
            tries_max=2,
            reset_at_iso="",
            global_remaining=suggest_global_monthly_remaining(),
        )

    used, reset_at = suggest_window_info(ip_h)
    if used >= 2 and reset_at is not None:
        return render_template(
            "suggest.html",
            nav="suggest",
            result={"ok": False, "code": "cooldown", "message": "Пробај подоцна."},
            form_username="",
            job_id=None,
            csrf_token=secrets.token_urlsafe(24),
            tries_used=used,
            tries_max=2,
            reset_at_iso=reset_at.isoformat(),
            global_remaining=suggest_global_monthly_remaining(),
        )

    # Global monthly cap (does not count "already in DB", checked above).
    if not suggest_global_monthly_try_consume():
        return render_template(
            "suggest.html",
            nav="suggest",
            result={
                "ok": False,
                "code": "global_cap",
                "message": "Лимитот за овој месец е достигнат. Прати ни профил на email.",
            },
            form_username="",
            job_id=None,
            csrf_token=secrets.token_urlsafe(24),
            tries_used=used,
            tries_max=2,
            reset_at_iso=reset_at.isoformat() if reset_at else "",
            global_remaining=0,
        )

    suggest_log_submission(ip_h)
    job_id = suggest_job_create(raw, ip_h)

    new_tok = secrets.token_urlsafe(24)
    session["csrf_suggest"] = new_tok
    return render_template(
        "suggest.html",
        nav="suggest",
        result={"ok": True, "code": "pending", "message": "Pending"},
        form_username="",
        job_id=job_id,
        csrf_token=new_tok,
        tries_used=used + 1,
        tries_max=2,
        reset_at_iso=(reset_at.isoformat() if reset_at else (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()),
        global_remaining=suggest_global_monthly_remaining(),
    )


@app.route("/api/suggest_status/<int:job_id>")
def suggest_status(job_id: int):
    j = suggest_job_get(job_id)
    if not j:
        return jsonify({"ok": False, "status": "missing"}), 404
    used, reset_at = suggest_window_info(str(j.get("ip_hash") or ""))
    return jsonify(
        {
            "ok": True,
            "status": j.get("status"),
            "done": j.get("status") == "done",
            "result_ok": bool(j.get("ok")) if j.get("ok") is not None else None,
            "message": j.get("message") or "",
            "canonical_username": j.get("canonical_username") or "",
            "tries_used": used,
            "tries_max": 2,
            "reset_at_iso": reset_at.isoformat() if reset_at else "",
        }
    )


@app.route("/u/<path:username>")
def by_user(username):
    if not account_exists(username):
        abort(404)
    d_from, d_to, raw_f, raw_t = parse_date_range_args()
    has_price, raw_has_price = parse_has_price_arg()
    weekday, raw_dow = parse_weekday_arg()
    role, raw_role = parse_performer_role_arg()
    search, raw_q = parse_search_arg()
    fa = filters_active(d_from, d_to, has_price, weekday, role)
    entries, total_events = fetch_flat_events_filtered(
        date_from=d_from,
        date_to=d_to,
        has_price=has_price,
        weekday=weekday,
        username=username,
        offset=0,
        limit=PAGE_SIZE,
        filters_active=fa,
        performer_role=role,
        search=search,
    )
    q_suffix = combined_filter_query_suffix(
        raw_f, raw_t, raw_has_price, raw_dow, raw_role, raw_q
    )
    has_more = total_events > len(entries)
    return render_template(
        "feed.html",
        entries=entries,
        total_events=total_events,
        has_more=has_more,
        page_size=PAGE_SIZE,
        feed_mode=True,
        active_user=username,
        date_from=raw_f,
        date_to=raw_t,
        has_price=raw_has_price,
        dow=raw_dow,
        role=raw_role,
        filter_active=bool(
            raw_f
            or raw_t
            or raw_has_price == "1"
            or raw_dow
            or raw_role
            or raw_q
        ),
        date_query_suffix=q_suffix,
        search_q=raw_q,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
