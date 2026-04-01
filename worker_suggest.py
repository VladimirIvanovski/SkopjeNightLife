"""
Background worker: processes queued /suggest jobs sequentially.

Run this as a separate Railway service/worker (same repo) so web requests stay fast.
"""

from __future__ import annotations

import time

from catalog_db import ensure_database, suggest_job_claim_next, suggest_job_finish
from suggest_pipeline import process_user_suggestion


def main() -> None:
    ensure_database()
    while True:
        job = suggest_job_claim_next()
        if not job:
            time.sleep(2.0)
            continue
        job_id = int(job["id"])
        raw = str(job.get("username_raw") or "")
        ip_hash = str(job.get("ip_hash") or "")
        try:
            res = process_user_suggestion(raw, ip_hash)
            ok = bool(res.get("ok"))
            msg = str(res.get("message") or "")
            canon = res.get("username") if isinstance(res.get("username"), str) else None
            suggest_job_finish(job_id, ok=ok, message=msg, canonical_username=canon)
        except Exception:
            suggest_job_finish(
                job_id,
                ok=False,
                message="Системска грешка при обработка. Обиди се подоцна.",
                canonical_username=None,
            )


if __name__ == "__main__":
    main()

