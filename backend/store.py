"""SQLite schema, queries, and sender aggregation.

See docs/ARCHITECTURE.md for the schema and derivation rules this module
implements. This file owns all SQL; callers never touch sqlite3 directly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.models import (
    ActionLogEntry,
    MessageSummary,
    OverviewResponse,
    SenderFilter,
    SenderStats,
    TopSender,
)

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_messages_gone_trash ON messages(gone, in_trash);
CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category);
CREATE TABLE IF NOT EXISTS sender_status (sender TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, action TEXT, sender TEXT, detail TEXT,
  status TEXT, message_ids TEXT,
  undoable INTEGER DEFAULT 0, undone INTEGER DEFAULT 0, job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_log_ts ON action_log(ts);
CREATE TABLE IF NOT EXISTS classifications (
  message_id TEXT PRIMARY KEY, category TEXT, confidence REAL, model TEXT, reason TEXT,
  applied INTEGER DEFAULT 0, overridden INTEGER DEFAULT 0, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS categories (key TEXT PRIMARY KEY, label_name TEXT, description TEXT, sort_order INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scan_state (key TEXT PRIMARY KEY, value TEXT);
-- extra table (not part of the frozen contract) used for resumable scans:
-- ids collected during the listing phase, drained during the fetch phase.
CREATE TABLE IF NOT EXISTS scan_pending (id TEXT PRIMARY KEY, thread_id TEXT, fetched INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_scan_pending_fetched ON scan_pending(fetched);
"""

MESSAGE_COLUMNS = [
    "id", "thread_id", "sender", "sender_name", "sender_raw", "domain", "subject", "snippet",
    "internal_ts", "is_unread", "is_starred", "is_important", "in_inbox", "in_trash", "labels",
    "category", "list_id", "list_unsubscribe", "list_unsubscribe_post", "size", "fetched_at", "gone",
]

_SQL_CHUNK = 500  # keep well under SQLite's default 999 bound-parameter limit


def normalize_sender(from_header: str) -> tuple[str, str]:
    """Parse a From header into (normalized email, display name).

    Lowercases the email, strips a `+tag` from the local part. See
    docs/ARCHITECTURE.md "Sender normalization".
    """
    if not from_header:
        return "", ""
    display_name, addr = parseaddr(from_header)
    addr = (addr or "").strip().lower()
    if "@" in addr:
        local, _, domain = addr.partition("@")
        local = local.split("+", 1)[0]
        addr = f"{local}@{domain}"
    return addr, display_name or ""


def _chunks(items: list, size: int = _SQL_CHUNK) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._local = threading.local()
        # An in-memory database only exists on one connection, so share it.
        self._shared: Optional[sqlite3.Connection] = (
            self._connect() if self.path == ":memory:" else None
        )
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """One connection per thread.

        The web server answers requests from a thread pool while a job thread
        writes. WAL mode lets those readers run alongside the writer, and
        `lock` serialises the writers themselves.
        """
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # ---------------------------------------------------------------- messages

    @staticmethod
    def _row_defaults(row: dict) -> dict:
        labels = row.get("labels")
        if isinstance(labels, (list, tuple)):
            labels = json.dumps(list(labels))
        return {
            "id": row["id"],
            "thread_id": row.get("thread_id"),
            "sender": row.get("sender", ""),
            "sender_name": row.get("sender_name"),
            "sender_raw": row.get("sender_raw"),
            "domain": row.get("domain"),
            "subject": row.get("subject"),
            "snippet": row.get("snippet"),
            "internal_ts": row.get("internal_ts", 0),
            "is_unread": int(bool(row.get("is_unread", 1))),
            "is_starred": int(bool(row.get("is_starred", 0))),
            "is_important": int(bool(row.get("is_important", 0))),
            "in_inbox": int(bool(row.get("in_inbox", 1))),
            "in_trash": int(bool(row.get("in_trash", 0))),
            "labels": labels,
            "category": row.get("category") or "unknown",
            "list_id": row.get("list_id"),
            "list_unsubscribe": row.get("list_unsubscribe"),
            "list_unsubscribe_post": row.get("list_unsubscribe_post"),
            "size": row.get("size"),
            "fetched_at": row.get("fetched_at") or int(time.time()),
            "gone": int(bool(row.get("gone", 0))),
        }

    def upsert_messages(self, rows: list[dict]) -> None:
        if not rows:
            return
        payload = [self._row_defaults(r) for r in rows]
        cols = MESSAGE_COLUMNS
        sql = (
            f"INSERT INTO messages ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        )
        with self.lock:
            self.conn.executemany(sql, payload)
            self.conn.commit()

    def known_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        out: set[str] = set()
        with self.lock:
            for chunk in _chunks(ids):
                q = f"SELECT id FROM messages WHERE id IN ({', '.join('?' * len(chunk))})"
                out.update(r["id"] for r in self.conn.execute(q, chunk))
        return out

    def mark_read_state(self, read_ids: set[str], window_start_ts: int) -> None:
        with self.lock:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_read (id TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM tmp_read")
            if read_ids:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO tmp_read (id) VALUES (?)", [(i,) for i in read_ids]
                )
            self.conn.execute(
                "UPDATE messages SET is_unread=0 WHERE id IN (SELECT id FROM tmp_read)"
            )
            self.conn.execute(
                "UPDATE messages SET is_unread=1 "
                "WHERE internal_ts >= ? AND gone=0 AND id NOT IN (SELECT id FROM tmp_read)",
                (window_start_ts,),
            )
            self.conn.execute("DELETE FROM tmp_read")
            self.conn.commit()

    def mark_gone(self, present_ids: set[str], window_start_ts: int) -> None:
        with self.lock:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS tmp_present (id TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM tmp_present")
            if present_ids:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO tmp_present (id) VALUES (?)", [(i,) for i in present_ids]
                )
            self.conn.execute(
                "UPDATE messages SET gone=1 "
                "WHERE internal_ts >= ? AND gone=0 AND id NOT IN (SELECT id FROM tmp_present)",
                (window_start_ts,),
            )
            # Anything the scan listed is present and, since the query excludes
            # Trash, not in Trash - even if we last saw it as gone or trashed.
            self.conn.execute(
                "UPDATE messages SET gone=0, in_trash=0 "
                "WHERE (gone=1 OR in_trash=1) AND id IN (SELECT id FROM tmp_present)"
            )
            self.conn.execute("DELETE FROM tmp_present")
            self.conn.commit()

    def counts(self) -> dict:
        with self.lock:
            n = self.conn.execute("SELECT COUNT(*) c FROM messages WHERE gone=0").fetchone()["c"]
            s = self.conn.execute(
                "SELECT COUNT(DISTINCT sender) c FROM messages WHERE gone=0"
            ).fetchone()["c"]
        return {"messages": n, "senders": s}

    # ------------------------------------------------------------- sender stats

    def _sender_universe(
        self, senders: Optional[list[str]], min_total: int = 1, max_open_rate: Optional[float] = None
    ) -> list[sqlite3.Row]:
        base = (
            "SELECT sender, COUNT(*) total, "
            "SUM(CASE WHEN is_unread=0 THEN 1 ELSE 0 END) opened, "
            "MIN(internal_ts) first_received, MAX(internal_ts) last_received, "
            "MAX(CASE WHEN is_unread=0 THEN internal_ts END) last_opened, "
            "SUM(in_inbox) inbox_count, SUM(is_starred) starred, SUM(is_important) important, "
            "MAX(CASE WHEN list_unsubscribe IS NOT NULL AND list_unsubscribe != '' THEN 1 ELSE 0 END) has_unsubscribe "
            "FROM messages WHERE gone=0 AND in_trash=0"
        )
        params: list[Any] = []
        if senders is not None:
            if not senders:
                return []
            placeholders = ", ".join("?" * len(senders))
            base += f" AND sender IN ({placeholders})"
            params.extend(senders)
        base += " GROUP BY sender HAVING total >= ?"
        params.append(min_total)
        if max_open_rate is not None:
            base += " AND (CAST(opened AS REAL) / total) <= ?"
            params.append(max_open_rate)
        with self.lock:
            return list(self.conn.execute(base, params))

    def _category_modes(self, senders: list[str]) -> dict[str, str]:
        if not senders:
            return {}
        modes: dict[str, str] = {}
        with self.lock:
            for chunk in _chunks(senders):
                placeholders = ", ".join("?" * len(chunk))
                rows = self.conn.execute(
                    "SELECT sender, category, COUNT(*) c FROM messages "
                    f"WHERE gone=0 AND in_trash=0 AND sender IN ({placeholders}) "
                    "GROUP BY sender, category",
                    chunk,
                )
                best: dict[str, tuple[int, str]] = {}
                for r in rows:
                    cat = r["category"] or "unknown"
                    cur = best.get(r["sender"])
                    if cur is None or r["c"] > cur[0] or (r["c"] == cur[0] and cat < cur[1]):
                        best[r["sender"]] = (r["c"], cat)
                for sender, (_, cat) in best.items():
                    modes[sender] = cat
        return modes

    def _latest_info(self, senders: list[str]) -> dict[str, dict]:
        """Most-recent-message fields and top-3 recent subjects per sender."""
        if not senders:
            return {}
        info: dict[str, dict] = {}
        with self.lock:
            for chunk in _chunks(senders):
                placeholders = ", ".join("?" * len(chunk))
                rows = self.conn.execute(
                    "SELECT sender, sender_name, domain, subject, list_id, internal_ts, "
                    "ROW_NUMBER() OVER (PARTITION BY sender ORDER BY internal_ts DESC) rn "
                    "FROM messages WHERE gone=0 AND in_trash=0 AND sender IN ({}) "
                    "".format(placeholders),
                    chunk,
                )
                for r in rows:
                    d = info.setdefault(
                        r["sender"], {"display_name": "", "domain": "", "list_id": None, "subjects": []}
                    )
                    if r["rn"] == 1:
                        d["display_name"] = r["sender_name"] or ""
                        d["domain"] = r["domain"] or ""
                        d["list_id"] = r["list_id"]
                    if r["rn"] <= 3:
                        d["subjects"].append((r["rn"], r["subject"] or ""))
        for d in info.values():
            d["subjects"] = [s for _, s in sorted(d["subjects"])]
        return info

    def _latest_unsub_info(self, senders: list[str]) -> dict[str, tuple[Optional[str], Optional[str]]]:
        if not senders:
            return {}
        out: dict[str, tuple[Optional[str], Optional[str]]] = {}
        with self.lock:
            for chunk in _chunks(senders):
                placeholders = ", ".join("?" * len(chunk))
                rows = self.conn.execute(
                    "SELECT sender, list_unsubscribe, list_unsubscribe_post, "
                    "ROW_NUMBER() OVER (PARTITION BY sender ORDER BY internal_ts DESC) rn "
                    "FROM messages WHERE gone=0 AND in_trash=0 AND sender IN ({}) "
                    "AND list_unsubscribe IS NOT NULL AND list_unsubscribe != ''".format(placeholders),
                    chunk,
                )
                for r in rows:
                    if r["rn"] == 1:
                        out[r["sender"]] = (r["list_unsubscribe"], r["list_unsubscribe_post"])
        return out

    def _sender_statuses(self, senders: Optional[list[str]] = None) -> dict[str, str]:
        with self.lock:
            if senders is None:
                rows = self.conn.execute("SELECT sender, status FROM sender_status")
            else:
                out: dict[str, str] = {}
                for chunk in _chunks(senders):
                    placeholders = ", ".join("?" * len(chunk))
                    rows2 = self.conn.execute(
                        f"SELECT sender, status FROM sender_status WHERE sender IN ({placeholders})",
                        chunk,
                    )
                    out.update({r["sender"]: r["status"] for r in rows2})
                return out
            return {r["sender"]: r["status"] for r in rows}

    def _build_sender_stats(
        self, base_rows: list[sqlite3.Row], now_ts: int
    ) -> dict[str, SenderStats]:
        from backend.actions import unsubscribe_kind

        senders = [r["sender"] for r in base_rows]
        cat_modes = self._category_modes(senders)
        latest = self._latest_info(senders)
        latest_unsub = self._latest_unsub_info(senders)
        statuses = self._sender_statuses(senders)
        out: dict[str, SenderStats] = {}
        for r in base_rows:
            sender = r["sender"]
            total = r["total"]
            opened = r["opened"] or 0
            first_received = r["first_received"]
            last_received = r["last_received"]
            last_opened = r["last_opened"]
            has_unsub = bool(r["has_unsubscribe"])
            category = cat_modes.get(sender, "unknown")
            li = latest.get(sender, {"display_name": "", "domain": "", "list_id": None, "subjects": []})
            lu = latest_unsub.get(sender)
            kind = unsubscribe_kind(lu[0], lu[1]) if lu else "none"
            months = max(1.0, (now_ts - first_received) / (86400 * 30.44))
            days_since_received = int((now_ts - last_received) // 86400)
            days_since_opened = int((now_ts - last_opened) // 86400) if last_opened is not None else None
            status = statuses.get(sender, "active")
            out[sender] = SenderStats(
                sender=sender,
                display_name=li["display_name"],
                domain=li["domain"],
                total=total,
                opened=opened,
                open_rate=(opened / total) if total else 0.0,
                first_received=_iso(first_received),
                last_received=_iso(last_received),
                last_opened=_iso(last_opened),
                days_since_last_received=days_since_received,
                days_since_last_opened=days_since_opened,
                per_month=total / months,
                has_unsubscribe=has_unsub,
                unsubscribe_kind=kind,
                list_id=li["list_id"],
                category=category,
                inbox_count=r["inbox_count"] or 0,
                starred=r["starred"] or 0,
                important=r["important"] or 0,
                is_subscription=bool(has_unsub or category == "promotions"),
                status=status,
                sample_subjects=li["subjects"],
            )
        return out

    def sender_stats(self, flt: SenderFilter, now_ts: int | None = None) -> tuple[int, list[SenderStats]]:
        now_ts = now_ts if now_ts is not None else int(time.time())
        base_rows = self._sender_universe(None, min_total=flt.min_total, max_open_rate=flt.max_open_rate)
        stats = self._build_sender_stats(base_rows, now_ts)
        items = list(stats.values())

        if flt.has_unsubscribe is not None:
            items = [s for s in items if s.has_unsubscribe == flt.has_unsubscribe]
        if flt.categories:
            items = [s for s in items if s.category in flt.categories]
        if flt.subscription_only:
            items = [s for s in items if s.is_subscription]
        if flt.search:
            needle = flt.search.lower()
            items = [
                s
                for s in items
                if needle in s.sender.lower()
                or needle in (s.display_name or "").lower()
                or needle in (s.domain or "").lower()
            ]
        if flt.status:
            items = [s for s in items if s.status in flt.status]
        if flt.last_opened_before_days is not None:
            def _keep(s: SenderStats) -> bool:
                if s.days_since_last_opened is None:
                    return flt.include_never_opened
                return s.days_since_last_opened >= flt.last_opened_before_days
            items = [s for s in items if _keep(s)]

        total = len(items)

        key_map = {
            "total": lambda s: s.total,
            "open_rate": lambda s: s.open_rate,
            "last_received": lambda s: s.last_received or "",
            "last_opened": lambda s: (s.last_opened is not None, s.last_opened or ""),
            "per_month": lambda s: s.per_month,
            "sender": lambda s: s.sender,
        }
        keyfn = key_map.get(flt.sort, key_map["total"])
        items.sort(key=keyfn, reverse=(flt.order == "desc"))
        page = items[flt.offset : flt.offset + flt.limit]
        return total, page

    def sender_stats_for(self, senders: list[str]) -> dict[str, SenderStats]:
        if not senders:
            return {}
        now_ts = int(time.time())
        base_rows = self._sender_universe(senders)
        return self._build_sender_stats(base_rows, now_ts)

    def subscription_senders(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT sender FROM messages WHERE gone=0 AND in_trash=0"
            )
            all_senders = [r["sender"] for r in rows]
        stats = self.sender_stats_for(all_senders)
        return {s for s, st in stats.items() if st.is_subscription}

    def sender_messages(self, sender: str, limit: int = 50) -> list[MessageSummary]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, thread_id, subject, internal_ts, is_unread, is_starred, is_important, "
                "category, in_inbox, in_trash FROM messages "
                "WHERE sender = ? AND gone = 0 ORDER BY internal_ts DESC LIMIT ?",
                (sender, limit),
            ).fetchall()
        return [
            MessageSummary(
                id=r["id"],
                thread_id=r["thread_id"],
                subject=r["subject"] or "",
                date=_iso(r["internal_ts"]),
                is_unread=bool(r["is_unread"]),
                is_starred=bool(r["is_starred"]),
                is_important=bool(r["is_important"]),
                category=r["category"] or "unknown",
                in_inbox=bool(r["in_inbox"]),
                in_trash=bool(r["in_trash"]),
            )
            for r in rows
        ]

    def purge_candidates(self, sender: str, older_than_days: int | None, skip_important: bool) -> dict:
        now_ts = int(time.time())
        with self.lock:
            if older_than_days:
                cutoff = now_ts - older_than_days * 86400
                rows = self.conn.execute(
                    "SELECT id, is_starred, is_important FROM messages "
                    "WHERE sender=? AND gone=0 AND in_trash=0 AND internal_ts < ?",
                    (sender, cutoff),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, is_starred, is_important FROM messages "
                    "WHERE sender=? AND gone=0 AND in_trash=0",
                    (sender,),
                ).fetchall()
        ids: list[str] = []
        skipped_starred = 0
        skipped_important = 0
        for r in rows:
            if r["is_starred"]:
                skipped_starred += 1
                continue
            if skip_important and r["is_important"]:
                skipped_important += 1
                continue
            ids.append(r["id"])
        return {"ids": ids, "skipped_starred": skipped_starred, "skipped_important": skipped_important}

    def sender_unsubscribe_headers(self, sender: str) -> tuple[Optional[str], Optional[str]]:
        """The list_unsubscribe/list_unsubscribe_post headers of the sender's most
        recent message that carries a List-Unsubscribe header (or (None, None))."""
        info = self._latest_unsub_info([sender])
        return info.get(sender, (None, None))

    def set_in_trash(self, ids: list[str], in_trash: bool) -> None:
        if not ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE messages SET in_trash=? WHERE id=?",
                [(int(in_trash), i) for i in ids],
            )
            self.conn.commit()

    # -------------------------------------------------------------- sender status

    def set_sender_status(self, sender: str, status: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO sender_status (sender, status, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(sender) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                (sender, status, int(time.time())),
            )
            self.conn.commit()

    def get_sender_status(self, sender: str) -> str:
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM sender_status WHERE sender=?", (sender,)
            ).fetchone()
        return row["status"] if row else "active"

    # ------------------------------------------------------------------ actions

    def log_action(
        self,
        action: str,
        sender: str | None,
        detail: dict,
        status: str,
        message_ids: list[str] | None = None,
        undoable: bool = False,
        job_id: str | None = None,
    ) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO action_log (ts, action, sender, detail, status, message_ids, undoable, undone, job_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    int(time.time()),
                    action,
                    sender,
                    json.dumps(detail or {}),
                    status,
                    json.dumps(message_ids or []),
                    int(bool(undoable)),
                    job_id,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def action_log(self, limit: int, offset: int) -> tuple[int, list[ActionLogEntry]]:
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) c FROM action_log").fetchone()["c"]
            rows = self.conn.execute(
                "SELECT * FROM action_log ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        items = []
        for r in rows:
            mids = json.loads(r["message_ids"] or "[]")
            items.append(
                ActionLogEntry(
                    id=r["id"],
                    ts=_iso(r["ts"]),
                    action=r["action"],
                    sender=r["sender"],
                    detail=json.loads(r["detail"] or "{}"),
                    status=r["status"],
                    message_count=len(mids),
                    undoable=bool(r["undoable"]),
                    undone=bool(r["undone"]),
                )
            )
        return total, items

    def get_action(self, log_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM action_log WHERE id=?", (log_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["detail"] = json.loads(d.get("detail") or "{}")
        d["message_ids"] = json.loads(d.get("message_ids") or "[]")
        return d

    def mark_undone(self, log_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE action_log SET undone=1 WHERE id=?", (log_id,))
            self.conn.commit()

    # ----------------------------------------------------------------- overview

    def overview(self, now_ts: int | None = None) -> OverviewResponse:
        now_ts = now_ts if now_ts is not None else int(time.time())
        with self.lock:
            messages_total = self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE gone=0 AND in_trash=0"
            ).fetchone()["c"]
            unread_total = self.conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE gone=0 AND in_trash=0 AND is_unread=1"
            ).fetchone()["c"]
            senders_total = self.conn.execute(
                "SELECT COUNT(DISTINCT sender) c FROM messages WHERE gone=0 AND in_trash=0"
            ).fetchone()["c"]
            by_cat_rows = self.conn.execute(
                "SELECT category, COUNT(*) c FROM messages WHERE gone=0 AND in_trash=0 GROUP BY category"
            ).fetchall()
            top_rows = self.conn.execute(
                "SELECT sender, sender_name, COUNT(*) total, "
                "SUM(CASE WHEN is_unread=0 THEN 1 ELSE 0 END) opened "
                "FROM messages WHERE gone=0 AND in_trash=0 GROUP BY sender ORDER BY total DESC LIMIT 10"
            ).fetchall()
            action_rows = self.conn.execute(
                "SELECT action, message_ids FROM action_log WHERE status IN ('done','simulated') AND undone=0"
            ).fetchall()

        by_category = {r["category"] or "unknown": r["c"] for r in by_cat_rows}
        top_senders = [
            TopSender(
                sender=r["sender"],
                display_name=r["sender_name"] or "",
                total=r["total"],
                open_rate=(r["opened"] / r["total"]) if r["total"] else 0.0,
            )
            for r in top_rows
        ]

        actions = {"unsubscribed": 0, "blocked": 0, "trashed_messages": 0, "labeled_messages": 0}
        for r in action_rows:
            mids = json.loads(r["message_ids"] or "[]")
            if r["action"] == "unsubscribe":
                actions["unsubscribed"] += 1
            elif r["action"] == "block":
                actions["blocked"] += 1
            elif r["action"] == "purge":
                actions["trashed_messages"] += len(mids)
            elif r["action"] == "apply_labels":
                actions["labeled_messages"] += len(mids)

        sub_senders = self.subscription_senders()
        sub_stats = self.sender_stats_for(list(sub_senders)) if sub_senders else {}
        subscription_messages = sum(s.total for s in sub_stats.values())

        all_senders = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT sender FROM messages WHERE gone=0 AND in_trash=0"
        )]
        all_stats = self.sender_stats_for(all_senders) if all_senders else {}
        never_opened_senders = sum(1 for s in all_stats.values() if s.opened == 0)

        return OverviewResponse(
            messages_total=messages_total,
            unread_total=unread_total,
            senders_total=senders_total,
            subscription_senders=len(sub_senders),
            subscription_messages=subscription_messages,
            never_opened_senders=never_opened_senders,
            by_category=by_category,
            top_senders=top_senders,
            actions=actions,
        )

    # ------------------------------------------------------------ settings/state

    def get_setting(self, key: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_setting(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    def get_scan_state(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM scan_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_scan_state(self, key, value) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO scan_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    # -------------------------------------------------- scan_pending (resumability)

    def scan_pending_add(self, pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO scan_pending (id, thread_id, fetched) VALUES (?, ?, 0)",
                pairs,
            )
            self.conn.commit()

    def scan_pending_mark_fetched(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE scan_pending SET fetched=1 WHERE id=?", [(i,) for i in ids]
            )
            self.conn.commit()

    def scan_pending_unfetched(self, limit: int) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id FROM scan_pending WHERE fetched=0 LIMIT ?", (limit,)
            ).fetchall()
        return [r["id"] for r in rows]

    def scan_pending_count(self) -> dict:
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) c FROM scan_pending").fetchone()["c"]
            fetched = self.conn.execute(
                "SELECT COUNT(*) c FROM scan_pending WHERE fetched=1"
            ).fetchone()["c"]
        return {"total": total, "fetched": fetched}

    def scan_pending_all_ids(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute("SELECT id FROM scan_pending").fetchall()
        return [r["id"] for r in rows]

    def scan_pending_clear(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM scan_pending")
            self.conn.commit()
