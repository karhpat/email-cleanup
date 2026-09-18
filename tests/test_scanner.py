from __future__ import annotations

import time

from backend.gmail_client import parse_metadata
from backend.scanner import run_scan
from backend.store import Store


# ------------------------------------------------------------------ fakes


class FakeJob:
    """Minimal duck-typed stand-in for jobs.Job, with controllable cancellation."""

    def __init__(self, cancel_after: int | None = None):
        self.cancel_after = cancel_after
        self._progress_calls = 0
        self._cancelled = False
        self.messages: list[str] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def progress(self, done: int, total: int, message: str = "") -> None:
        self.messages.append(message)
        self._progress_calls += 1
        if self.cancel_after is not None and self._progress_calls >= self.cancel_after:
            self._cancelled = True


class FakeGmail:
    """Serves canned list/metadata pages. Pages are resumed by opaque token,
    so a resumed scan can jump straight to the right page regardless of how
    many calls happened before."""

    def __init__(self, listing_pages, metadata_by_id, read_ids=None):
        self.listing_pages = listing_pages  # list[list[(id, thread_id)]]
        self.metadata_by_id = metadata_by_id
        self.read_ids = read_ids or []
        self.calls: list[tuple] = []

    def list_message_ids(self, query: str, page_token: str | None = None):
        self.calls.append(("list", query, page_token))
        if query.strip().endswith("is:read"):
            return [(i, f"th-{i}") for i in self.read_ids], None, len(self.read_ids)
        idx = 0 if page_token is None else int(page_token[3:])
        page = self.listing_pages[idx]
        total = sum(len(p) for p in self.listing_pages)
        next_token = f"tok{idx + 1}" if idx + 1 < len(self.listing_pages) else None
        return list(page), next_token, total

    def get_metadata_batch(self, ids):
        self.calls.append(("meta", tuple(ids)))
        return [self.metadata_by_id[i] for i in ids if i in self.metadata_by_id]


def raw_for(mid, sender="Brand <brand@x.com>", subject="Subj", unread=True,
            list_unsub=None, list_unsub_post=None, internal_ms=None, extra_labels=None,
            from_header_name="From"):
    label_ids = ["INBOX"]
    if unread:
        label_ids.append("UNREAD")
    if extra_labels:
        label_ids += extra_labels
    headers = [
        {"name": from_header_name, "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
    ]
    if list_unsub:
        headers.append({"name": "List-Unsubscribe", "value": list_unsub})
    if list_unsub_post:
        headers.append({"name": "List-Unsubscribe-Post", "value": list_unsub_post})
    return {
        "id": mid,
        "threadId": f"t-{mid}",
        "labelIds": label_ids,
        "snippet": "a short snippet",
        "internalDate": str(internal_ms if internal_ms is not None else int(time.time() * 1000)),
        "sizeEstimate": 4321,
        "payload": {"headers": headers},
    }


# --------------------------------------------------------------- parse_metadata


def test_parse_metadata_basic():
    raw = raw_for(
        "m1",
        sender="Some Brand <NO-REPLY+promo@Example.COM>",
        subject="Hello there",
        unread=True,
        list_unsub="<https://example.com/u>",
        list_unsub_post="List-Unsubscribe=One-Click",
        extra_labels=["CATEGORY_PROMOTIONS", "STARRED", "IMPORTANT"],
        internal_ms=1700000000000,
        from_header_name="FROM",  # case-insensitive header name
    )
    row = parse_metadata(raw)
    assert row["id"] == "m1"
    assert row["thread_id"] == "t-m1"
    assert row["sender"] == "no-reply@example.com"
    assert row["sender_name"] == "Some Brand"
    assert row["domain"] == "example.com"
    assert row["subject"] == "Hello there"
    assert row["category"] == "promotions"
    assert row["is_unread"] == 1
    assert row["is_starred"] == 1
    assert row["is_important"] == 1
    assert row["in_inbox"] == 1
    assert row["in_trash"] == 0
    assert row["list_unsubscribe"] == "<https://example.com/u>"
    assert row["list_unsubscribe_post"] == "List-Unsubscribe=One-Click"
    assert row["internal_ts"] == 1700000000
    assert row["gone"] == 0


def test_parse_metadata_unknown_category_and_read():
    raw = raw_for("m2", unread=False, extra_labels=[])
    row = parse_metadata(raw)
    assert row["category"] == "unknown"
    assert row["is_unread"] == 0
    assert row["is_starred"] == 0


# -------------------------------------------------------------------- run_scan


def test_run_scan_resume_after_cancel_during_listing(tmp_path):
    store = Store(tmp_path / "t.db")
    pages = [
        [("id1", "th1"), ("id2", "th2")],
        [("id3", "th3"), ("id4", "th4")],
        [("id5", "th5")],
    ]
    meta = {i: raw_for(i) for i in ["id1", "id2", "id3", "id4", "id5"]}
    gmail = FakeGmail(pages, meta, read_ids=["id1", "id3"])

    job1 = FakeJob(cancel_after=2)  # cancel right after the first page is processed
    result1 = run_scan(job1, store, gmail, window_months=0, full=False)
    assert result1 == {"cancelled": True, "stage": "listing"}
    assert store.scan_pending_count() == {"total": 2, "fetched": 0}
    assert store.get_scan_state("scan_stage") == "listing"
    list_calls = [c for c in gmail.calls if c[0] == "list" and not c[1].endswith("is:read")]
    assert len(list_calls) == 1  # only page 0 was ever fetched

    job2 = FakeJob()
    result2 = run_scan(job2, store, gmail, window_months=0, full=False)
    assert result2 == {"messages_seen": 5, "window_months": 0}

    list_calls2 = [c for c in gmail.calls if c[0] == "list" and not c[1].endswith("is:read")]
    assert len(list_calls2) == 3  # page0 (run1) + page1 + page2 (run2), no re-fetch of page0

    assert store.counts()["messages"] == 5
    assert store.scan_pending_count() == {"total": 0, "fetched": 0}
    assert store.get_scan_state("scan_stage") is None

    msgs = {m.id: m for m in store.sender_messages("brand@x.com", limit=10)}
    assert msgs["id1"].is_unread is False
    assert msgs["id3"].is_unread is False
    assert msgs["id2"].is_unread is True

    last_scan = store.get_scan_state("last_scan")
    assert last_scan["messages_seen"] == 5


def test_run_scan_resume_after_cancel_during_fetching(tmp_path):
    store = Store(tmp_path / "t.db")
    ids = [f"id{i}" for i in range(60)]
    pages = [[(i, f"th-{i}") for i in ids]]
    meta = {i: raw_for(i) for i in ids}
    gmail = FakeGmail(pages, meta, read_ids=[])

    # progress calls: 1) initial "listing" 2) after the (only) listing page
    # 3) initial "fetching" 4) after the first 50-id fetch batch -> cancel here.
    job1 = FakeJob(cancel_after=4)
    result1 = run_scan(job1, store, gmail, window_months=0, full=False)
    assert result1 == {"cancelled": True, "stage": "fetching"}
    counts = store.scan_pending_count()
    assert counts["total"] == 60
    assert counts["fetched"] == 50
    assert store.counts()["messages"] == 50
    meta_calls = [c for c in gmail.calls if c[0] == "meta"]
    assert len(meta_calls) == 1
    assert len(meta_calls[0][1]) == 50

    job2 = FakeJob()
    result2 = run_scan(job2, store, gmail, window_months=0, full=False)
    assert result2["messages_seen"] == 60
    assert store.counts()["messages"] == 60
    meta_calls2 = [c for c in gmail.calls if c[0] == "meta"]
    assert len(meta_calls2) == 2  # first batch (run1) + remaining 10 ids (run2)
    assert len(meta_calls2[1][1]) == 10


def test_run_scan_full_refetches_known_ids(tmp_path):
    store = Store(tmp_path / "t.db")
    pages = [[("id1", "th1")]]
    meta = {"id1": raw_for("id1", subject="Original")}
    gmail = FakeGmail(pages, meta, read_ids=["id1"])
    run_scan(FakeJob(), store, gmail, window_months=0, full=False)
    assert store.sender_messages("brand@x.com")[0].subject == "Original"

    gmail.metadata_by_id["id1"] = raw_for("id1", subject="Updated")
    run_scan(FakeJob(), store, gmail, window_months=0, full=True)
    assert store.sender_messages("brand@x.com")[0].subject == "Updated"
    meta_calls = [c for c in gmail.calls if c[0] == "meta"]
    # second run (full=True) should have re-fetched id1 even though it was known
    assert any("id1" in c[1] for c in meta_calls[1:])


def test_run_scan_marks_gone_messages(tmp_path):
    store = Store(tmp_path / "t.db")
    pages = [[("id1", "th1"), ("id2", "th2")]]
    meta = {"id1": raw_for("id1"), "id2": raw_for("id2")}
    gmail = FakeGmail(pages, meta, read_ids=[])
    run_scan(FakeJob(), store, gmail, window_months=0, full=False)
    assert store.counts()["messages"] == 2

    # id2 no longer shows up on the server at all (e.g. permanently deleted)
    gmail2 = FakeGmail([[("id1", "th1")]], {"id1": raw_for("id1")}, read_ids=[])
    run_scan(FakeJob(), store, gmail2, window_months=0, full=False)

    row = store.conn.execute("SELECT gone FROM messages WHERE id='id2'").fetchone()
    assert row["gone"] == 1  # still in the DB, just marked gone
    # gone messages are excluded from counts() (gone=0 filter)
    assert store.counts()["messages"] == 1
