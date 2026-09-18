# Gmail Cleanup Dashboard — Architecture & Contract

This file is the source of truth for every module. If code and this file disagree, fix the code (or update this file deliberately and tell the others).

## Purpose

Local web app with three jobs:

1. **Find subscriptions you don't open and stop them** (unsubscribe and/or block with a Gmail filter).
2. **Trash their old mail** (Gmail Trash, recoverable for 30 days; nothing is permanently deleted).
3. **Label what's left** into categories using the cheapest capable Claude model.

Rule: **report first, act only after explicit approval in the UI.** Every mutating action goes through a preview → confirm → background job → activity log, and is undoable where Gmail allows it.

## Stack

- Python 3.11, FastAPI + uvicorn, SQLite via stdlib `sqlite3`, `google-api-python-client`, `anthropic` SDK (1.x).
- Frontend: one static page (`frontend/index.html`, `frontend/app.js`, `frontend/styles.css`). Vanilla JS, no build step, no framework. Served by FastAPI at `/`.
- Runs at `http://127.0.0.1:8765` (env `PORT`).

## Repo layout

```
backend/
  config.py        # env + paths                                   (written)
  models.py        # Pydantic API models — JSON shapes live here    (written)
  store.py         # SQLite schema, queries, sender aggregation
  gmail_client.py  # OAuth + Gmail API wrapper
  scanner.py       # scan job: list ids -> batch metadata -> store; resumable
  actions.py       # unsubscribe / block / purge / keep / undo
  classifier.py    # Claude classification (Haiku first, Sonnet for low confidence)
  organizer.py     # candidate selection, cost estimate, apply labels
  demo.py          # synthetic mailbox for DEMO=1
  jobs.py          # single background job runner with progress
  api.py           # FastAPI app + routes; `python -m backend.api` starts it
frontend/          # index.html, app.js, styles.css
tests/             # pytest
docs/              # ARCHITECTURE.md (this), SETUP.md
```

## Modes

- **DEMO=1**: `demo.py` fills SQLite with synthetic data (fictional brands only). Every Gmail-mutating call is recorded in the action log with status `simulated` and nothing is sent anywhere. Classifier uses `FakeClassifier` unless `ANTHROPIC_API_KEY` is set. UI shows a DEMO banner.
- **Live**: needs `credentials.json` (OAuth desktop client) in the project root. First `POST /api/auth/login` opens the browser consent; token cached in `token.json`.

## Gmail API facts (use exactly these; do not invent others)

- Scopes: `https://www.googleapis.com/auth/gmail.modify` (read, label, trash, send) and `https://www.googleapis.com/auth/gmail.settings.basic` (filters).
- Build: `googleapiclient.discovery.build("gmail", "v1", credentials=creds, cache_discovery=False)`.
- Auth: `InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)`; persist `creds.to_json()` to `token.json`; on load, if `creds.expired and creds.refresh_token` → `creds.refresh(google.auth.transport.requests.Request())`.
- Profile: `service.users().getProfile(userId="me").execute()` → `{"emailAddress", "messagesTotal", "historyId"}`.
- List ids: `service.users().messages().list(userId="me", q=QUERY, maxResults=500, pageToken=...)` → `{"messages": [{"id", "threadId"}], "nextPageToken", "resultSizeEstimate"}`. Ids only. 5 quota units per call.
- Metadata: `service.users().messages().get(userId="me", id=ID, format="metadata", metadataHeaders=["From","Subject","Date","List-Unsubscribe","List-Unsubscribe-Post","List-Id"])` → `{"id","threadId","labelIds":[...],"snippet","internalDate":"<ms epoch as str>","sizeEstimate","payload":{"headers":[{"name","value"}]}}`. 5 units each. Use `service.new_batch_http_request(callback=cb)`; add ≤ 50 gets per batch; `.execute()`; the callback gets `(request_id, response, exception)` per item.
- Quota is about 250 units per user per second. On `googleapiclient.errors.HttpError` with status 429, or 403 whose reason is `rateLimitExceeded`/`userRateLimitExceeded`: exponential backoff with jitter (1, 2, 4 … max 64 s), up to 8 attempts. Also throttle proactively: sleep ~1.2 s between 50-message batches.
- Category from `labelIds`: `CATEGORY_PROMOTIONS`→promotions, `CATEGORY_UPDATES`→updates, `CATEGORY_SOCIAL`→social, `CATEGORY_FORUMS`→forums, `CATEGORY_PERSONAL`→primary, none→unknown. `UNREAD` present = not opened. Also read `INBOX`, `STARRED`, `IMPORTANT`, `TRASH`.
- Batch modify: `service.users().messages().batchModify(userId="me", body={"ids": [≤1000], "addLabelIds": [...], "removeLabelIds": [...]}).execute()`. Trash = add `TRASH`; untrash = remove `TRASH`; archive = remove `INBOX`; mark read = remove `UNREAD`. 50 units per call. If adding `TRASH` via batchModify is rejected, fall back to per-message `messages().trash(userId="me", id=ID)` inside a batch http request.
- Labels: `service.users().labels().list(userId="me")` → `{"labels": [{"id","name","type"}]}`. Create: `labels().create(userId="me", body={"name": NAME, "labelListVisibility": "labelShow", "messageListVisibility": "show"})`. Nested labels use `/` in the name.
- Filters ("Block"): `service.users().settings().filters().create(userId="me", body={"criteria": {"from": EMAIL}, "action": {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}}).execute()` → `{"id": ...}`. Store the id; undo = `filters().delete(userId="me", id=ID)`.
- Send (mailto unsubscribe): build `email.message.EmailMessage`, set To/Subject and text body, `raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()`, `service.users().messages().send(userId="me", body={"raw": raw}).execute()`.
- Scan query: `-in:chats -in:sent -in:draft -in:spam -in:trash` plus ` after:YYYY/MM/DD` when `window_months > 0`.
- Read-state refresh sweep (each scan): same query plus ` is:read` → ids listed are `is_unread=0`; every other DB message inside the window becomes `is_unread=1`. Ids in DB but absent from the full id list → `gone=1`.

## Unsubscribe mechanics (RFC 2369 / RFC 8058)

`List-Unsubscribe: <https://…>, <mailto:…>` is comma-separated angle-bracketed URIs.

| kind | condition | action |
|---|---|---|
| `one_click` | `List-Unsubscribe-Post: List-Unsubscribe=One-Click` present AND an https URL exists | `POST url` body `List-Unsubscribe=One-Click`, `Content-Type: application/x-www-form-urlencoded`, timeout 15 s. 2xx = done. |
| `mailto` | a `mailto:` URI exists (and no one-click) | send an email via the Gmail API to that address; subject/body from the mailto query string, defaults `unsubscribe`. |
| `http` | only an http(s) link, no one-click | cannot automate safely. Log status `manual`, return the URL; UI shows "Open link". |
| `none` | no header | offer Block. |

"Stop" in the UI = unsubscribe (when possible) **plus** Block. Both toggles default on; user can turn either off. Block is the reliable stop; unsubscribe is polite.

Helpers in `actions.py`: `parse_list_unsubscribe(header: str) -> list[str]`, `unsubscribe_kind(list_unsub: str|None, list_unsub_post: str|None) -> Literal["one_click","mailto","http","none"]`, `parse_mailto(uri) -> (address, subject, body)`.

## Sender normalization

`normalize_sender(from_header) -> (email, display_name)` in `store.py`: `email.utils.parseaddr`; lowercase; strip `+tag` from the local part; `domain` = part after `@`. Senders are grouped by normalized email.

## SQLite schema (`store.py`)

```sql
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, thread_id TEXT, sender TEXT NOT NULL, sender_name TEXT, sender_raw TEXT,
  domain TEXT, subject TEXT, snippet TEXT, internal_ts INTEGER NOT NULL,        -- unix seconds
  is_unread INTEGER NOT NULL DEFAULT 1, is_starred INTEGER DEFAULT 0, is_important INTEGER DEFAULT 0,
  in_inbox INTEGER DEFAULT 1, in_trash INTEGER DEFAULT 0, labels TEXT,           -- json array of labelIds
  category TEXT, list_id TEXT, list_unsubscribe TEXT, list_unsubscribe_post TEXT,
  size INTEGER, fetched_at INTEGER, gone INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(internal_ts);
CREATE TABLE IF NOT EXISTS sender_status (sender TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, action TEXT, sender TEXT, detail TEXT,  -- json
  status TEXT, message_ids TEXT,                                                              -- json array
  undoable INTEGER DEFAULT 0, undone INTEGER DEFAULT 0, job_id TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
  message_id TEXT PRIMARY KEY, category TEXT, confidence REAL, model TEXT, reason TEXT,
  applied INTEGER DEFAULT 0, overridden INTEGER DEFAULT 0, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS categories (key TEXT PRIMARY KEY, label_name TEXT, description TEXT, sort_order INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);   -- json
CREATE TABLE IF NOT EXISTS scan_state (key TEXT PRIMARY KEY, value TEXT);
```

`Store(path)` owns all SQL. Use `check_same_thread=False` and a `threading.Lock` around writes (jobs run in a thread). WAL journal mode.

### Sender stats derivation (one GROUP BY over `gone=0 AND in_trash=0`)

- `total` = count; `opened` = count where `is_unread=0`; `open_rate` = opened/total
- `last_opened` = max ts where `is_unread=0` (null if none); `first_received`/`last_received` = min/max ts
- `per_month` = total / max(1, months between first_received and now)
- `has_unsubscribe` = any message has `list_unsubscribe`; `unsubscribe_kind` from the most recent such message
- `category` = most common category across the sender's messages
- `is_subscription` = `has_unsubscribe OR category == "promotions"`
- `status` = sender_status row or `"active"`
- `sample_subjects` = subjects of the 3 most recent messages

### Filter semantics (`SenderFilter`)

- `min_total`: total ≥ n
- `max_open_rate`: open_rate ≤ x (null = ignore)
- `last_opened_before_days`: include if `last_opened` is null (only when `include_never_opened`) OR `days_since_last_opened ≥ n` (null = ignore)
- `has_unsubscribe`: null = any
- `categories`: empty = all
- `subscription_only`: true → `is_subscription` only
- `search`: case-insensitive substring over sender, display_name, domain
- `status`: statuses to include (default `["active"]`)
- `sort`/`order`/`limit`/`offset`

UI defaults (tuned for a large mailbox): `min_total=3, max_open_rate=0.10, last_opened_before_days=90, subscription_only=true`. API defaults are neutral (see `models.py`).

## Jobs (`jobs.py`)

One background job at a time (`threading.Thread`). `JobRunner.start(kind, fn) -> job_id` (409 if busy), `JobRunner.current() -> JobState|None`, `cancel()` sets a flag the job checks between batches. `fn(job)` receives a handle with `job.progress(done, total, message)` and `job.cancelled`. Kinds: `scan`, `execute_actions`, `classify`, `apply_labels`. The last finished job stays visible in `/api/status` until the next one starts.

## HTTP API

All JSON. Errors → `{"error": "message"}` with a 4xx/5xx status. Timestamps are ISO-8601 UTC strings. Pydantic models in `backend/models.py` define every body/response.

| Method & path | Body / query | Response |
|---|---|---|
| `GET /api/status` | – | `StatusResponse` |
| `POST /api/auth/login` | – | `{"ok": true, "email": "..."}` (live only; runs the OAuth flow, may block ~1 min) |
| `POST /api/auth/logout` | – | `{"ok": true}` (deletes token.json) |
| `POST /api/scan` | `ScanRequest` | `{"ok": true, "job_id": "..."}`; 409 if a job is running |
| `POST /api/jobs/cancel` | – | `{"ok": true}` |
| `GET /api/overview` | – | `OverviewResponse` |
| `GET /api/senders` | `SenderFilter` as query params; list fields comma-separated | `SenderListResponse` |
| `GET /api/senders/{sender}/messages` | `?limit=50` | `{"items": [MessageSummary]}` |
| `POST /api/actions/preview` | `ActionRequest` | `ActionPreview` |
| `POST /api/actions/execute` | `ActionRequest` | `{"ok": true, "job_id": "..."}` |
| `GET /api/actions/log` | `?limit=100&offset=0` | `{"total": n, "items": [ActionLogEntry]}` |
| `POST /api/actions/undo/{id}` | – | `{"ok": true}`; 400 if not undoable |
| `GET /api/organize/categories` | – | `{"items": [CategoryDef]}` |
| `PUT /api/organize/categories` | `{"items": [CategoryDef]}` | same |
| `GET /api/organize/estimate` | `?scope=inbox|all&days=365` | `OrganizeEstimate` |
| `POST /api/organize/classify` | `ClassifyRequest` | `{"ok": true, "job_id": "..."}` |
| `GET /api/organize/results` | `?category=&min_confidence=&limit=100&offset=0` | `OrganizeResults` |
| `PUT /api/organize/results/{message_id}` | `{"category": "travel"}` | `{"ok": true}` (sets `overridden=1`, `confidence=1.0`) |
| `POST /api/organize/apply` | `ApplyRequest` | `{"ok": true, "job_id": "..."}` |
| `GET /api/labels` | – | `{"items": [{"id","name","type"}]}` |
| `GET /api/settings` | – | `Settings` |
| `PUT /api/settings` | `Settings` (partial ok) | `Settings` |
| `GET /` | – | `frontend/index.html`; `/static/*` serves the frontend folder |

### Action semantics (`ActionRequest.action`)

- `stop`: for each sender, unsubscribe (if `unsubscribe` and kind ≠ none) then block (if `block`). Sender status → `blocked` if blocked else `unsubscribed`. One `action_log` row per step (`unsubscribe`, `block`).
- `purge`: trash the sender's messages with `internal_ts < now - purge_older_than_days` (`0`/null = all). Always skip starred; skip important when `skip_important`. Marks `in_trash=1` locally. One `action_log` row per sender with `message_ids`, `undoable=1`.
- `keep`: sender status → `kept`. Undoable (status back to active).
- `reactivate`: status → `active`. Not undoable.

Undo: `purge` → batchModify remove `TRASH`, set `in_trash=0`; `block` → delete filter id from `detail`; `keep` → status active; `unsubscribe` → 400 "cannot undo an unsubscribe".

### Organize semantics

Candidates = messages with `gone=0, in_trash=0`, `is_subscription` sender false (computed per sender), `in_inbox=1` when `scope=inbox`, within `days`, and no row in `classifications` (unless `ClassifyRequest.reclassify`). Cost estimate uses `Classifier.estimate_cost_usd`. Apply = create missing labels, `batchModify` add label id per category (≤1000 ids per call), optional remove `INBOX` (`archive`) and remove `UNREAD` (`mark_read`); category `skip` gets nothing. Sets `applied=1`.

Default categories (editable in UI; `label_name` is the Gmail label that gets created):

| key | label_name | description |
|---|---|---|
| finance | Finance | bank and card statements, transaction alerts, investments, taxes, insurance |
| receipts | Receipts & Orders | purchase confirmations, shipping, returns, invoices |
| travel | Travel | flights, hotels, rentals, itineraries, loyalty programs |
| health | Health | doctors, appointments, pharmacy, insurance claims, fitness |
| home | Home | utilities, mortgage or rent, home services, HOA, real estate |
| work | Work & Career | job applications, recruiters, professional correspondence |
| personal | Personal | real people writing to you: friends, family, acquaintances |
| government | Government & Legal | taxes, DMV, tolls, legal notices, voting |
| accounts | Accounts & Security | password resets, sign-in alerts, account notices, subscriptions billing |
| skip | *(no label)* | marketing or newsletters that slipped through, or nothing worth labeling |

## Classifier contract (`classifier.py`)

```python
@dataclass
class EmailForClassification:
    message_id: str; sender: str; display_name: str; subject: str; snippet: str; date: str

@dataclass
class ClassificationResult:
    message_id: str; category: str; confidence: float; reason: str; model: str

class Classifier:
    def __init__(self, client: anthropic.Anthropic, categories: list[CategoryDef],
                 cheap_model: str, strong_model: str, escalate_below: float): ...
    def classify(self, emails, on_progress=None, should_cancel=None) -> list[ClassificationResult]
    def estimate_cost_usd(self, n_emails: int) -> float

class FakeClassifier:  # same interface; deterministic keyword rules; used in DEMO and tests
```

- Batches of 20 emails per request. Cheap model (`claude-haiku-4-5`) first. Results with `confidence < escalate_below` are re-run, batched, on the strong model (`claude-sonnet-5`, `output_config={"effort": "low"}`); the strong result wins.
- Request shape (documented SDK usage, do not deviate): `client.messages.parse(model=..., max_tokens=4096, system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}], messages=[{"role": "user", "content": <json array of {id, from, name, subject, snippet, date}>}], output_format=BatchOutput)` where `BatchOutput(BaseModel): results: list[Item]` and `Item(BaseModel): id: str; category: str; confidence: float; reason: str`. Read `response.parsed_output`. Do not pass `thinking` for Haiku 4.5.
- Validate: category not in keys → `skip`, confidence 0. Missing ids → `skip`, confidence 0. `response.stop_reason == "refusal"` → mark batch `skip` with reason "refused". `stop_reason == "max_tokens"` → retry that batch split in half.
- Errors: `anthropic.RateLimitError` → sleep `retry-after` header (default 20 s) and retry, up to 5×; `anthropic.APIStatusError` with `status_code >= 500` → backoff retry 3×; `anthropic.APIConnectionError` → backoff retry 3×; anything else → raise.
- Cost estimate: per email ≈ 130 input tokens + 700 system tokens amortised per batch of 20 (35/email) + 30 output tokens; price table `{"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-5": (2.00, 10.00)}` USD per million (input, output); add 15% of emails escalated at the strong price. Round up to cents.

## Frontend spec (`frontend/`)

Tabs: **Overview · Subscriptions · Organize · Activity · Settings**. Poll `GET /api/status` every 2 s while a job runs, else every 10 s.

- **Overview**: stat tiles (Messages scanned, Unread share, Senders, Subscription senders, Never-opened senders, Messages trashed so far); scan controls (window select 3/6/12/24 months/all, "Scan" button, progress bar with message, cancel); "Top senders by volume" as a horizontal bar list (single hue, direct-labelled counts); "By category" bar list.
- **Subscriptions**: filter row (min emails, max open %, last opened before N days with "include never opened", has-unsubscribe select, category select, status select, search) with "Reset to defaults"; table with select-all checkbox, sender (display name over email), count, open %, last received, last opened, per month, unsubscribe badge, category, status; row click expands recent subjects. Sticky bulk-action bar when rows are selected: **Stop** (toggles: Unsubscribe, Block), **Trash older than [30] days** (toggle: skip Important), **Keep**. Every action: `preview` → modal listing per-sender effect → Confirm → `execute` → progress → toast → Activity refresh.
- **Organize**: category editor (key, label name, description; add/remove/reorder); scope (inbox/all) + days; estimate card (candidates, cost, model) → **Classify** → progress; results table filterable by category with confidence bar and inline category override; **Apply labels** with archive/mark-read toggles → confirm modal → job.
- **Activity**: log table (time, action, sender, detail, count, status) with Undo where `undoable && !undone`.
- **Settings**: window default, skip-important default, model names (read-only from env), mode, auth (Sign in / Sign out in live mode).
- Empty states: no scan yet → prompt to scan; demo banner when `mode == "demo"`.

Design tokens (CSS custom properties on `:root`; dark values under `@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme="light"])` and again under `:root[data-theme="dark"]`):

| token | light | dark |
|---|---|---|
| `--surface-1` | `#fcfcfb` | `#1a1a19` |
| `--page` | `#f9f9f7` | `#0d0d0d` |
| `--text-primary` | `#0b0b0b` | `#ffffff` |
| `--text-secondary` | `#52514e` | `#c3c2b7` |
| `--text-muted` | `#898781` | `#898781` |
| `--hairline` | `#e1e0d9` | `#2c2c2a` |
| `--accent` (single hue for bars/links/primary buttons) | `#2a78d6` | `#3987e5` |
| `--status-good` | `#0ca30c` | `#0ca30c` |
| `--status-warning` | `#fab219` | `#fab219` |
| `--status-critical` | `#d03b3b` | `#d03b3b` |

Font: `system-ui, -apple-system, "Segoe UI", sans-serif`; `font-variant-numeric: tabular-nums` on table numbers. Layout works at 375px width with 16px side gutters. Status colours always ship with an icon or text label, never colour alone.
