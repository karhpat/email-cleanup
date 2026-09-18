"""Scan job: list ids -> batch metadata -> store. Resumable via scan_state
and the scan_pending table. See docs/ARCHITECTURE.md "Gmail API facts" for
the query and sweep semantics this implements.
"""
from __future__ import annotations

import calendar
import time
from datetime import datetime, timezone

from backend.gmail_client import parse_metadata
from backend.jobs import Job
from backend.store import Store

BASE_QUERY = "-in:chats -in:sent -in:draft -in:spam -in:trash"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _months_ago(now: datetime, months: int) -> datetime:
    total = now.year * 12 + (now.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)


def _window(window_months: int) -> tuple[int, str]:
    if window_months and window_months > 0:
        start_dt = _months_ago(datetime.now(timezone.utc), window_months)
        query = f"{BASE_QUERY} after:{start_dt.strftime('%Y/%m/%d')}"
        return int(start_dt.timestamp()), query
    return 0, BASE_QUERY


def run_scan(job: Job, store: Store, gmail, window_months: int, full: bool) -> dict:
    window_start_ts, query = _window(window_months)

    stage = store.get_scan_state("scan_stage")
    resuming = (
        stage
        and store.get_scan_state("scan_query") == query
        and store.get_scan_state("scan_full") == full
    )
    if resuming:
        page_token = store.get_scan_state("scan_page_token")
    else:
        store.scan_pending_clear()
        store.set_scan_state("scan_query", query)
        store.set_scan_state("scan_full", full)
        store.set_scan_state("scan_page_token", None)
        store.set_scan_state("scan_stage", "listing")
        stage = "listing"
        page_token = None

    messages_seen = 0

    if stage == "listing":
        job.progress(0, 0, "Listing ids…")
        while True:
            if job.cancelled:
                return {"cancelled": True, "stage": "listing"}
            ids, next_token, estimate = gmail.list_message_ids(query, page_token)
            if ids:
                store.scan_pending_add(ids)
            page_token = next_token
            store.set_scan_state("scan_page_token", page_token)
            pending = store.scan_pending_count()
            job.progress(pending["total"], max(pending["total"], estimate or 0), "Listing ids…")
            if not page_token:
                break
        if job.cancelled:
            return {"cancelled": True, "stage": "listing"}
        store.set_scan_state("scan_stage", "fetching")
        stage = "fetching"

    if stage == "fetching":
        counts = store.scan_pending_count()
        total_ids, done = counts["total"], counts["fetched"]
        job.progress(done, total_ids, f"Fetching metadata {done}/{total_ids}")
        while True:
            if job.cancelled:
                return {"cancelled": True, "stage": "fetching"}
            batch_ids = store.scan_pending_unfetched(50)
            if not batch_ids:
                break
            if full:
                to_fetch = batch_ids
            else:
                known = store.known_ids(batch_ids)
                to_fetch = [i for i in batch_ids if i not in known]
            if to_fetch:
                raw = gmail.get_metadata_batch(to_fetch)
                store.upsert_messages([parse_metadata(r) for r in raw])
            store.scan_pending_mark_fetched(batch_ids)
            done += len(batch_ids)
            job.progress(done, total_ids, f"Fetching metadata {done}/{total_ids}")
        if job.cancelled:
            return {"cancelled": True, "stage": "fetching"}
        store.set_scan_state("scan_stage", "sweep")
        stage = "sweep"

    if stage == "sweep":
        if job.cancelled:
            return {"cancelled": True, "stage": "sweep"}
        job.progress(0, 1, "Refreshing read state")
        read_query = f"{query} is:read"
        read_ids: set[str] = set()
        rtoken = None
        while True:
            if job.cancelled:
                return {"cancelled": True, "stage": "sweep"}
            ids, rtoken, _ = gmail.list_message_ids(read_query, rtoken)
            read_ids.update(i for i, _ in ids)
            if not rtoken:
                break
        store.mark_read_state(read_ids, window_start_ts)

        present_ids = set(store.scan_pending_all_ids())
        store.mark_gone(present_ids, window_start_ts)
        messages_seen = len(present_ids)

        store.set_scan_state(
            "last_scan",
            {"finished_at": _now_iso(), "window_months": window_months, "messages_seen": messages_seen},
        )
        store.scan_pending_clear()
        store.set_scan_state("scan_stage", None)
        store.set_scan_state("scan_page_token", None)
        job.progress(1, 1, "Done")

    return {"messages_seen": messages_seen, "window_months": window_months}
