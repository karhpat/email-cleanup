"""Organize: category selection, cost estimate, classification, apply labels.

See docs/ARCHITECTURE.md -> "Organize semantics" and the default categories
table for the exact rules this module implements. All SQL here targets the
schema documented there (tables `messages`, `classifications`, `categories`).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

from backend.classifier import EmailForClassification
from backend.models import (
    ApplyRequest,
    CategoryDef,
    ClassifyRequest,
    OrganizeEstimate,
    OrganizeResultItem,
    OrganizeResults,
)

APPLY_CHUNK_SIZE = 1000

_DEFAULT_CATEGORIES: list[tuple[str, Optional[str], str]] = [
    ("finance", "Finance", "bank and card statements, transaction alerts, investments, taxes, insurance"),
    ("receipts", "Receipts & Orders", "purchase confirmations, shipping, returns, invoices"),
    ("travel", "Travel", "flights, hotels, rentals, itineraries, loyalty programs"),
    ("health", "Health", "doctors, appointments, pharmacy, insurance claims, fitness"),
    ("home", "Home", "utilities, mortgage or rent, home services, HOA, real estate"),
    ("work", "Work & Career", "job applications, recruiters, professional correspondence"),
    ("personal", "Personal", "real people writing to you: friends, family, acquaintances"),
    ("government", "Government & Legal", "taxes, DMV, tolls, legal notices, voting"),
    ("accounts", "Accounts & Security", "password resets, sign-in alerts, account notices, subscriptions billing"),
    ("skip", None, "marketing or newsletters that slipped through, or nothing worth labeling"),
]


def _iso(ts: Optional[int]) -> str:
    if ts is None:
        ts = 0
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Organizer:
    def __init__(self, store, gmail, config, classifier_factory: Callable[[list[CategoryDef]], object]):
        self.store = store
        self.gmail = gmail
        self.config = config
        self.classifier_factory = classifier_factory

    # ---------- categories ----------

    @staticmethod
    def default_categories() -> list[CategoryDef]:
        return [
            CategoryDef(key=key, label_name=label_name, description=description)
            for key, label_name, description in _DEFAULT_CATEGORIES
        ]

    def get_categories(self) -> list[CategoryDef]:
        rows = self._fetch_categories()
        if not rows:
            self._seed_default_categories()
            rows = self._fetch_categories()
        return rows

    def set_categories(self, items: list[CategoryDef]) -> list[CategoryDef]:
        items = list(items)
        if not any(c.key == "skip" for c in items):
            skip_default = next(c for c in self.default_categories() if c.key == "skip")
            items.append(skip_default)

        with self.store.lock:
            cur = self.store.conn.cursor()
            cur.execute("DELETE FROM categories")
            for i, c in enumerate(items):
                cur.execute(
                    "INSERT INTO categories (key, label_name, description, sort_order) VALUES (?, ?, ?, ?)",
                    (c.key, c.label_name, c.description, i),
                )
            self.store.conn.commit()

        return self.get_categories()

    def _fetch_categories(self) -> list[CategoryDef]:
        rows = self.store.conn.execute(
            "SELECT key, label_name, description FROM categories ORDER BY sort_order"
        ).fetchall()
        return [
            CategoryDef(key=r["key"], label_name=r["label_name"], description=r["description"] or "")
            for r in rows
        ]

    def _seed_default_categories(self) -> None:
        with self.store.lock:
            cur = self.store.conn.cursor()
            for i, c in enumerate(self.default_categories()):
                cur.execute(
                    "INSERT INTO categories (key, label_name, description, sort_order) VALUES (?, ?, ?, ?)",
                    (c.key, c.label_name, c.description, i),
                )
            self.store.conn.commit()

    # ---------- candidate selection ----------

    def _candidate_conditions(self, scope: str, days: int) -> tuple[list[str], list]:
        conditions = ["m.gone = 0", "m.in_trash = 0"]
        params: list = []
        if scope == "inbox":
            conditions.append("m.in_inbox = 1")
        if days and days > 0:
            cutoff = int(time.time()) - days * 86400
            conditions.append("m.internal_ts >= ?")
            params.append(cutoff)
        return conditions, params

    def candidates(
        self,
        scope: str,
        days: int,
        limit: Optional[int],
        reclassify: bool,
    ) -> list[EmailForClassification]:
        conditions, params = self._candidate_conditions(scope, days)
        if not reclassify:
            conditions.append("c.message_id IS NULL")
        else:
            # A reclassify never discards a category the user set by hand.
            conditions.append("(c.message_id IS NULL OR c.overridden = 0)")

        sql = f"""
            SELECT m.id, m.sender, m.sender_name, m.subject, m.snippet, m.internal_ts
            FROM messages m
            LEFT JOIN classifications c ON c.message_id = m.id
            WHERE {' AND '.join(conditions)}
            ORDER BY m.internal_ts ASC, m.id ASC
        """
        rows = self.store.conn.execute(sql, params).fetchall()

        excluded_senders = self.store.subscription_senders()

        out: list[EmailForClassification] = []
        for r in rows:
            if r["sender"] in excluded_senders:
                continue
            out.append(EmailForClassification(
                message_id=r["id"],
                sender=r["sender"],
                display_name=r["sender_name"] or "",
                subject=r["subject"] or "",
                snippet=r["snippet"] or "",
                date=_iso(r["internal_ts"]),
            ))
            if limit is not None and len(out) >= limit:
                break

        return out

    def estimate(self, scope: str, days: int) -> OrganizeEstimate:
        conditions, params = self._candidate_conditions(scope, days)

        sql = f"""
            SELECT m.sender, c.message_id AS classified_id
            FROM messages m
            LEFT JOIN classifications c ON c.message_id = m.id
            WHERE {' AND '.join(conditions)}
        """
        rows = self.store.conn.execute(sql, params).fetchall()
        excluded_senders = self.store.subscription_senders()

        candidates_n = 0
        already_classified_n = 0
        for r in rows:
            if r["sender"] in excluded_senders:
                continue
            if r["classified_id"] is not None:
                already_classified_n += 1
            else:
                candidates_n += 1

        categories = self.get_categories()
        classifier = self.classifier_factory(categories)
        est_cost = classifier.estimate_cost_usd(candidates_n)

        return OrganizeEstimate(
            candidates=candidates_n,
            already_classified=already_classified_n,
            est_cost_usd=est_cost,
            model=self.config.classify_model,
            escalate_model=self.config.escalate_model,
        )

    # ---------- classify ----------

    def run_classify(self, job, req: ClassifyRequest) -> dict:
        categories = self.get_categories()
        classifier = self.classifier_factory(categories)

        emails = self.candidates(req.scope, req.days, req.limit, req.reclassify)

        results = classifier.classify(
            emails,
            on_progress=lambda done, total, message: job.progress(done, total, message),
            should_cancel=lambda: job.cancelled,
        )

        now = int(time.time())
        with self.store.lock:
            cur = self.store.conn.cursor()
            for r in results:
                cur.execute(
                    """
                    INSERT INTO classifications
                        (message_id, category, confidence, model, reason, applied, overridden, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        category = excluded.category,
                        confidence = excluded.confidence,
                        model = excluded.model,
                        reason = excluded.reason,
                        applied = 0,
                        overridden = 0,
                        created_at = excluded.created_at
                    """,
                    (r.message_id, r.category, r.confidence, r.model, r.reason, now),
                )
            self.store.conn.commit()

        usage = dict(getattr(classifier, "usage", {}))
        escalated = usage.get("escalated", 0)

        return {"classified": len(results), "escalated": escalated, "usage": usage}

    # ---------- results ----------

    def results(
        self,
        category: Optional[str],
        min_confidence: Optional[float],
        limit: int,
        offset: int,
    ) -> OrganizeResults:
        conditions = []
        params: list = []
        if category is not None:
            conditions.append("c.category = ?")
            params.append(category)
        if min_confidence is not None:
            conditions.append("c.confidence >= ?")
            params.append(min_confidence)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        total = self.store.conn.execute(
            f"SELECT COUNT(*) FROM classifications c {where_clause}", params
        ).fetchone()[0]

        rows = self.store.conn.execute(
            f"""
            SELECT c.message_id, c.category, c.confidence, c.model, c.reason,
                   c.applied, c.overridden, m.sender, m.sender_name, m.subject, m.internal_ts
            FROM classifications c
            JOIN messages m ON m.id = c.message_id
            {where_clause}
            ORDER BY m.internal_ts DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()

        items = [
            OrganizeResultItem(
                message_id=r["message_id"],
                sender=r["sender"],
                display_name=r["sender_name"] or "",
                subject=r["subject"] or "",
                date=_iso(r["internal_ts"]),
                category=r["category"],
                confidence=r["confidence"],
                model=r["model"] or "",
                reason=r["reason"] or "",
                applied=bool(r["applied"]),
                overridden=bool(r["overridden"]),
            )
            for r in rows
        ]

        summary_rows = self.store.conn.execute(
            "SELECT category, COUNT(*) AS n FROM classifications GROUP BY category"
        ).fetchall()
        summary = {r["category"]: r["n"] for r in summary_rows}

        return OrganizeResults(total=total, summary=summary, items=items)

    def override(self, message_id: str, category: str) -> None:
        now = int(time.time())
        with self.store.lock:
            self.store.conn.execute(
                """
                INSERT INTO classifications
                    (message_id, category, confidence, model, reason, applied, overridden, created_at)
                VALUES (?, ?, 1.0, '', '', 0, 1, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    category = excluded.category,
                    confidence = 1.0,
                    overridden = 1,
                    applied = 0
                """,
                (message_id, category, now),
            )
            self.store.conn.commit()

    # ---------- apply ----------

    def run_apply(self, job, req: ApplyRequest) -> dict:
        categories = {c.key: c for c in self.get_categories()}

        if req.categories is not None:
            target_keys = [k for k in req.categories if k != "skip"]
        else:
            target_keys = [key for key in categories if key != "skip"]

        labeled_total = 0
        by_category: dict[str, int] = {}
        total_targets = len(target_keys)

        remove_labels = []
        if req.archive:
            remove_labels.append("INBOX")
        if req.mark_read:
            remove_labels.append("UNREAD")

        for i, key in enumerate(target_keys):
            if job.cancelled:
                break

            cat = categories.get(key)
            if cat is None or cat.label_name is None:
                job.progress(i + 1, total_targets, f"skipped {key}")
                continue

            rows = self.store.conn.execute(
                "SELECT message_id FROM classifications WHERE category = ? AND applied = 0",
                (key,),
            ).fetchall()
            message_ids = [r["message_id"] for r in rows]

            if not message_ids:
                job.progress(i + 1, total_targets, f"{key}: nothing to apply")
                continue

            label_id = self.gmail.ensure_label(cat.label_name)

            for j in range(0, len(message_ids), APPLY_CHUNK_SIZE):
                chunk = message_ids[j:j + APPLY_CHUNK_SIZE]
                self.gmail.batch_modify(chunk, add=[label_id], remove=remove_labels or None)

            with self.store.lock:
                cur = self.store.conn.cursor()
                placeholders = ",".join("?" * len(message_ids))
                cur.execute(
                    f"UPDATE classifications SET applied = 1 WHERE message_id IN ({placeholders})",
                    message_ids,
                )
                if req.archive:
                    cur.execute(
                        f"UPDATE messages SET in_inbox = 0 WHERE id IN ({placeholders})",
                        message_ids,
                    )
                if req.mark_read:
                    cur.execute(
                        f"UPDATE messages SET is_unread = 0 WHERE id IN ({placeholders})",
                        message_ids,
                    )
                self.store.conn.commit()

                status = "simulated" if self.config.demo else "done"
                self.store.log_action(
                    "apply_labels",
                    None,
                    {"category": key, "label": cat.label_name},
                    status,
                    message_ids=message_ids,
                    undoable=False,
                )

            labeled_total += len(message_ids)
            by_category[key] = len(message_ids)
            job.progress(i + 1, total_targets, f"{key}: labeled {len(message_ids)}")

        return {"labeled": labeled_total, "by_category": by_category}
