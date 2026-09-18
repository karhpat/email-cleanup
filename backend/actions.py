"""Unsubscribe / block / purge / keep / undo.

See docs/ARCHITECTURE.md "Unsubscribe mechanics" and "Action semantics".
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse

from backend.models import ActionPreview, ActionPreviewItem, ActionRequest

UnsubscribeKind = Literal["one_click", "mailto", "http", "none"]

_ANGLE_RE = re.compile(r"<([^>]+)>")


def parse_list_unsubscribe(header: str | None) -> list[str]:
    """Parse a (comma-separated, angle-bracketed) List-Unsubscribe header into URIs.

    Tolerates a header with no angle brackets (a single bare URI) too.
    """
    if not header or not header.strip():
        return []
    matches = _ANGLE_RE.findall(header)
    if matches:
        return [m.strip() for m in matches if m.strip()]
    return [p.strip() for p in header.split(",") if p.strip()]


def unsubscribe_kind(list_unsub: str | None, list_unsub_post: str | None) -> UnsubscribeKind:
    uris = parse_list_unsubscribe(list_unsub)
    if not uris:
        return "none"
    has_https = any(u.lower().startswith("https://") for u in uris)
    has_http = any(u.lower().startswith("http://") for u in uris)
    has_mailto = any(u.lower().startswith("mailto:") for u in uris)
    post = (list_unsub_post or "").strip().lower().replace(" ", "")
    is_one_click_post = post == "list-unsubscribe=one-click"

    if is_one_click_post and has_https:
        return "one_click"
    if has_mailto:
        return "mailto"
    if has_https or has_http:
        return "http"
    return "none"


def parse_mailto(uri: str) -> tuple[str, str, str]:
    """(address, subject, body) from a mailto: URI, defaulting subject/body to 'unsubscribe'."""
    if not uri:
        return "", "unsubscribe", "unsubscribe"
    parsed = urlparse(uri)
    address = unquote(parsed.path or "")
    qs = parse_qs(parsed.query)
    subject = unquote(qs.get("subject", ["unsubscribe"])[0]) or "unsubscribe"
    body = unquote(qs.get("body", ["unsubscribe"])[0]) or "unsubscribe"
    return address, subject, body


def _first(uris: list[str], *prefixes: str) -> str | None:
    for u in uris:
        if u.lower().startswith(prefixes):
            return u
    return None


def preview(store, req: ActionRequest) -> ActionPreview:
    stats = store.sender_stats_for(req.senders)
    items: list[ActionPreviewItem] = []
    totals = {
        "senders": 0,
        "will_unsubscribe": 0,
        "will_block": 0,
        "purge_count": 0,
        "skipped_starred": 0,
        "skipped_important": 0,
    }

    for sender in req.senders:
        s = stats.get(sender)
        display_name = s.display_name if s else ""
        kind: UnsubscribeKind = s.unsubscribe_kind if s else "none"
        item = ActionPreviewItem(sender=sender, display_name=display_name)

        if req.action == "stop":
            item.unsubscribe_method = kind
            item.will_unsubscribe = bool(req.unsubscribe and kind != "none")
            item.will_block = bool(req.block)
        elif req.action == "purge":
            cand = store.purge_candidates(sender, req.purge_older_than_days, req.skip_important)
            item.purge_count = len(cand["ids"])
            item.skipped_starred = cand["skipped_starred"]
            item.skipped_important = cand["skipped_important"]

        items.append(item)
        totals["senders"] += 1
        totals["will_unsubscribe"] += int(item.will_unsubscribe)
        totals["will_block"] += int(item.will_block)
        totals["purge_count"] += item.purge_count
        totals["skipped_starred"] += item.skipped_starred
        totals["skipped_important"] += item.skipped_important

    return ActionPreview(action=req.action, items=items, totals=totals)


def run_execute(job, store, gmail, req: ActionRequest, demo: bool) -> dict:
    senders = list(req.senders)
    processed = 0
    failed = 0

    for i, sender in enumerate(senders):
        if job.cancelled:
            break
        job.progress(i, len(senders), f"{req.action}: {sender}")
        try:
            if req.action == "stop":
                _do_stop(store, gmail, sender, req, demo)
            elif req.action == "purge":
                _do_purge(store, gmail, sender, req, demo)
            elif req.action == "keep":
                store.set_sender_status(sender, "kept")
                store.log_action("keep", sender, {}, "simulated" if demo else "done", undoable=True, job_id=job.id)
            elif req.action == "reactivate":
                store.set_sender_status(sender, "active")
                store.log_action("reactivate", sender, {}, "simulated" if demo else "done", job_id=job.id)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - one sender's failure must not stop the rest
            failed += 1
            store.log_action(req.action, sender, {"error": str(exc)}, "failed", job_id=job.id)
        job.progress(i + 1, len(senders), f"{req.action}: {sender}")

    return {"processed": processed, "failed": failed, "total": len(senders)}


def _do_stop(store, gmail, sender: str, req: ActionRequest, demo: bool) -> None:
    did_block = False
    did_unsub = False

    if req.unsubscribe:
        list_unsub, list_unsub_post = store.sender_unsubscribe_headers(sender)
        kind = unsubscribe_kind(list_unsub, list_unsub_post)
        uris = parse_list_unsubscribe(list_unsub)
        if kind == "one_click":
            url = _first(uris, "https://")
            ok = gmail.one_click_unsubscribe(url)
            status = "simulated" if demo else ("done" if ok else "failed")
            store.log_action("unsubscribe", sender, {"kind": kind, "url": url}, status)
            did_unsub = status in ("done", "simulated")
        elif kind == "mailto":
            mailto_uri = _first(uris, "mailto:")
            address, subject, body = parse_mailto(mailto_uri or "")
            gmail.send_mail(address, subject, body)
            status = "simulated" if demo else "done"
            store.log_action("unsubscribe", sender, {"kind": kind, "to": address}, status)
            did_unsub = True
        elif kind == "http":
            url = _first(uris, "http://", "https://")
            store.log_action("unsubscribe", sender, {"kind": kind, "url": url}, "manual")
        # kind == "none": nothing to do, nothing to log

    if req.block:
        filter_id = gmail.create_filter(sender)
        status = "simulated" if demo else "done"
        store.log_action("block", sender, {"filter_id": filter_id}, status, undoable=True)
        did_block = True

    if did_block:
        store.set_sender_status(sender, "blocked")
    elif did_unsub:
        store.set_sender_status(sender, "unsubscribed")


def _do_purge(store, gmail, sender: str, req: ActionRequest, demo: bool) -> None:
    cand = store.purge_candidates(sender, req.purge_older_than_days, req.skip_important)
    ids = cand["ids"]
    if ids:
        gmail.trash(ids)
        store.set_in_trash(ids, True)
    status = "simulated" if demo else "done"
    store.log_action(
        "purge",
        sender,
        {
            "skipped_starred": cand["skipped_starred"],
            "skipped_important": cand["skipped_important"],
            "purge_older_than_days": req.purge_older_than_days,
        },
        status,
        message_ids=ids,
        undoable=True,
    )


def undo(store, gmail, log_id: int, demo: bool) -> None:
    row = store.get_action(log_id)
    if row is None:
        raise ValueError(f"no such action log entry: {log_id}")
    if row["action"] == "unsubscribe":
        raise ValueError("cannot undo an unsubscribe")
    if not row["undoable"] or row["undone"]:
        raise ValueError("action is not undoable")

    action = row["action"]
    sender = row["sender"]
    detail = row["detail"] or {}
    message_ids = row["message_ids"] or []

    if action == "purge":
        if message_ids:
            gmail.untrash(message_ids)
            store.set_in_trash(message_ids, False)
    elif action == "block":
        filter_id = detail.get("filter_id")
        if filter_id:
            gmail.delete_filter(filter_id)
        if sender:
            store.set_sender_status(sender, "active")
    elif action == "keep":
        if sender:
            store.set_sender_status(sender, "active")
    else:
        raise ValueError(f"cannot undo action '{action}'")

    store.mark_undone(log_id)
