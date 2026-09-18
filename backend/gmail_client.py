"""OAuth + Gmail API wrapper, plus the demo stand-in.

See docs/ARCHITECTURE.md "Gmail API facts" for the exact calls this module
is allowed to make.
"""
from __future__ import annotations

import base64
import logging
import random
import time
from email.message import EmailMessage
from typing import Callable, Optional

import httpx

from backend.store import normalize_sender

log = logging.getLogger("backend.gmail_client")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

_CATEGORY_MAP = {
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
    "CATEGORY_PERSONAL": "primary",
}


def parse_metadata(raw: dict) -> dict:
    """Turn a raw messages.get(format=metadata) dict into a messages-table row."""
    headers = {}
    for h in (raw.get("payload") or {}).get("headers", []):
        headers[h.get("name", "").lower()] = h.get("value", "")

    from_header = headers.get("from", "")
    sender, sender_name = normalize_sender(from_header)
    domain = sender.split("@", 1)[1] if "@" in sender else ""
    label_ids = raw.get("labelIds") or []

    category = "unknown"
    for label, cat in _CATEGORY_MAP.items():
        if label in label_ids:
            category = cat
            break

    internal_date = raw.get("internalDate") or "0"
    try:
        internal_ts = int(internal_date) // 1000
    except (TypeError, ValueError):
        internal_ts = 0

    return {
        "id": raw.get("id"),
        "thread_id": raw.get("threadId"),
        "sender": sender,
        "sender_name": sender_name,
        "sender_raw": from_header,
        "domain": domain,
        "subject": headers.get("subject", ""),
        "snippet": raw.get("snippet", ""),
        "internal_ts": internal_ts,
        "is_unread": 1 if "UNREAD" in label_ids else 0,
        "is_starred": 1 if "STARRED" in label_ids else 0,
        "is_important": 1 if "IMPORTANT" in label_ids else 0,
        "in_inbox": 1 if "INBOX" in label_ids else 0,
        "in_trash": 1 if "TRASH" in label_ids else 0,
        "labels": label_ids,
        "category": category,
        "list_id": headers.get("list-id"),
        "list_unsubscribe": headers.get("list-unsubscribe"),
        "list_unsubscribe_post": headers.get("list-unsubscribe-post"),
        "size": raw.get("sizeEstimate"),
        "fetched_at": int(time.time()),
        "gone": 0,
    }


def _is_rate_limit_error(exc: Exception) -> bool:
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    if status == 429:
        return True
    if status == 403:
        try:
            reason = exc.error_details[0].get("reason", "") if exc.error_details else ""
        except Exception:
            reason = ""
        text = (reason or "") + str(exc)
        return "rateLimitExceeded" in text or "userRateLimitExceeded" in text
    return False


def _retry(fn: Callable, max_attempts: int = 8):
    """Run fn() with exponential backoff + jitter on Gmail rate-limit errors."""
    delay = 1.0
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit_error(exc) or attempt == max_attempts - 1:
                raise
            time.sleep(min(delay, 64) + random.uniform(0, 0.5))
            delay *= 2
    raise RuntimeError("unreachable")


class GmailClient:
    """Live Gmail API client. Builds the service lazily on first use."""

    def __init__(self, config):
        self.config = config
        self._service = None
        self._creds = None
        self._email: Optional[str] = None
        self._label_cache: Optional[list[dict]] = None

    # -------------------------------------------------------------- auth

    def _load_creds(self):
        from google.oauth2.credentials import Credentials

        if not self.config.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self.config.token_path), SCOPES)
        except Exception:
            return None

    def _service_from_creds(self, creds):
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _ensure_service(self):
        if self._service is not None:
            return self._service
        creds = self._creds or self._load_creds()
        if creds is None:
            raise RuntimeError("not authenticated")
        if creds.expired and creds.refresh_token:
            import google.auth.transport.requests

            creds.refresh(google.auth.transport.requests.Request())
            self.config.token_path.write_text(creds.to_json())
        self._creds = creds
        self._service = self._service_from_creds(creds)
        return self._service

    def is_authenticated(self) -> bool:
        creds = self._creds or self._load_creds()
        if creds is None:
            return False
        if creds.valid:
            self._creds = creds
            return True
        if creds.expired and creds.refresh_token:
            try:
                import google.auth.transport.requests

                creds.refresh(google.auth.transport.requests.Request())
                self.config.token_path.write_text(creds.to_json())
                self._creds = creds
                return True
            except Exception:
                return False
        return False

    def login(self) -> str:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self.config.credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)
        self.config.token_path.write_text(creds.to_json())
        self._creds = creds
        self._service = self._service_from_creds(creds)
        profile = _retry(lambda: self._service.users().getProfile(userId="me").execute())
        self._email = profile.get("emailAddress")
        return self._email

    def logout(self) -> None:
        if self.config.token_path.exists():
            self.config.token_path.unlink()
        self._service = None
        self._creds = None
        self._email = None
        self._label_cache = None

    def email(self) -> Optional[str]:
        if self._email:
            return self._email
        if not self.is_authenticated():
            return None
        service = self._ensure_service()
        profile = _retry(lambda: service.users().getProfile(userId="me").execute())
        self._email = profile.get("emailAddress")
        return self._email

    # -------------------------------------------------------------- reading

    def list_message_ids(
        self, query: str, page_token: str | None = None
    ) -> tuple[list[tuple[str, str]], Optional[str], int]:
        service = self._ensure_service()

        def _call():
            return (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=500, pageToken=page_token)
                .execute()
            )

        resp = _retry(_call)
        items = [(m["id"], m.get("threadId", "")) for m in resp.get("messages", [])]
        return items, resp.get("nextPageToken"), resp.get("resultSizeEstimate", 0)

    def get_metadata_batch(self, ids: list[str]) -> list[dict]:
        """Fetch metadata for ids in batches of 50.

        Items that come back rate-limited inside a batch are re-batched together
        after a backoff (up to 8 rounds) instead of being retried one by one;
        any other per-item error (typically a 404 for a message deleted since
        listing) is logged and skipped.
        """
        service = self._ensure_service()
        headers = ["From", "Subject", "Date", "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id"]
        results: dict[str, dict] = {}

        def make_cb(id_, errors):
            def cb(request_id, response, exception):
                if exception is not None:
                    errors[id_] = exception
                else:
                    results[id_] = response

            return cb

        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            pending = list(chunk)
            delay = 1.0
            for _attempt in range(8):
                errors: dict[str, Exception] = {}
                batch = service.new_batch_http_request()
                for mid in pending:
                    batch.add(
                        service.users().messages().get(
                            userId="me", id=mid, format="metadata", metadataHeaders=headers
                        ),
                        callback=make_cb(mid, errors),
                        request_id=mid,
                    )
                _retry(batch.execute)

                rate_limited = [m for m, e in errors.items() if _is_rate_limit_error(e)]
                for m, e in errors.items():
                    if m not in rate_limited:
                        log.warning("skipping message %s: %s", m, e)
                if not rate_limited:
                    break
                time.sleep(min(delay, 64) + random.uniform(0, 0.5))
                delay *= 2
                pending = rate_limited
            else:
                log.warning("giving up on %d rate-limited messages in this batch", len(pending))

            if start + 50 < len(ids):
                time.sleep(1.2)

        return [results[mid] for mid in ids if mid in results]

    # -------------------------------------------------------------- mutating

    def batch_modify(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> None:
        if not ids:
            return
        service = self._ensure_service()
        body = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]

            def _call(chunk=chunk):
                return service.users().messages().batchModify(
                    userId="me", body={"ids": chunk, **body}
                ).execute()

            _retry(_call)

    def trash(self, ids: list[str]) -> None:
        if not ids:
            return
        try:
            self.batch_modify(ids, add=["TRASH"])
        except Exception as exc:  # noqa: BLE001
            log.warning("batchModify TRASH rejected (%s); falling back to per-message trash", exc)
            service = self._ensure_service()
            batch = service.new_batch_http_request()
            for mid in ids:
                batch.add(service.users().messages().trash(userId="me", id=mid))
            _retry(batch.execute)

    def untrash(self, ids: list[str]) -> None:
        if not ids:
            return
        try:
            self.batch_modify(ids, remove=["TRASH"])
        except Exception as exc:  # noqa: BLE001
            log.warning("batchModify untrash rejected (%s); falling back to per-message untrash", exc)
            service = self._ensure_service()
            batch = service.new_batch_http_request()
            for mid in ids:
                batch.add(service.users().messages().untrash(userId="me", id=mid))
            _retry(batch.execute)

    # -------------------------------------------------------------- labels

    def list_labels(self) -> list[dict]:
        service = self._ensure_service()
        resp = _retry(lambda: service.users().labels().list(userId="me").execute())
        self._label_cache = resp.get("labels", [])
        return self._label_cache

    def ensure_label(self, name: str) -> str:
        if self._label_cache is None:
            self.list_labels()
        for label in self._label_cache:
            if label.get("name") == name:
                return label["id"]
        service = self._ensure_service()
        created = _retry(
            lambda: service.users()
            .labels()
            .create(
                userId="me",
                body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            )
            .execute()
        )
        self._label_cache.append(created)
        return created["id"]

    # -------------------------------------------------------------- filters

    def create_filter(self, from_email: str) -> str:
        service = self._ensure_service()
        resp = _retry(
            lambda: service.users()
            .settings()
            .filters()
            .create(
                userId="me",
                body={
                    "criteria": {"from": from_email},
                    "action": {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
                },
            )
            .execute()
        )
        return resp["id"]

    def delete_filter(self, filter_id: str) -> None:
        service = self._ensure_service()
        _retry(lambda: service.users().settings().filters().delete(userId="me", id=filter_id).execute())

    # -------------------------------------------------------------- sending

    def send_mail(self, to: str, subject: str, body: str) -> str:
        service = self._ensure_service()
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        resp = _retry(
            lambda: service.users().messages().send(userId="me", body={"raw": raw}).execute()
        )
        return resp["id"]

    def one_click_unsubscribe(self, url: str) -> bool:
        try:
            resp = httpx.post(
                url,
                data={"List-Unsubscribe": "One-Click"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "InboxCleanup/1.0 (one-click unsubscribe, RFC 8058)",
                },
                timeout=15,
                follow_redirects=True,
            )
            return 200 <= resp.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("one-click unsubscribe POST to %s failed: %s", url, exc)
            return False


class DemoGmailClient:
    """Same public interface as GmailClient; never touches the network."""

    def __init__(self):
        self.calls: list[dict] = []
        self._email = "demo.user@example.com"
        self._filter_n = 0
        self._msg_n = 0
        self._labels = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "SENT", "name": "SENT", "type": "system"},
            {"id": "TRASH", "name": "TRASH", "type": "system"},
        ]

    def _log(self, method: str, **kwargs) -> None:
        self.calls.append({"method": method, **kwargs})

    def is_authenticated(self) -> bool:
        return True

    def login(self) -> str:
        self._log("login")
        return self._email

    def logout(self) -> None:
        self._log("logout")

    def email(self) -> Optional[str]:
        return self._email

    def list_message_ids(
        self, query: str, page_token: str | None = None
    ) -> tuple[list[tuple[str, str]], Optional[str], int]:
        self._log("list_message_ids", query=query, page_token=page_token)
        return [], None, 0

    def get_metadata_batch(self, ids: list[str]) -> list[dict]:
        self._log("get_metadata_batch", ids=list(ids))
        return []

    def batch_modify(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> None:
        self._log("batch_modify", ids=list(ids), add=add, remove=remove)

    def trash(self, ids: list[str]) -> None:
        self._log("trash", ids=list(ids))

    def untrash(self, ids: list[str]) -> None:
        self._log("untrash", ids=list(ids))

    def list_labels(self) -> list[dict]:
        self._log("list_labels")
        return list(self._labels)

    def ensure_label(self, name: str) -> str:
        for label in self._labels:
            if label["name"] == name:
                self._log("ensure_label", name=name, created=False)
                return label["id"]
        label_id = f"demo-label-{len(self._labels)}"
        self._labels.append({"id": label_id, "name": name, "type": "user"})
        self._log("ensure_label", name=name, created=True)
        return label_id

    def create_filter(self, from_email: str) -> str:
        self._filter_n += 1
        filter_id = f"demo-filter-{self._filter_n}"
        self._log("create_filter", from_email=from_email, filter_id=filter_id)
        return filter_id

    def delete_filter(self, filter_id: str) -> None:
        self._log("delete_filter", filter_id=filter_id)

    def send_mail(self, to: str, subject: str, body: str) -> str:
        self._msg_n += 1
        msg_id = f"demo-msg-{self._msg_n}"
        self._log("send_mail", to=to, subject=subject, body=body, msg_id=msg_id)
        return msg_id

    def one_click_unsubscribe(self, url: str) -> bool:
        self._log("one_click_unsubscribe", url=url)
        return True
