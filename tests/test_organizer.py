"""Tests for backend/organizer.py.

Since backend/store.py does not exist yet (written by another engineer in
parallel), these tests build a tiny in-memory test double: a sqlite3
connection created straight from the contract schema in
docs/ARCHITECTURE.md, plus FakeStore/FakeGmail/FakeJob doubles matching the
interfaces Organizer expects.
"""
from __future__ import annotations

import sqlite3
import threading

from backend.classifier import FakeClassifier
from backend.config import Config
from backend.models import ApplyRequest, CategoryDef, ClassifyRequest
from backend.organizer import Organizer

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, thread_id TEXT, sender TEXT NOT NULL, sender_name TEXT, sender_raw TEXT,
  domain TEXT, subject TEXT, snippet TEXT, internal_ts INTEGER NOT NULL,
  is_unread INTEGER NOT NULL DEFAULT 1, is_starred INTEGER DEFAULT 0, is_important INTEGER DEFAULT 0,
  in_inbox INTEGER DEFAULT 1, in_trash INTEGER DEFAULT 0, labels TEXT,
  category TEXT, list_id TEXT, list_unsubscribe TEXT, list_unsubscribe_post TEXT,
  size INTEGER, fetched_at INTEGER, gone INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(internal_ts);
CREATE TABLE IF NOT EXISTS sender_status (sender TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, action TEXT, sender TEXT, detail TEXT,
  status TEXT, message_ids TEXT,
  undoable INTEGER DEFAULT 0, undone INTEGER DEFAULT 0, job_id TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
  message_id TEXT PRIMARY KEY, category TEXT, confidence REAL, model TEXT, reason TEXT,
  applied INTEGER DEFAULT 0, overridden INTEGER DEFAULT 0, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS categories (key TEXT PRIMARY KEY, label_name TEXT, description TEXT, sort_order INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scan_state (key TEXT PRIMARY KEY, value TEXT);
"""


# ---------- test doubles ----------

class FakeStore:
    def __init__(self, conn, subscription_senders=None):
        self.conn = conn
        self.lock = threading.RLock()
        self._subscription_senders = set(subscription_senders or [])
        self.logged_actions = []

    def subscription_senders(self):
        return self._subscription_senders

    def log_action(self, action, sender, detail, status, message_ids=None, undoable=False, job_id=None):
        entry = {
            "action": action,
            "sender": sender,
            "detail": detail,
            "status": status,
            "message_ids": message_ids,
            "undoable": undoable,
            "job_id": job_id,
        }
        self.logged_actions.append(entry)
        return len(self.logged_actions)


class FakeGmail:
    def __init__(self):
        self.labels = {}
        self.batch_modify_calls = []
        self._next_id = 1

    def ensure_label(self, name):
        if name not in self.labels:
            self.labels[name] = f"Label_{self._next_id}"
            self._next_id += 1
        return self.labels[name]

    def batch_modify(self, ids, add=None, remove=None):
        self.batch_modify_calls.append({"ids": list(ids), "add": add, "remove": remove})


class FakeJob:
    def __init__(self):
        self.cancelled = False
        self.progress_calls = []

    def progress(self, done, total, message):
        self.progress_calls.append((done, total, message))


class StubEstimateClassifier:
    """Records how it was constructed and what it was asked to estimate."""

    last_categories = None

    def __init__(self, categories):
        self.categories = categories
        StubEstimateClassifier.last_categories = categories

    def estimate_cost_usd(self, n):
        return round(n * 0.02, 2)

    def classify(self, emails, on_progress=None, should_cancel=None):
        raise NotImplementedError


# ---------- fixtures / helpers ----------

def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def make_config(demo=False):
    return Config(
        demo=demo,
        classify_model="claude-haiku-4-5",
        escalate_model="claude-sonnet-5",
        escalate_below=0.6,
    )


def insert_message(
    conn, id, sender, sender_name="", subject="", snippet="", internal_ts=0,
    in_inbox=1, in_trash=0, gone=0, is_unread=1,
):
    conn.execute(
        """
        INSERT INTO messages (id, sender, sender_name, subject, snippet, internal_ts,
                               is_unread, in_inbox, in_trash, gone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (id, sender, sender_name, subject, snippet, internal_ts, is_unread, in_inbox, in_trash, gone),
    )
    conn.commit()


def insert_classification(
    conn, message_id, category, confidence=0.9, model="fake", reason="",
    applied=0, overridden=0, created_at=0,
):
    conn.execute(
        """
        INSERT INTO classifications (message_id, category, confidence, model, reason, applied, overridden, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, category, confidence, model, reason, applied, overridden, created_at),
    )
    conn.commit()


def fake_classifier_factory(categories):
    return FakeClassifier(categories, escalate_below=0.6)


# ---------- categories ----------

def test_default_categories_matches_contract_table():
    defaults = Organizer.default_categories()
    keys = [c.key for c in defaults]
    assert keys == [
        "finance", "receipts", "travel", "health", "home",
        "work", "personal", "government", "accounts", "skip",
    ]
    by_key = {c.key: c for c in defaults}
    assert by_key["skip"].label_name is None
    assert by_key["finance"].label_name == "Finance"
    assert all(c.description for c in defaults)


def test_get_categories_seeds_defaults_when_empty():
    conn = make_conn()
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    cats = org.get_categories()
    assert [c.key for c in cats] == [c.key for c in Organizer.default_categories()]

    row_count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    assert row_count == len(Organizer.default_categories())


def test_set_categories_keeps_skip_present():
    conn = make_conn()
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    result = org.set_categories([
        CategoryDef(key="finance", label_name="Finance", description="money stuff"),
    ])

    keys = [c.key for c in result]
    assert "skip" in keys
    skip = next(c for c in result if c.key == "skip")
    assert skip.label_name is None

    # Replacing again drops the old set entirely.
    result2 = org.set_categories([
        CategoryDef(key="work", label_name="Work", description="job stuff"),
    ])
    keys2 = {c.key for c in result2}
    assert keys2 == {"work", "skip"}


# ---------- candidates ----------

def test_candidates_excludes_subscription_senders():
    conn = make_conn()
    insert_message(conn, "m1", "person@example.com", subject="hi", internal_ts=1000)
    insert_message(conn, "m2", "newsletter@promo.com", subject="deal", internal_ts=1000)
    store = FakeStore(conn, subscription_senders={"newsletter@promo.com"})
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    result = org.candidates(scope="all", days=0, limit=None, reclassify=False)
    ids = [e.message_id for e in result]
    assert ids == ["m1"]


def test_candidates_scope_inbox_filters_in_inbox():
    conn = make_conn()
    insert_message(conn, "m1", "a@example.com", internal_ts=1000, in_inbox=1)
    insert_message(conn, "m2", "b@example.com", internal_ts=1000, in_inbox=0)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    inbox_only = org.candidates(scope="inbox", days=0, limit=None, reclassify=False)
    assert [e.message_id for e in inbox_only] == ["m1"]

    all_scope = org.candidates(scope="all", days=0, limit=None, reclassify=False)
    assert {e.message_id for e in all_scope} == {"m1", "m2"}


def test_candidates_days_window(monkeypatch):
    conn = make_conn()
    now = 10_000_000
    old_ts = now - (400 * 86400)
    recent_ts = now - (5 * 86400)
    insert_message(conn, "old", "a@example.com", internal_ts=old_ts)
    insert_message(conn, "recent", "b@example.com", internal_ts=recent_ts)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    monkeypatch.setattr("backend.organizer.time.time", lambda: now)  # freeze "now" for the days cutoff

    within_30 = org.candidates(scope="all", days=30, limit=None, reclassify=False)
    assert [e.message_id for e in within_30] == ["recent"]

    no_filter = org.candidates(scope="all", days=0, limit=None, reclassify=False)
    assert {e.message_id for e in no_filter} == {"old", "recent"}


def test_candidates_limit():
    conn = make_conn()
    for i in range(5):
        insert_message(conn, f"m{i}", "a@example.com", internal_ts=1000 + i)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    result = org.candidates(scope="all", days=0, limit=2, reclassify=False)
    assert len(result) == 2


def test_candidates_reclassify_flag():
    conn = make_conn()
    insert_message(conn, "m1", "a@example.com", internal_ts=1000)
    insert_classification(conn, "m1", "finance")
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    not_reclassify = org.candidates(scope="all", days=0, limit=None, reclassify=False)
    assert not_reclassify == []

    reclassify = org.candidates(scope="all", days=0, limit=None, reclassify=True)
    assert [e.message_id for e in reclassify] == ["m1"]


# ---------- estimate ----------

def test_estimate_counts_and_cost():
    conn = make_conn()
    insert_message(conn, "m1", "a@example.com", internal_ts=1000)          # candidate
    insert_message(conn, "m2", "b@example.com", internal_ts=1000)          # already classified
    insert_classification(conn, "m2", "finance")
    insert_message(conn, "m3", "promo@newsletter.com", internal_ts=1000)   # excluded (subscription)
    store = FakeStore(conn, subscription_senders={"promo@newsletter.com"})
    org = Organizer(store, FakeGmail(), make_config(), StubEstimateClassifier)

    est = org.estimate(scope="all", days=0)
    assert est.candidates == 1
    assert est.already_classified == 1
    assert est.est_cost_usd == 0.02  # StubEstimateClassifier: n * 0.02
    assert est.model == "claude-haiku-4-5"
    assert est.escalate_model == "claude-sonnet-5"


# ---------- run_classify ----------

def test_run_classify_writes_rows_and_reports_progress():
    conn = make_conn()
    insert_message(conn, "m1", "shop@store.com", subject="Your order has shipped", internal_ts=1000)
    insert_message(conn, "m2", "biz@company.com", subject="50% off sale!", internal_ts=1000)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)
    job = FakeJob()

    result = org.run_classify(job, ClassifyRequest(scope="all", days=0, limit=None, reclassify=False))

    assert result["classified"] == 2
    assert result["escalated"] == 0
    assert "usage" in result
    assert job.progress_calls  # on_progress was invoked at least once

    rows = {
        r["message_id"]: r
        for r in conn.execute("SELECT * FROM classifications").fetchall()
    }
    assert rows["m1"]["category"] == "receipts"
    assert rows["m1"]["model"] == "fake"
    assert rows["m1"]["applied"] == 0
    assert rows["m1"]["overridden"] == 0
    assert rows["m2"]["category"] == "skip"


def test_run_classify_upserts_on_reclassify():
    conn = make_conn()
    # m1 was classified automatically; m2 was set by hand (overridden=1).
    insert_message(conn, "m1", "shop@store.com", subject="Your order has shipped", internal_ts=1000)
    insert_classification(conn, "m1", "personal", confidence=0.5, applied=1, overridden=0)
    insert_message(conn, "m2", "shop@store.com", subject="Your order has shipped", internal_ts=1001)
    insert_classification(conn, "m2", "personal", confidence=1.0, applied=1, overridden=1)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)
    job = FakeJob()

    org.run_classify(job, ClassifyRequest(scope="all", days=0, limit=None, reclassify=True))

    row = conn.execute("SELECT * FROM classifications WHERE message_id = 'm1'").fetchone()
    assert row["category"] == "receipts"
    assert row["applied"] == 0
    assert row["overridden"] == 0

    # A reclassify never discards a category the user set by hand.
    row2 = conn.execute("SELECT * FROM classifications WHERE message_id = 'm2'").fetchone()
    assert row2["category"] == "personal"
    assert row2["overridden"] == 1
    assert row2["applied"] == 1


# ---------- results ----------

def test_results_summary_filters_and_pagination():
    conn = make_conn()
    for i in range(3):
        insert_message(conn, f"f{i}", "a@example.com", subject=f"finance {i}", internal_ts=1000 + i)
        insert_classification(conn, f"f{i}", "finance", confidence=0.9, created_at=1000 + i)
    for i in range(2):
        insert_message(conn, f"r{i}", "b@example.com", subject=f"receipt {i}", internal_ts=2000 + i)
        insert_classification(conn, f"r{i}", "receipts", confidence=0.4, created_at=2000 + i)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    all_results = org.results(category=None, min_confidence=None, limit=100, offset=0)
    assert all_results.total == 5
    assert all_results.summary == {"finance": 3, "receipts": 2}

    finance_only = org.results(category="finance", min_confidence=None, limit=100, offset=0)
    assert finance_only.total == 3
    assert all(item.category == "finance" for item in finance_only.items)
    # summary always reflects the whole table, not the category filter.
    assert finance_only.summary == {"finance": 3, "receipts": 2}

    high_confidence = org.results(category=None, min_confidence=0.8, limit=100, offset=0)
    assert high_confidence.total == 3

    page1 = org.results(category=None, min_confidence=None, limit=2, offset=0)
    page2 = org.results(category=None, min_confidence=None, limit=2, offset=2)
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert {i.message_id for i in page1.items}.isdisjoint({i.message_id for i in page2.items})


def test_override_creates_or_updates_row():
    conn = make_conn()
    insert_message(conn, "m1", "a@example.com", internal_ts=1000)
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)

    # No prior classification row: override creates one.
    org.override("m1", "travel")
    row = conn.execute("SELECT * FROM classifications WHERE message_id = 'm1'").fetchone()
    assert row["category"] == "travel"
    assert row["confidence"] == 1.0
    assert row["overridden"] == 1
    assert row["applied"] == 0

    # Existing row (already applied): override resets applied and confidence.
    insert_message(conn, "m2", "a@example.com", internal_ts=1000)
    insert_classification(conn, "m2", "finance", confidence=0.7, applied=1, overridden=0)
    org.override("m2", "personal")
    row2 = conn.execute("SELECT * FROM classifications WHERE message_id = 'm2'").fetchone()
    assert row2["category"] == "personal"
    assert row2["confidence"] == 1.0
    assert row2["overridden"] == 1
    assert row2["applied"] == 0


# ---------- run_apply ----------

def _setup_apply_scenario(conn, demo=False):
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(demo=demo), fake_classifier_factory)
    org.get_categories()  # seed defaults

    insert_message(conn, "f1", "a@example.com", internal_ts=1000)
    insert_message(conn, "f2", "a@example.com", internal_ts=1000)
    insert_classification(conn, "f1", "finance", applied=0)
    insert_classification(conn, "f2", "finance", applied=0)

    insert_message(conn, "r1", "b@example.com", internal_ts=1000)
    insert_classification(conn, "r1", "receipts", applied=0)

    insert_message(conn, "s1", "c@example.com", internal_ts=1000)
    insert_classification(conn, "s1", "skip", applied=0)

    return store, org


def test_run_apply_labels_archives_and_marks_read():
    conn = make_conn()
    store, org = _setup_apply_scenario(conn, demo=False)
    job = FakeJob()

    result = org.run_apply(job, ApplyRequest(categories=None, archive=True, mark_read=True))

    assert result["labeled"] == 3  # f1, f2, r1 (skip excluded)
    assert result["by_category"] == {"finance": 2, "receipts": 1}

    gmail = org.gmail
    assert set(gmail.labels.keys()) == {"Finance", "Receipts & Orders"}
    # remove list includes INBOX and UNREAD for every batch_modify call.
    for call in gmail.batch_modify_calls:
        assert set(call["remove"]) == {"INBOX", "UNREAD"}

    # Each category's batch_modify call adds that category's label id.
    calls_by_add = {tuple(c["add"]): c for c in gmail.batch_modify_calls}
    assert (gmail.labels["Finance"],) in calls_by_add
    assert (gmail.labels["Receipts & Orders"],) in calls_by_add

    rows = {r["message_id"]: r for r in conn.execute("SELECT * FROM classifications").fetchall()}
    assert rows["f1"]["applied"] == 1
    assert rows["f2"]["applied"] == 1
    assert rows["r1"]["applied"] == 1
    assert rows["s1"]["applied"] == 0  # skip category untouched

    messages = {r["id"]: r for r in conn.execute("SELECT * FROM messages").fetchall()}
    assert messages["f1"]["in_inbox"] == 0
    assert messages["f1"]["is_unread"] == 0
    assert messages["s1"]["in_inbox"] == 1  # never touched

    # One log_action per category actually applied.
    logged_categories = {entry["detail"]["category"] for entry in store.logged_actions}
    assert logged_categories == {"finance", "receipts"}
    assert all(entry["status"] == "done" for entry in store.logged_actions)
    assert all(entry["undoable"] is False for entry in store.logged_actions)


def test_run_apply_logs_simulated_in_demo_mode():
    conn = make_conn()
    store, org = _setup_apply_scenario(conn, demo=True)
    job = FakeJob()

    org.run_apply(job, ApplyRequest(categories=None, archive=False, mark_read=False))

    assert store.logged_actions
    assert all(entry["status"] == "simulated" for entry in store.logged_actions)

    # archive/mark_read both False: no INBOX/UNREAD removal, and messages
    # table stays untouched.
    gmail = org.gmail
    for call in gmail.batch_modify_calls:
        assert call["remove"] is None
    messages = {r["id"]: r for r in conn.execute("SELECT * FROM messages").fetchall()}
    assert messages["f1"]["in_inbox"] == 1
    assert messages["f1"]["is_unread"] == 1


def test_run_apply_respects_categories_filter():
    conn = make_conn()
    store, org = _setup_apply_scenario(conn, demo=False)
    job = FakeJob()

    result = org.run_apply(job, ApplyRequest(categories=["finance"], archive=False, mark_read=False))

    assert result["by_category"] == {"finance": 2}
    rows = {r["message_id"]: r for r in conn.execute("SELECT * FROM classifications").fetchall()}
    assert rows["r1"]["applied"] == 0  # not in the requested categories


def test_run_apply_chunks_batch_modify_at_1000():
    conn = make_conn()
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)
    org.get_categories()

    for i in range(1500):
        insert_classification(conn, f"m{i}", "finance", applied=0)

    job = FakeJob()
    result = org.run_apply(job, ApplyRequest(categories=["finance"], archive=False, mark_read=False))

    assert result["by_category"]["finance"] == 1500
    gmail = org.gmail
    finance_calls = [c for c in gmail.batch_modify_calls]
    assert len(finance_calls) == 2
    assert len(finance_calls[0]["ids"]) == 1000
    assert len(finance_calls[1]["ids"]) == 500


def test_run_apply_skips_skip_category_entirely():
    conn = make_conn()
    store = FakeStore(conn)
    org = Organizer(store, FakeGmail(), make_config(), fake_classifier_factory)
    org.get_categories()
    insert_classification(conn, "s1", "skip", applied=0)

    job = FakeJob()
    result = org.run_apply(job, ApplyRequest(categories=["skip"], archive=False, mark_read=False))

    assert result == {"labeled": 0, "by_category": {}}
    assert org.gmail.batch_modify_calls == []
    assert org.gmail.labels == {}
