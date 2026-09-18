from __future__ import annotations

from backend.models import SenderFilter
from backend.store import Store, normalize_sender

NOW = 1_700_000_000
DAY = 86400


def mk(mid, sender, days_ago, unread=1, **kw):
    row = {
        "id": mid,
        "thread_id": f"t-{mid}",
        "sender": sender,
        "sender_name": kw.pop("sender_name", "Sender Name"),
        "sender_raw": f"Sender Name <{sender}>",
        "domain": sender.split("@")[1],
        "subject": kw.pop("subject", "Hello"),
        "snippet": "snippet text",
        "internal_ts": NOW - days_ago * DAY,
        "is_unread": unread,
        "is_starred": kw.pop("is_starred", 0),
        "is_important": kw.pop("is_important", 0),
        "in_inbox": kw.pop("in_inbox", 1),
        "in_trash": kw.pop("in_trash", 0),
        "labels": ["INBOX"],
        "category": kw.pop("category", "unknown"),
        "list_id": kw.pop("list_id", None),
        "list_unsubscribe": kw.pop("list_unsubscribe", None),
        "list_unsubscribe_post": kw.pop("list_unsubscribe_post", None),
        "size": 1000,
        "fetched_at": NOW,
        "gone": kw.pop("gone", 0),
    }
    row.update(kw)
    return row


def test_normalize_sender():
    assert normalize_sender("Foo <No-Reply+abc@Example.com>") == ("no-reply@example.com", "Foo")
    assert normalize_sender("plain@Example.com") == ("plain@example.com", "")
    assert normalize_sender("") == ("", "")


def test_schema_and_upsert(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.counts() == {"messages": 0, "senders": 0}
    store.upsert_messages([mk("m1", "a@example.com", 1), mk("m2", "a@example.com", 2)])
    assert store.counts() == {"messages": 2, "senders": 1}
    # upsert again with same id updates in place, no duplicate
    store.upsert_messages([mk("m1", "a@example.com", 1, subject="Updated", unread=0)])
    assert store.counts() == {"messages": 2, "senders": 1}
    msgs = store.sender_messages("a@example.com", limit=10)
    m1 = [m for m in msgs if m.id == "m1"][0]
    assert m1.subject == "Updated"
    assert m1.is_unread is False


def test_known_ids(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_messages([mk("m1", "a@example.com", 1), mk("m2", "a@example.com", 2)])
    assert store.known_ids(["m1", "m2", "m3"]) == {"m1", "m2"}
    assert store.known_ids([]) == set()


def test_sender_stats_math(tmp_path):
    store = Store(tmp_path / "t.db")
    rows = [
        mk("a1", "a@example.com", 10, unread=0),
        mk("a2", "a@example.com", 20, unread=0),
        mk("a3", "a@example.com", 30, unread=1),
        mk("a4", "a@example.com", 40, unread=1),
        mk("a5", "a@example.com", 90, unread=1),
        mk("b1", "b@example.com", 5, unread=1),
        mk("b2", "b@example.com", 15, unread=1),
        mk("b3", "b@example.com", 25, unread=1),
        mk("c1", "c@promo.com", 5, unread=1, category="promotions",
           list_unsubscribe="<https://promo.com/unsub>", list_unsubscribe_post="List-Unsubscribe=One-Click"),
    ]
    store.upsert_messages(rows)

    total, items = store.sender_stats(SenderFilter(min_total=1, status=[]), now_ts=NOW)
    by_sender = {s.sender: s for s in items}
    assert total == 3

    a = by_sender["a@example.com"]
    assert a.total == 5
    assert a.opened == 2
    assert a.open_rate == 2 / 5
    assert a.days_since_last_received == 10
    assert a.days_since_last_opened == 10
    assert a.first_received.startswith("20")  # ISO string
    months = 90 / 30.44
    assert abs(a.per_month - 5 / months) < 1e-6

    b = by_sender["b@example.com"]
    assert b.opened == 0
    assert b.last_opened is None
    assert b.days_since_last_opened is None

    c = by_sender["c@promo.com"]
    assert c.has_unsubscribe is True
    assert c.unsubscribe_kind == "one_click"
    assert c.is_subscription is True
    assert c.category == "promotions"

    # last_opened_before_days: never-opened senders included only when include_never_opened
    total2, items2 = store.sender_stats(
        SenderFilter(min_total=1, last_opened_before_days=5, include_never_opened=True, status=[]),
        now_ts=NOW,
    )
    senders2 = {s.sender for s in items2}
    assert "b@example.com" in senders2  # never opened -> included
    assert "a@example.com" in senders2  # last opened 10 days ago >= 5

    total3, items3 = store.sender_stats(
        SenderFilter(min_total=1, last_opened_before_days=5, include_never_opened=False, status=[]),
        now_ts=NOW,
    )
    senders3 = {s.sender for s in items3}
    assert "b@example.com" not in senders3

    # search
    _, items4 = store.sender_stats(SenderFilter(min_total=1, search="promo", status=[]), now_ts=NOW)
    assert {s.sender for s in items4} == {"c@promo.com"}

    # status filter default excludes non-active senders once we mark one
    store.set_sender_status("a@example.com", "kept")
    _, items5 = store.sender_stats(SenderFilter(min_total=1), now_ts=NOW)  # default status=["active"]
    assert "a@example.com" not in {s.sender for s in items5}
    _, items6 = store.sender_stats(SenderFilter(min_total=1, status=["kept"]), now_ts=NOW)
    assert {s.sender for s in items6} == {"a@example.com"}


def test_sender_stats_max_open_rate_and_min_total(tmp_path):
    store = Store(tmp_path / "t.db")
    rows = [mk(f"h{i}", "heavy@example.com", i + 1, unread=0) for i in range(3)]
    rows += [mk(f"h{i}", "heavy@example.com", i + 1, unread=1) for i in range(3, 10)]
    rows += [mk("l1", "light@example.com", 1, unread=1)]
    store.upsert_messages(rows)
    total, items = store.sender_stats(SenderFilter(min_total=3, max_open_rate=0.5, status=[]), now_ts=NOW)
    senders = {s.sender for s in items}
    assert "heavy@example.com" in senders  # 3/10 open rate = 0.3 <= 0.5, total 10 >= 3
    assert "light@example.com" not in senders  # total 1 < 3


def test_sender_stats_for(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_messages([mk("m1", "a@example.com", 1), mk("m2", "b@example.com", 2)])
    stats = store.sender_stats_for(["a@example.com", "missing@example.com"])
    assert set(stats.keys()) == {"a@example.com"}


def test_subscription_senders(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_messages(
        [
            mk("p1", "promo@x.com", 1, category="promotions"),
            mk("n1", "normal@x.com", 1, category="primary"),
        ]
    )
    assert store.subscription_senders() == {"promo@x.com"}


def test_purge_candidates(tmp_path):
    # purge_candidates has no now_ts override (it uses wall-clock time), so build
    # rows relative to the real current time rather than the fixed NOW constant.
    import time as _time

    real_now = int(_time.time())

    def real_mk(mid, sender, days_ago, **kw):
        row = mk(mid, sender, 0, **kw)
        row["internal_ts"] = real_now - days_ago * DAY
        return row

    store = Store(tmp_path / "t.db")
    rows = [
        real_mk("p1", "a@example.com", 100, is_starred=1),
        real_mk("p2", "a@example.com", 90, is_important=1),
        real_mk("p3", "a@example.com", 80),
        real_mk("p4", "a@example.com", 5),  # too recent for older_than_days=30
    ]
    store.upsert_messages(rows)
    result = store.purge_candidates("a@example.com", older_than_days=30, skip_important=True)
    assert result["skipped_starred"] == 1
    assert result["skipped_important"] == 1
    assert result["ids"] == ["p3"]

    result_all = store.purge_candidates("a@example.com", older_than_days=None, skip_important=False)
    assert set(result_all["ids"]) == {"p2", "p3", "p4"}
    assert result_all["skipped_starred"] == 1
    assert result_all["skipped_important"] == 0


def test_set_in_trash(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_messages([mk("m1", "a@example.com", 1)])
    store.set_in_trash(["m1"], True)
    msgs = store.sender_messages("a@example.com")
    assert msgs[0].in_trash is True
    store.set_in_trash(["m1"], False)
    msgs = store.sender_messages("a@example.com")
    assert msgs[0].in_trash is False


def test_sender_status_default_active(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.get_sender_status("nobody@example.com") == "active"
    store.set_sender_status("nobody@example.com", "blocked")
    assert store.get_sender_status("nobody@example.com") == "blocked"


def test_action_log_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    log_id = store.log_action(
        "purge", "a@example.com", {"note": "x"}, "done", message_ids=["m1", "m2"], undoable=True
    )
    total, items = store.action_log(limit=10, offset=0)
    assert total == 1
    assert items[0].id == log_id
    assert items[0].message_count == 2
    assert items[0].undoable is True
    assert items[0].undone is False

    raw = store.get_action(log_id)
    assert raw["detail"] == {"note": "x"}
    assert raw["message_ids"] == ["m1", "m2"]

    store.mark_undone(log_id)
    raw2 = store.get_action(log_id)
    assert raw2["undone"] == 1
    assert store.get_action(999999) is None


def test_overview(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_messages(
        [
            mk("p1", "promo@x.com", 1, category="promotions", unread=1),
            mk("p2", "promo@x.com", 2, category="promotions", unread=0),
            mk("n1", "normal@x.com", 1, category="primary", unread=0),
        ]
    )
    store.log_action("purge", "promo@x.com", {}, "done", message_ids=["p1"], undoable=True)
    store.log_action("unsubscribe", "promo@x.com", {}, "simulated")
    ov = store.overview(now_ts=NOW)
    assert ov.messages_total == 3
    assert ov.senders_total == 2
    assert ov.subscription_senders == 1
    assert ov.subscription_messages == 2
    assert ov.by_category["promotions"] == 2
    assert ov.actions["trashed_messages"] == 1
    assert ov.actions["unsubscribed"] == 1


def test_settings_and_scan_state(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.get_setting("foo") is None
    assert store.get_setting("foo", "default") == "default"
    store.set_setting("foo", {"a": 1})
    assert store.get_setting("foo") == {"a": 1}

    assert store.get_scan_state("bar") is None
    store.set_scan_state("bar", {"page": 2})
    assert store.get_scan_state("bar") == {"page": 2}
