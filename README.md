# NightLife MK

A nightlife discovery site for Skopje and North Macedonia. It collects posts from the Instagram accounts of clubs, bars, kafanas and concert venues. Gemini reads each caption and poster and pulls out the event details (date, time, performers, ticket price, reservations phone, venue type), and the site shows everything as one filterable event feed in Macedonian.

Visitors can:

- browse upcoming events, soonest first, and filter them by date range, weekday, venue type, performer role (DJ, singer, live band…), "has ticket price", or free-text search
- open a single venue's feed at `/u/<instagram_username>`
- get three random events for the coming weekend at `/weekend`
- save events in the browser and download them as `.ics` calendar files
- suggest a new Instagram account at `/suggest`, which is scraped, checked by AI and added automatically if it qualifies
- install the site as a PWA (home-screen app)

---

## Table of contents

1. [Architecture](#architecture)
2. [Tech stack and external services](#tech-stack-and-external-services)
3. [Project structure](#project-structure)
4. [Data pipeline](#data-pipeline)
5. [Database](#database)
6. [The "Suggest an account" flow](#the-suggest-an-account-flow)
7. [Web app routes](#web-app-routes)
8. [Environment variables](#environment-variables)
9. [Running locally](#running-locally)
10. [Hosting on Railway](#hosting-on-railway)
11. [Admin and maintenance scripts](#admin-and-maintenance-scripts)
12. [Tricks and implementation notes](#tricks-and-implementation-notes)
13. [Security notes](#security-notes)

---

## Architecture

```mermaid
flowchart LR
    IG[(Instagram)] -->|RapidAPI instagram120| SCR[Scraper<br/>scrape_rapidapi_cloudinary.py]
    SCR -->|upload images| CLD[(Cloudinary CDN)]
    SCR -->|posts + profile| JSON[cloudinary_catalog.json]
    JSON --> GEM[Gemini analyzer<br/>analyze_captions_gemini.py]
    GEM -->|caption_analysis| JSON
    JSON -->|catalog_db.py sync| PG[(Postgres)]

    USER((Visitor)) --> WEB[Flask web<br/>gallery_app.py]
    WEB -->|read events| PG
    WEB -->|enqueue suggest job| PG
    WORKER[worker_suggest.py] -->|claim job| PG
    WORKER -->|scrape + Gemini| SCR
    WORKER -->|upsert account| PG
    CRON[weekly_refresh.py<br/>cron] -->|rescrape + reanalyze all| SCR
    USER -. images .-> CLD
```

Production runs as **three processes that share one Postgres database**:

| Process | Command | Role |
|---|---|---|
| **Web** | `gunicorn gallery_app:app` | Serves the site and JSON endpoints and puts suggestions on the queue |
| **Worker** | `python worker_suggest.py` | Long-running loop that handles queued suggestions one at a time |
| **Cron** | `python weekly_refresh.py` | Weekly job: rescrape every account, rerun Gemini, re-sync the DB |

---

## Tech stack and external services

| Piece | What it is used for |
|---|---|
| **Python 3.10+ / Flask 3** | Web app with Jinja2 templates rendered on the server |
| **Gunicorn** | Production WSGI server |
| **PostgreSQL** (via `psycopg` 3) | Main data store. Full post data is kept as `JSONB`, and the filterable fields get their own columns |
| **RapidAPI – `instagram120`** | Fetches the latest posts and profile info for an Instagram username. A second key (`RAPIDAPI_KEY_V2`) is tried if the first one fails |
| **Cloudinary** | Stores and serves the post images. Instagram CDN URLs expire, so every image is copied to Cloudinary when it is scraped |
| **Google Gemini** (`google-genai`, default model `gemini-3.1-flash-lite-preview`) | Turns each caption, plus the first photo, into structured JSON. Also decides whether an account is in North Macedonia and whether it is a nightlife account |
| **Apify** (legacy, `extract_users.py`) | Old scraper, replaced by RapidAPI. Kept for reference |
| **Railway** | Hosting for the web app, worker, cron and Postgres |

---

## Project structure

```
.
├── gallery_app.py              # Flask app: routes, filters, template helpers, PWA manifest
├── catalog_db.py               # Postgres schema + every DB query; also a CLI (sync / export / refresh)
├── suggest_pipeline.py         # One suggestion end to end: scrape → DB → Gemini → accept/reject
├── worker_suggest.py           # Background worker that runs suggest jobs from the queue
├── weekly_refresh.py           # Cron entry point: rescrape + reanalyze + sync
├── reset_suggest_ip.py         # Dev helper: clear suggest limits for one IP
├── requirements.txt
├── scripts/
│   ├── clear_suggest_jobs.py   # Delete every suggest job (unblocks a stuck "pending" UI)
│   ├── remove_account.py       # Remove one account from Postgres + JSON
│   ├── rapidapi_dump_username.py  # Save the raw RapidAPI response for debugging
│   └── show_suggest_ip_hash.py # Compute the ip_hash the app would store for an IP
├── back-end/
│   ├── scraping/
│   │   ├── scrape_rapidapi_cloudinary.py  # Main scraper (RapidAPI → Cloudinary → JSON)
│   │   ├── debug_rapidapi_posts.py        # Print the raw RapidAPI JSON
│   │   └── extract_users.py               # Legacy Apify scraper
│   ├── AI-Summarization/
│   │   ├── analyze_captions_gemini.py     # Gemini caption analysis + MK/nightlife classifiers
│   │   └── caption_analysis.example.json
│   ├── data/
│   │   ├── cloudinary_catalog.json        # Catalog snapshot (seed / backup of the DB)
│   │   └── scrape_usernames.txt           # Accounts to scrape + last/next scrape times
│   └── database-adding-content/
│       └── .env                           # Local secrets (git-ignored)
├── templates/                  # Jinja2 pages (home, feed, weekend, suggest, contact) + partials (_*.html)
└── static/
    ├── sw.js                   # Minimal service worker (so the site can be installed as a PWA)
    └── icons/                  # PWA icons (normal + maskable)
```

---

## Data pipeline

### 1. Scraping: `back-end/scraping/scrape_rapidapi_cloudinary.py`

- Reads its targets from `back-end/data/scrape_usernames.txt` (`username | last_scraped_at_utc | next_rescrape_due_utc`), or from every row of the `accounts` table when run with `--from-db`.
- For each username it calls RapidAPI `POST /api/instagram/posts` and takes the **latest 5 posts** (`POSTS_LIMIT`) plus the profile (full name, followers, profile picture).
- It downloads each photo and uploads it to Cloudinary. **Reels and videos use the profile picture as a placeholder image**, because only a still image is needed.
- It saves `cloudinary_catalog.json` after **every** successful username, so you can stop it with Ctrl+C and resume later.
- Each account is rescraped at most once every **7 days** (`SCRAPE_INTERVAL_DAYS`) unless you pass `--force`. Failed accounts are retried (`--retries 5`, `--retry-delay 10`) and then skipped.

```bash
python back-end/scraping/scrape_rapidapi_cloudinary.py            # only accounts that are due
python back-end/scraping/scrape_rapidapi_cloudinary.py --force    # all accounts in the TXT file
python back-end/scraping/scrape_rapidapi_cloudinary.py --force --from-db   # all accounts in Postgres
```

### 2. AI analysis: `back-end/AI-Summarization/analyze_captions_gemini.py`

For each post, Gemini gets the caption, the post's publish time (so it can resolve words like "this Saturday" to a real date) and the **first photo**, because many events are announced only on the poster. It returns strict JSON (`caption_analysis`, schema v3):

```json
{
  "schema_version": 3,
  "ticket_price_mkd": 300,
  "performers": [{"name": "DJ Ficho", "role": "dj"}],
  "day_of_week": "saturday",
  "event_date": "2026-03-28",
  "start_time": "00:00",
  "end_time": null,
  "reservations_phone": "071317338",
  "reservations_url": null,
  "location": "PURE Club, Градски парк, Skopje",
  "city_mk": "Скопје",
  "venue_category": "nightclub",
  "listing_type": "nightlife_event"
}
```

`listing_type` is either `nightlife_event` or `not_nightlife`. **Only `nightlife_event` posts are shown on the site**, so food photos and generic ads are hidden.

```bash
python back-end/AI-Summarization/analyze_captions_gemini.py             # only posts without an analysis
python back-end/AI-Summarization/analyze_captions_gemini.py --force     # reanalyze everything
python back-end/AI-Summarization/analyze_captions_gemini.py -u bistro.komedija   # one account (also upserts it into Postgres)
python back-end/AI-Summarization/analyze_captions_gemini.py --force --mk-only    # also drop accounts that aren't in North Macedonia, then sync the DB
```

### 3. Load into Postgres: `catalog_db.py`

```bash
python catalog_db.py        # TRUNCATE posts/accounts and reload them from cloudinary_catalog.json
```

> ⚠️ A plain sync **replaces** the database with the JSON file. Accounts that exist only in Postgres (for example, ones added through `/suggest`) are **deleted** if they are missing from the JSON file. Run `python catalog_db.py --export-json` first to copy the DB back into the JSON file.

### 4. Weekly refresh: `weekly_refresh.py`

Runs steps 1, 2 and 3 in a row: `scrape --force --from-db`, then `gemini --force --mk-only`, then `catalog_db.py`. It uses a lock file (`back-end/data/weekly_refresh.lock`) so two runs can't overlap. Optional environment variables:

- `FORCE_GEMINI=0`: analyze only posts that don't have an analysis yet (the default reanalyzes everything)
- `MK_FILTER=0`: skip the North Macedonia account filter

---

## Database

The schema lives in `catalog_db.py` (`SCHEMA`) and is **created automatically** with `CREATE TABLE IF NOT EXISTS` whenever a connection is opened. There are no migration tools. New columns are added with `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.

### Tables

| Table | Purpose |
|---|---|
| `accounts` | One row per Instagram account: `username` (PK) and `profile_json` (JSONB: name, followers, avatar, `display_handle`) |
| `posts` | One row per post. `post_json` (JSONB) holds the whole post (caption, media, Cloudinary URLs, `caption_analysis`). Frequently filtered values are copied into their own indexed columns: `event_date`, `event_date_end`, `posted_at`, `posted_date`, `ticket_price_mkd`, `listing_type`, `performer_roles` (JSONB), `venue_category`, `visible`. Unique on `(username, post_index)`. `ON DELETE CASCADE` from `accounts` |
| `suggest_jobs` | Queue of suggested accounts: `status` = `queued` → `running` → `done`, plus `ok`, `message`, `canonical_username`, `ip_hash` |
| `suggest_submit_log` | One row per suggestion, used for the per-IP limit |
| `suggest_global_monthly` | `ym` (`YYYY-MM`) → `used`. Site-wide monthly limit on suggestions |
| `suggest_submit_blocks` | Old per-IP block table, kept for compatibility |

### How it is used

- **JSONB plus copied columns.** The full post is kept as JSONB so templates can render any field. The columns used in `WHERE` and `ORDER BY` are extracted when a row is written (`upsert_post_row`), so filtering stays fast and indexed.
- **Recomputing derived columns** after a rule change, without scraping again:
  - `python catalog_db.py --refresh-visible` recalculates `visible` (true only when `listing_type = nightlife_event`)
  - `python catalog_db.py --refresh-venue` recalculates `venue_category`. When Gemini didn't set one, it is guessed from keywords (`kafana`/`кафана` → kafana, `pub`/`saloon` → bar_pub, `mkc`/`филхармонија` → concert_venue, …)
- **Feed order:** upcoming events first (soonest `event_date` first, then by like count), then past or undated posts, newest first.
- **First start:** `ensure_database()` runs when the app imports. If `posts` is empty and `cloudinary_catalog.json` exists, it imports the JSON file automatically.
- **Hidden accounts:** `HIDDEN_FROM_FEED_USERNAMES` in `catalog_db.py` removes accounts from every listing, and their `/u/<name>` page returns 404.

### Backup and restore

```bash
python catalog_db.py --export-json                 # Postgres → back-end/data/cloudinary_catalog.json
python catalog_db.py --export-json backup.json     # Postgres → any file
python catalog_db.py                               # JSON → Postgres (replaces the tables)
```

---

## The "Suggest an account" flow

The page response returns immediately, and the slow work (scraping plus several Gemini calls, which takes a minute or two) runs in the worker.

1. **`POST /suggest`** (web). Checks, in order: CSRF token, honeypot field (`website`, which must stay empty), valid username, not already in the catalog, **at most 2 suggestions per 12 hours per IP** (`SUGGEST_MAX_PER_WINDOW`, `SUGGEST_WINDOW`), **one pending job per IP at a time**, and a **global limit of 350 per month** (`SUGGEST_GLOBAL_MONTHLY_LIMIT`), which caps RapidAPI and Gemini costs. If everything passes, a `suggest_jobs` row is inserted with `status = 'queued'`.
2. **Browser polling.** The page calls `GET /api/suggest_status/<id>` every few seconds and shows the result. `POST /api/suggest_ack/<id>` clears the job from the session.
3. **`worker_suggest.py`** claims the oldest queued job and runs `suggest_pipeline.process_user_suggestion`:
   1. scrape the account with RapidAPI and upload to Cloudinary
   2. upsert the account and posts into Postgres (this flow **does not** touch the JSON file)
   3. run Gemini on every post
   4. ask Gemini whether the account is **in North Macedonia**, using the name and 3 recent captions. If not, roll back (delete the account)
   5. accept the account if any post is a nightlife event, has an event date or lists performers. Otherwise ask Gemini about the 3 recent captions, then about the profile name alone. If none of these pass, roll back
4. The worker marks the job `done` with `ok` and a message in Macedonian, which the polling page then shows.

**Stale jobs:** every 20 seconds the worker marks as failed any job that stayed `queued` for more than 2 hours or `running` for more than 6 hours (for example after a crash), so no visitor is stuck on "pending" forever.

---

## Web app routes

| Route | Description |
|---|---|
| `GET /` | Home feed (8 per page, more load on scroll) with filters |
| `GET /u/<username>` | Feed for one account |
| `GET /weekend` | 3 random events on the nearest Friday–Sunday (Europe/Skopje time) |
| `GET /suggest`, `POST /suggest` | Suggest an Instagram account |
| `GET /contact` | Contact page |
| `GET /api/events` | JSON `{html, has_more, next_offset}` used for infinite scroll. Accepts the same filters plus `user`, `offset`, `limit` (max 50) |
| `GET /api/suggest_status/<id>`, `POST /api/suggest_ack/<id>` | Suggest job polling |
| `GET /manifest.webmanifest`, `GET /sw.js` | PWA manifest and service worker |

**Filter query parameters:** `date_from`, `date_to` (YYYY-MM-DD, compared with the AI `event_date`, **not** the Instagram post date), `dow` (`monday`…`sunday`), `has_price=1`, `role` (`dj`, `singer`, `live_band`, `artist`, `mc`), `venue` (`nightclub`, `bar_pub`, `kafana`, `cafe`, `restaurant`, `concert_venue`, `lounge_rooftop`, `festival_outdoor`, `hotel_resort`, `other_venue`), `q` (text search).

---

## Environment variables

Configuration is loaded with `python-dotenv` from **`back-end/database-adding-content/.env`** (the main local file) and from `./.env` in the project root. Both are git-ignored. In production, set the same variables in the Railway dashboard.

| Variable | Used by | Required | Notes |
|---|---|---|---|
| `DATABASE_URL` | all | ✅ (prod) | Postgres connection string. On Railway, use the private URL |
| `DATABASE_PUBLIC_URL` | all | ✅ (local) | Used when `DATABASE_URL` is empty, e.g. connecting from your laptop to Railway Postgres through its public proxy |
| `FLASK_SECRET_KEY` | web | ✅ | Signs session cookies (CSRF token, suggest job id). If missing, a random key is generated at startup and all sessions reset on every restart or differ between gunicorn workers |
| `RAPIDAPI_KEY` | scraper, worker, cron | ✅ | RapidAPI key subscribed to `instagram120` |
| `RAPIDAPI_KEY_V2` | scraper, worker, cron | optional | Backup key used when the first one errors |
| `CLOUDINARY_URL` | scraper, worker, cron | ✅* | `cloudinary://<api_key>:<api_secret>@<cloud_name>` |
| `CLOUD_NAME`, `API_KEY`, `API_SECRET` | scraper | ✅* | Alternative to `CLOUDINARY_URL` (`CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` also work) |
| `GEMINI_API_KEY` | analyzer, worker, cron | ✅ | Google AI Studio key |
| `GEMINI_MODEL` | analyzer | optional | Overrides the default `gemini-3.1-flash-lite-preview` |
| `SUGGEST_WORKER_VERBOSE` | worker | optional | `1` turns on INFO logs (idle heartbeats, job lifecycle) |
| `FORCE_GEMINI`, `MK_FILTER` | cron | optional | See [Weekly refresh](#4-weekly-refresh-weekly_refreshpy) |
| `APIFY_API_TOKEN` | legacy `extract_users.py` | only if you use it | |

\* Set either `CLOUDINARY_URL` or the three separate variables.

Example `back-end/database-adding-content/.env`:

```dotenv
DATABASE_PUBLIC_URL=postgresql://postgres:<password>@<host>.proxy.rlwy.net:<port>/railway
FLASK_SECRET_KEY=<long random string>
RAPIDAPI_KEY=<key>
RAPIDAPI_KEY_V2=<optional second key>
CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
GEMINI_API_KEY=<key>
```

Generate a secret key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

---

## Running locally

### 1. Prerequisites

- Python 3.10 or newer
- A Postgres database: either Railway's (use its public URL) or a local one:
  ```bash
  docker run -d --name nightlife-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
  # DATABASE_PUBLIC_URL=postgresql://postgres:dev@localhost:5432/postgres
  ```

### 2. Install

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Create `back-end/database-adding-content/.env` as shown in [Environment variables](#environment-variables). To only browse the site, `DATABASE_PUBLIC_URL` (or `DATABASE_URL`) and `FLASK_SECRET_KEY` are enough. Scraping and suggestions also need the RapidAPI, Cloudinary and Gemini keys.

### 4. Start the web app

```bash
python gallery_app.py
```

Open <http://127.0.0.1:5001>. On first start with an empty DB, `back-end/data/cloudinary_catalog.json` is imported automatically, so there is data right away.

### 5. (Optional) Start the suggest worker

In a second terminal, with the same `.env`:

```bash
python worker_suggest.py
```

Without it, suggestions stay "pending" and are marked failed after 2 hours.

### 6. (Optional) Refresh data manually

```bash
python back-end/scraping/scrape_rapidapi_cloudinary.py --force
python back-end/AI-Summarization/analyze_captions_gemini.py
python catalog_db.py
# or all three in one go:
python weekly_refresh.py
```

### Adding a venue by hand

1. Add a line with only the username to `back-end/data/scrape_usernames.txt`, e.g. `some.club.skopje`
2. `python back-end/scraping/scrape_rapidapi_cloudinary.py`
3. `python back-end/AI-Summarization/analyze_captions_gemini.py -u some.club.skopje`, which analyzes the account and upserts it into Postgres

---

## Hosting on Railway

The app is deployed on [Railway](https://railway.app) as one project: a Postgres plugin plus three services built from this repo. There is no Procfile. Start commands are set per service in the Railway dashboard, and the build (Nixpacks) detects Python from `requirements.txt`.

| Service | Start command | Notes |
|---|---|---|
| **Postgres** | (Railway plugin) | Provides `DATABASE_URL` (private network) and `DATABASE_PUBLIC_URL` |
| **web** | `gunicorn gallery_app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60` | Public domain, optionally a custom domain |
| **worker** | `python worker_suggest.py` | No public domain. **Must use the same `DATABASE_URL` as web**, otherwise jobs go into one DB and the worker reads another. The worker prints the Postgres host and DB name at startup so you can check |
| **cron** | `python weekly_refresh.py` | Railway Cron Schedule, e.g. `0 4 * * 1` (Mondays 04:00 UTC) |

Set the [environment variables](#environment-variables) on each service. Railway variable references work well for this, e.g. `DATABASE_URL=${{Postgres.DATABASE_URL}}`.

Hosting details handled in the code:

- `ProxyFix` is enabled, so the real client IP (`X-Forwarded-For`) and HTTPS scheme are seen behind Railway's proxy. This matters for the per-IP suggest limits.
- The PWA manifest uses host-relative paths, so installing works on both `*.up.railway.app` and a custom domain.
- The schema is created under a Postgres advisory lock, so several gunicorn workers and the worker service can start at the same time without deadlocking.

---

## Admin and maintenance scripts

Run these from the project root with the same `.env`/`DATABASE_URL` as production if you want to act on production.

| Command | What it does |
|---|---|
| `python catalog_db.py` | JSON → Postgres (replaces the tables) |
| `python catalog_db.py --export-json [path]` | Postgres → JSON backup |
| `python catalog_db.py --refresh-visible` | Recalculate `posts.visible` |
| `python catalog_db.py --refresh-venue` | Recalculate `posts.venue_category` |
| `python catalog_db.py remove-user <name>` | Remove an account from the catalog, its suggest jobs and the scrape list |
| `python scripts/remove_account.py <name>` | Remove an account from Postgres and the JSON file (`--list-like <text>` to search first) |
| `python scripts/clear_suggest_jobs.py` | Delete **all** suggest jobs (fixes a stuck "pending" state) |
| `python reset_suggest_ip.py <ip>` | Reset suggest limits for one IP (testing) |
| `python scripts/show_suggest_ip_hash.py [ip] [--xff "..."]` | Show the `ip_hash` the app would store |
| `python scripts/rapidapi_dump_username.py [user] [out.json]` | Save the raw RapidAPI response for debugging (dumps are git-ignored) |
| `python back-end/scraping/debug_rapidapi_posts.py [user]` | Print the raw RapidAPI JSON |

---

## Tricks and implementation notes

- **Dates come from the AI, not Instagram.** Venues post days before the event, so filters, sorting, countdowns and `.ics` export all use `caption_analysis.event_date` and `start_time`. When no time is given, **21:00** Europe/Skopje is assumed, and events without an end time last **2 hours**. Gemini gets the post's publish date so it can resolve "this Friday".
- **Posters are analyzed too.** The first photo is sent to Gemini together with the caption, because many venues put the lineup and price only on the image.
- **Images are copied to Cloudinary.** Instagram CDN links expire within days, so each image is uploaded to Cloudinary once and the site loads it from there.
- **Phone numbers are normalized.** Reservation numbers are reduced to digits, `+389…` becomes `0…`, and if the model joins two numbers, only the first 9 digits are kept.
- **Venue type fallback.** When Gemini returns `unknown`, `venue_category` is guessed from keywords in the handle, caption or location, so older rows still work with the venue filter.
- **Deadlock-safe queue.** Jobs are claimed with one `UPDATE … FROM (SELECT … FOR UPDATE SKIP LOCKED)`, stale-job cleanup is serialized with `pg_advisory_xact_lock`, and deadlocks are retried with random backoff.
- **Abuse protection without accounts.** IPs are stored only as SHA-256 hashes. The suggest form is protected by a CSRF token, a honeypot field, per-IP and site-wide limits, and one pending job per IP. A session-stored job id is also accepted, because `X-Forwarded-For` can change between requests on mobile networks.
- **In-app browser banner.** Links opened inside Instagram or Facebook show a banner asking the visitor to open the site in Chrome, because those webviews block PWA install and some features. The install dialog copies the link for them.
- **Saved events stay in the browser.** Saved events are kept in `localStorage` and exported as `.ics`, with no server storage.
- **Minimal service worker.** `sw.js` passes every request straight to the network. It exists only so browsers offer to install the site, and it caches nothing.

---

## Security notes

- Secrets belong only in `.env` files (git-ignored) or in Railway variables. **Never commit keys.**
- `back-end/data/cloudinary_catalog.json` is committed on purpose. It contains only public Instagram data and Cloudinary image URLs, no credentials.
- Local RapidAPI debug dumps (`rapidapi_dump*.json`) are git-ignored.
