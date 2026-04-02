"""
Background worker: processes queued /suggest jobs sequentially.

Run this as a separate Railway service/worker (same repo) so web requests stay fast.

Logs: WARNING+ by default (errors only). Set SUGGEST_WORKER_VERBOSE=1 for INFO (idle, job lifecycle).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from urllib.parse import urlparse

from catalog_db import (
    DATABASE_URL,
    ensure_database,
    suggest_job_claim_next,
    suggest_job_finish,
    suggest_jobs_open_summary,
    suggest_repair_stale_jobs,
)
from suggest_pipeline import process_user_suggestion

_VERBOSE = bool(os.environ.get("SUGGEST_WORKER_VERBOSE"))
logging.basicConfig(
    level=logging.INFO if _VERBOSE else logging.WARNING,
    format="%(asctime)s [%(levelname)s] suggest_worker: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("suggest_worker")

_IDLE_LOG_EVERY = 15


def _log_db_target() -> None:
    if not (DATABASE_URL or "").strip():
        log.warning("DATABASE_URL is empty — worker cannot see the same queue as the web app")
        return
    try:
        u = urlparse(DATABASE_URL)
        name = (u.path or "").replace("/", "", 1).split("/")[0] or "?"
        log.info("Postgres target host=%s database=%s (must match gallery_app)", u.hostname or "?", name)
    except Exception:
        log.info("DATABASE_URL is set (could not parse for log)")


def main() -> None:
    if _VERBOSE:
        log.info("starting (verbose mode)")
        _log_db_target()
    ensure_database()
    idle_count = 0
    while True:
        n_stale = suggest_repair_stale_jobs()
        if n_stale and _VERBOSE:
            log.info("repaired %s stale suggest job(s)", n_stale)

        job = suggest_job_claim_next()
        if not job:
            idle_count += 1
            if _VERBOSE and idle_count >= _IDLE_LOG_EVERY:
                log.info(
                    "idle — only status=queued is claimed | %s",
                    suggest_jobs_open_summary(),
                )
                idle_count = 0
            time.sleep(2.0)
            continue

        idle_count = 0
        job_id = int(job["id"])
        raw = str(job.get("username_raw") or "")
        ip_hash = str(job.get("ip_hash") or "")
        if _VERBOSE:
            log.info("claimed job id=%s username=%r ip_hash=%s...", job_id, raw, ip_hash[:16])
        try:
            res = process_user_suggestion(raw, ip_hash)
            ok = bool(res.get("ok"))
            msg = str(res.get("message") or "")
            canon = res.get("username") if isinstance(res.get("username"), str) else None
            suggest_job_finish(job_id, ok=ok, message=msg, canonical_username=canon)
            if _VERBOSE:
                log.info(
                    "finished job id=%s ok=%s canonical=%r message=%s",
                    job_id,
                    ok,
                    canon,
                    (msg[:120] + "…") if len(msg) > 120 else msg,
                )
        except Exception:
            log.exception("job id=%s failed with exception", job_id)
            suggest_job_finish(
                job_id,
                ok=False,
                message="Системска грешка при обработка. Обиди се подоцна.",
                canonical_username=None,
            )


if __name__ == "__main__":
    main()
