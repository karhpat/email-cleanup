from __future__ import annotations

import time

import pytest

from backend.actions import (
    parse_list_unsubscribe,
    parse_mailto,
    preview,
    run_execute,
    undo,
    unsubscribe_kind,
)
from backend.gmail_client import DemoGmailClient
from backend.jobs import Job, JobRunner
from backend.models import ActionRequest
from backend.store import Store

NOW = 1_700_000_000
DAY = 86400


def mk(mid, sender, days_ago, unread=1, **kw):
    row = {
        "id": mid,
        "thread_id": f"t-{mid}",
        "sender": sender,
        "sender_name": kw.pop("sender_name", "Brand"),
        "sender_raw": f"Brand <{sender}>",
        "domain": sender.split("@")[1],
        "subject": kw.pop("subject", "Hello"),
        "snippet": "snippet",
        "internal_ts": int(time.time()) - days_ago * DAY,
        "is_unread": unread,
        "is_starred": kw.pop("is_starred", 0),
        "is_important": kw.pop("is_important", 0),
        "in_inbox": kw.pop("in_inbox", 1),
        "in_trash": kw.pop("in_trash", 0),
        "labels": ["INBOX"],
        "category": kw.pop("category", "promotions"),
        "list_id": kw.pop("list_id", None),
        "list_unsubscribe": kw.pop("list_unsubscribe", None),
        "list_unsubscribe_post": kw.pop("list_unsubscribe_post", None),
        "size": 1000,
        "fetched_at": NOW,
        "gone": kw.pop("gone", 0),
    }
    row.update(kw)
    return row


# --------------------------------------------------------- header parsing


def test_parse_list_unsubscribe_both():
    header = "<https://example.com/unsub?u=1>, <mailto:unsub@example.com?subject=unsubscribe>"
    assert parse_list_unsubscribe(header) == [
        "https://example.com/unsub?u=1",
        "mailto:unsub@example.com?subject=unsubscribe",
    ]


def test_parse_list_unsubscribe_no_brackets_with_spaces():
    header = "  https://example.com/unsub  "
    assert parse_list_unsubscribe(header) == ["https://example.com/unsub"]


def test_parse_list_unsubscribe_empty():
    assert parse_list_unsubscribe(None) == []
    assert parse_list_unsubscribe("") == []


def test_unsubscribe_kind_one_click():
    kind = unsubscribe_kind("<https://x.com/u>", "List-Unsubscribe=One-Click")
    assert kind == "one_click"


def test_unsubscribe_kind_mailto_only():
    kind = unsubscribe_kind("<mailto:unsub@x.com>", None)
    assert kind == "mailto"


def test_unsubscribe_kind_http_only():
    kind = unsubscribe_kind("<https://x.com/prefs>", None)
    assert kind == "http"


def test_unsubscribe_kind_both_no_one_click_flag_prefers_mailto():
    kind = unsubscribe_kind("<https://x.com/u>, <mailto:unsub@x.com>", None)
    assert kind == "mailto"


def test_unsubscribe_kind_none():
    assert unsubscribe_kind(None, None) == "none"
    assert unsubscribe_kind("", "") == "none"


def test_parse_mailto_with_subject_and_body():
    address, subject, body = parse_mailto("mailto:unsub@x.com?subject=Stop%20it&body=please")
    assert address == "unsub@x.com"
    assert subject == "Stop it"
    assert body == "please"


def test_parse_mailto_defaults():
    address, subject, body = parse_mailto("mailto:unsub@x.com")
    assert address == "unsub@x.com"
    assert subject == "unsubscribe"
    assert body == "unsubscribe"


# --------------------------------------------------------- preview/execute/undo


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_messages(
        [
            mk(
                "oc1", "promo@x.com", 40,
                list_unsubscribe="<https://x.com/u>", list_unsubscribe_post="List-Unsubscribe=One-Click",
            ),
            mk(
                "oc2", "promo@x.com", 5,
                list_unsubscribe="<https://x.com/u2>", list_unsubscribe_post="List-Unsubscribe=One-Click",
            ),
            mk("mt1", "mailto@y.com", 10, list_unsubscribe="<mailto:unsub@y.com>"),
            mk("none1", "plain@z.com", 10),
            mk("none2", "plain@z.com", 20, is_starred=1),
        ]
    )
    return s


@pytest.fixture
def gmail():
    return DemoGmailClient()


def test_preview_stop(store):
    req = ActionRequest(action="stop", senders=["promo@x.com", "mailto@y.com", "plain@z.com"])
    result = preview(store, req)
    by_sender = {i.sender: i for i in result.items}
    assert by_sender["promo@x.com"].unsubscribe_method == "one_click"
    assert by_sender["promo@x.com"].will_unsubscribe is True
    assert by_sender["promo@x.com"].will_block is True
    assert by_sender["mailto@y.com"].unsubscribe_method == "mailto"
    assert by_sender["plain@z.com"].unsubscribe_method == "none"
    assert by_sender["plain@z.com"].will_unsubscribe is False


def test_preview_purge(store):
    req = ActionRequest(action="purge", senders=["promo@x.com"], purge_older_than_days=30, skip_important=True)
    result = preview(store, req)
    item = result.items[0]
    assert item.purge_count == 1  # only oc1 (40 days) is older than 30 days
    assert item.skipped_starred == 0


def _run_job(store, gmail, req, demo=True):
    runner = JobRunner()
    job_id = runner.start(
        "execute_actions", lambda job: run_execute(job, store, gmail, req, demo=demo)
    )
    for _ in range(200):
        state = runner.current()
        if state.state != "running":
            break
        time.sleep(0.01)
    return runner.current()


def test_execute_stop_one_click(store, gmail):
    req = ActionRequest(action="stop", senders=["promo@x.com"], unsubscribe=True, block=True)
    state = _run_job(store, gmail, req)
    assert state.state == "done"
    total, items = store.action_log(limit=10, offset=0)
    actions = {i.action for i in items}
    assert actions == {"unsubscribe", "block"}
    for i in items:
        assert i.status == "simulated"
    assert store.get_sender_status("promo@x.com") == "blocked"
    assert any(c["method"] == "one_click_unsubscribe" for c in gmail.calls)
    assert any(c["method"] == "create_filter" for c in gmail.calls)


def test_execute_stop_mailto(store, gmail):
    req = ActionRequest(action="stop", senders=["mailto@y.com"], unsubscribe=True, block=False)
    _run_job(store, gmail, req)
    assert store.get_sender_status("mailto@y.com") == "unsubscribed"
    send_calls = [c for c in gmail.calls if c["method"] == "send_mail"]
    assert len(send_calls) == 1
    assert send_calls[0]["to"] == "unsub@y.com"


def test_execute_purge_and_undo(store, gmail):
    req = ActionRequest(action="purge", senders=["plain@z.com"], purge_older_than_days=None, skip_important=False)
    _run_job(store, gmail, req)
    total, items = store.action_log(limit=10, offset=0)
    purge_row = [i for i in items if i.action == "purge"][0]
    assert purge_row.status == "simulated"
    assert purge_row.undoable is True
    # none2 is starred -> always skipped; none1 gets trashed
    msgs = store.sender_messages("plain@z.com")
    trashed = {m.id: m.in_trash for m in msgs}
    assert trashed["none1"] is True
    assert trashed["none2"] is False

    undo(store, gmail, purge_row.id, demo=True)
    msgs2 = store.sender_messages("plain@z.com")
    assert all(not m.in_trash for m in msgs2)
    _, items2 = store.action_log(limit=10, offset=0)
    assert [i for i in items2 if i.id == purge_row.id][0].undone is True

    with pytest.raises(ValueError):
        undo(store, gmail, purge_row.id, demo=True)


def test_execute_keep_and_reactivate(store, gmail):
    req = ActionRequest(action="keep", senders=["plain@z.com"])
    _run_job(store, gmail, req)
    assert store.get_sender_status("plain@z.com") == "kept"

    req2 = ActionRequest(action="reactivate", senders=["plain@z.com"])
    _run_job(store, gmail, req2)
    assert store.get_sender_status("plain@z.com") == "active"


def test_undo_unsubscribe_is_rejected(store, gmail):
    log_id = store.log_action("unsubscribe", "promo@x.com", {}, "simulated", undoable=False)
    with pytest.raises(ValueError, match="cannot undo an unsubscribe"):
        undo(store, gmail, log_id, demo=True)


def test_undo_unknown_log_id(store, gmail):
    with pytest.raises(ValueError):
        undo(store, gmail, 999999, demo=True)


def test_execute_continues_after_sender_failure(store, gmail, monkeypatch):
    calls = {"n": 0}
    original = gmail.create_filter

    def flaky(from_email):
        calls["n"] += 1
        if from_email == "promo@x.com":
            raise RuntimeError("boom")
        return original(from_email)

    monkeypatch.setattr(gmail, "create_filter", flaky)
    req = ActionRequest(action="stop", senders=["promo@x.com", "mailto@y.com"], unsubscribe=False, block=True)
    state = _run_job(store, gmail, req)
    assert state.state == "done"
    assert state.result["failed"] == 1
    assert state.result["processed"] == 1
    total, items = store.action_log(limit=10, offset=0)
    statuses = {(i.sender, i.action): i.status for i in items}
    assert statuses[("promo@x.com", "stop")] == "failed"
    assert statuses[("mailto@y.com", "block")] == "simulated"
