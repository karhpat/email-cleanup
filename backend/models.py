"""Pydantic models shared by the API, the store, and the frontend contract.

These are the JSON shapes. See docs/ARCHITECTURE.md for semantics.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["promotions", "updates", "social", "forums", "primary", "unknown"]
UnsubscribeKind = Literal["one_click", "mailto", "http", "none"]
SenderStatus = Literal["active", "unsubscribed", "blocked", "kept"]
JobKind = Literal["scan", "execute_actions", "classify", "apply_labels"]
JobStateName = Literal["running", "done", "error", "cancelled"]
ActionName = Literal["stop", "purge", "keep", "reactivate"]


# ---------- status / jobs ----------

class JobProgress(BaseModel):
    done: int = 0
    total: int = 0
    message: str = ""


class JobState(BaseModel):
    id: str
    kind: JobKind
    state: JobStateName
    progress: JobProgress = Field(default_factory=JobProgress)
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[dict] = None


class Settings(BaseModel):
    window_months: int = 12
    skip_important: bool = True
    purge_older_than_days: int = 30
    default_filters: dict = Field(default_factory=lambda: {
        "min_total": 3,
        "max_open_rate": 0.10,
        "last_opened_before_days": 90,
        "include_never_opened": True,
        "subscription_only": True,
    })
    classify_model: str = "claude-haiku-4-5"
    escalate_model: str = "claude-sonnet-5"
    escalate_below: float = 0.6


class LastScan(BaseModel):
    finished_at: Optional[str] = None
    window_months: Optional[int] = None
    messages_seen: Optional[int] = None


class StatusResponse(BaseModel):
    mode: Literal["demo", "live"]
    authenticated: bool
    email: Optional[str] = None
    job: Optional[JobState] = None
    counts: dict = Field(default_factory=lambda: {"messages": 0, "senders": 0})
    last_scan: LastScan = Field(default_factory=LastScan)
    settings: Settings = Field(default_factory=Settings)
    anthropic_configured: bool = False


class ScanRequest(BaseModel):
    window_months: int = 12   # 0 = all mail
    full: bool = False        # True = refetch metadata even for known ids


# ---------- senders ----------

class SenderStats(BaseModel):
    sender: str
    display_name: str = ""
    domain: str = ""
    total: int
    opened: int
    open_rate: float
    first_received: str
    last_received: str
    last_opened: Optional[str] = None
    days_since_last_received: int
    days_since_last_opened: Optional[int] = None
    per_month: float
    has_unsubscribe: bool
    unsubscribe_kind: UnsubscribeKind = "none"
    list_id: Optional[str] = None
    category: Category = "unknown"
    inbox_count: int = 0
    starred: int = 0
    important: int = 0
    is_subscription: bool
    status: SenderStatus = "active"
    sample_subjects: list[str] = Field(default_factory=list)


class SenderFilter(BaseModel):
    min_total: int = 1
    max_open_rate: Optional[float] = None
    last_opened_before_days: Optional[int] = None
    include_never_opened: bool = True
    has_unsubscribe: Optional[bool] = None
    categories: list[Category] = Field(default_factory=list)
    subscription_only: bool = False
    search: str = ""
    status: list[SenderStatus] = Field(default_factory=lambda: ["active"])
    sort: Literal["total", "open_rate", "last_received", "last_opened", "per_month", "sender"] = "total"
    order: Literal["asc", "desc"] = "desc"
    limit: int = 200
    offset: int = 0


class SenderListResponse(BaseModel):
    total: int
    items: list[SenderStats]


class MessageSummary(BaseModel):
    id: str
    thread_id: Optional[str] = None
    subject: str = ""
    date: str
    is_unread: bool
    is_starred: bool = False
    is_important: bool = False
    category: Category = "unknown"
    in_inbox: bool = True
    in_trash: bool = False


class TopSender(BaseModel):
    sender: str
    display_name: str = ""
    total: int
    open_rate: float


class OverviewResponse(BaseModel):
    messages_total: int
    unread_total: int
    senders_total: int
    subscription_senders: int
    subscription_messages: int
    never_opened_senders: int
    by_category: dict[str, int]
    top_senders: list[TopSender]
    actions: dict[str, int] = Field(default_factory=lambda: {
        "unsubscribed": 0, "blocked": 0, "trashed_messages": 0, "labeled_messages": 0,
    })


# ---------- actions ----------

class ActionRequest(BaseModel):
    action: ActionName
    senders: list[str]
    unsubscribe: bool = True
    block: bool = True
    purge_older_than_days: Optional[int] = 30   # 0/None = everything
    skip_important: bool = True


class ActionPreviewItem(BaseModel):
    sender: str
    display_name: str = ""
    unsubscribe_method: UnsubscribeKind = "none"
    will_unsubscribe: bool = False
    will_block: bool = False
    purge_count: int = 0
    skipped_starred: int = 0
    skipped_important: int = 0


class ActionPreview(BaseModel):
    action: ActionName
    items: list[ActionPreviewItem]
    totals: dict[str, int]


class ActionLogEntry(BaseModel):
    id: int
    ts: str
    action: str                  # unsubscribe | block | purge | keep | reactivate | apply_labels
    sender: Optional[str] = None
    detail: dict = Field(default_factory=dict)
    status: str                  # done | simulated | failed | manual
    message_count: int = 0
    undoable: bool = False
    undone: bool = False


# ---------- organize ----------

class CategoryDef(BaseModel):
    key: str
    label_name: Optional[str] = None   # None = no label applied (e.g. "skip")
    description: str = ""


class OrganizeEstimate(BaseModel):
    candidates: int
    already_classified: int
    est_cost_usd: float
    model: str
    escalate_model: str


class ClassifyRequest(BaseModel):
    scope: Literal["inbox", "all"] = "inbox"
    days: int = 365
    limit: Optional[int] = None
    reclassify: bool = False


class OrganizeResultItem(BaseModel):
    message_id: str
    sender: str
    display_name: str = ""
    subject: str = ""
    date: str
    category: str
    confidence: float
    model: str
    reason: str = ""
    applied: bool = False
    overridden: bool = False


class OrganizeResults(BaseModel):
    total: int
    summary: dict[str, int]
    items: list[OrganizeResultItem]


class ApplyRequest(BaseModel):
    categories: Optional[list[str]] = None   # None = all except skip
    archive: bool = False
    mark_read: bool = False
