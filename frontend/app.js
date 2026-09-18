"use strict";

/* ==========================================================================
   Inbox Cleanup — frontend application (vanilla JS, no build step)
   Organized as: constants -> storage -> utils -> toast -> api -> modal ->
   state -> header/status polling -> tabs -> one render module per tab -> init
   ========================================================================== */

/* ---------------------------------------------------------------------- */
/* Constants                                                               */
/* ---------------------------------------------------------------------- */

const CATEGORIES = ["promotions", "updates", "social", "forums", "primary", "unknown"];
const CATEGORY_LABELS = {
  promotions: "Promotions", updates: "Updates", social: "Social",
  forums: "Forums", primary: "Primary", unknown: "Unknown",
};
const SENDER_STATUSES = ["active", "unsubscribed", "blocked", "kept"];

const SUBS_DEFAULT_FILTERS = {
  min_total: 3,
  max_open_rate_pct: 10, // UI keeps this as a 0-100 percent; converted to 0-1 for the API
  last_opened_before_days: 90,
  include_never_opened: true,
  has_unsubscribe: "any", // any | yes | no
  category: "all",
  status: "active", // active | unsubscribed | blocked | kept | all
  subscription_only: true,
  search: "",
  sort: "total",
  order: "desc",
};

const JOB_KIND_LABELS = {
  scan: "Scan", execute_actions: "Action", classify: "Classification", apply_labels: "Apply labels",
};

const SENDERS_PAGE_LIMIT = 100;
const RESULTS_PAGE_LIMIT = 100;

/* ---------------------------------------------------------------------- */
/* localStorage helpers (wrapped, per spec)                                */
/* ---------------------------------------------------------------------- */

function storageGet(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw == null) return fallback;
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}

function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    /* ignore (private mode, quota, disabled storage) */
  }
}

/* ---------------------------------------------------------------------- */
/* Generic utils                                                          */
/* ---------------------------------------------------------------------- */

function qs(sel, root) { return (root || document).querySelector(sel); }
function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

function esc(value) {
  const s = value == null ? "" : String(value);
  return s.replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtInt(n) {
  if (n == null || Number.isNaN(n)) return "0";
  return Math.round(n).toLocaleString("en-US");
}

function fmtPct(x, digits) {
  if (x == null || Number.isNaN(x)) return "—";
  return (x * 100).toFixed(digits == null ? 0 : digits) + "%";
}

function fmtMoney(x) {
  if (x == null || Number.isNaN(x)) return "$0.00";
  return "$" + x.toFixed(2);
}

function fmtPerMonth(x) {
  if (x == null || Number.isNaN(x)) return "0";
  return x.toFixed(1);
}

function parseDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function relTime(value) {
  const d = parseDate(value);
  if (!d) return "never";
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.round(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const mins = Math.round(diffSec / 60);
  if (mins < 60) return mins + " min ago";
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + " h ago";
  const days = Math.round(hours / 24);
  if (days < 30) return days + " d ago";
  const months = Math.round(days / 30);
  if (months < 24) return months + " mo ago";
  const years = Math.round(months / 12);
  return years + " y ago";
}

function fmtDateTime(value) {
  const d = parseDate(value);
  if (!d) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

function debounce(fn, ms) {
  let t = null;
  return function debounced(...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

function titleCaseKey(key) {
  return String(key).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ---------------------------------------------------------------------- */
/* Toasts                                                                  */
/* ---------------------------------------------------------------------- */

function toast(message, kind) {
  const region = qs("#toasts");
  if (!region) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " toast-error" : kind === "success" ? " toast-success" : "");
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  region.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity 0.3s ease";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 320);
  }, kind === "error" ? 6000 : 4000);
}

/* ---------------------------------------------------------------------- */
/* API helper                                                              */
/* ---------------------------------------------------------------------- */

function buildQuery(params) {
  const parts = [];
  Object.keys(params).forEach((k) => {
    const v = params[k];
    if (v === undefined || v === null || v === "") return;
    parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
  });
  return parts.length ? "?" + parts.join("&") : "";
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (networkErr) {
    toast("Network error: could not reach the server.", "error");
    throw networkErr;
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (parseErr) { data = null; }
  }
  if (!res.ok) {
    const message = (data && typeof data.error === "string") ? data.error : `Request failed (${res.status})`;
    toast(message, "error");
    const err = new Error(message);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function apiGet(path, params) {
  return api("GET", path + (params ? buildQuery(params) : ""));
}

/* ---------------------------------------------------------------------- */
/* Modal (focus trap + Escape)                                             */
/* ---------------------------------------------------------------------- */

const modalState = { openEl: null, lastFocus: null, onKeydown: null };

function openModal({ title, bodyHtml, footerButtons }) {
  closeModal();
  const root = qs("#modalRoot");
  modalState.lastFocus = document.activeElement;

  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.setAttribute("data-modal-backdrop", "1");

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-labelledby", "modalTitle");

  const header = document.createElement("div");
  header.className = "modal-header";
  header.innerHTML = `<h3 id="modalTitle">${esc(title)}</h3>
    <button type="button" class="modal-close" data-action="modal-close" aria-label="Close dialog">&times;</button>`;

  const body = document.createElement("div");
  body.className = "modal-body";
  body.innerHTML = bodyHtml;

  const footer = document.createElement("div");
  footer.className = "modal-footer";
  (footerButtons || []).forEach((btn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn " + (btn.className || "btn-secondary");
    b.textContent = btn.label;
    b.addEventListener("click", btn.onClick);
    footer.appendChild(b);
  });

  modal.appendChild(header);
  modal.appendChild(body);
  modal.appendChild(footer);
  backdrop.appendChild(modal);
  root.appendChild(backdrop);
  modalState.openEl = backdrop;

  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) closeModal();
  });

  modalState.onKeydown = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeModal();
      return;
    }
    if (e.key === "Tab") {
      const focusables = qsa(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        modal
      ).filter((el) => !el.disabled && el.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  document.addEventListener("keydown", modalState.onKeydown, true);

  const firstFocusable = qs('button, [href], input, select, textarea', modal);
  (firstFocusable || modal).focus({ preventScroll: true });

  return modal;
}

function closeModal() {
  if (!modalState.openEl) return;
  modalState.openEl.remove();
  modalState.openEl = null;
  if (modalState.onKeydown) {
    document.removeEventListener("keydown", modalState.onKeydown, true);
    modalState.onKeydown = null;
  }
  if (modalState.lastFocus && modalState.lastFocus.focus) {
    try { modalState.lastFocus.focus(); } catch (e) { /* ignore */ }
  }
  modalState.lastFocus = null;
}

/* ---------------------------------------------------------------------- */
/* Application state                                                       */
/* ---------------------------------------------------------------------- */

const state = {
  activeTab: storageGet("inboxCleanup.activeTab", "overview"),
  status: null,
  lastJobKey: null,
  statusObserved: false, // `${id}:${state}` of the last-seen job, to detect transitions
  pollTimer: null,

  overview: null,
  overviewLoading: false,

  scanWindow: "12",

  senders: {
    filters: storageGet("inboxCleanup.subscriptionFilters", SUBS_DEFAULT_FILTERS),
    items: [],
    total: 0,
    offset: 0,
    loading: false,
    selected: new Set(),
    expanded: new Set(),
    built: false,
  },

  organize: {
    categories: [],
    scope: "inbox",
    days: 365,
    estimate: null,
    results: null,
    resultsOffset: 0,
    filterCategory: null,
    built: false,
  },

  activity: { items: [], total: 0 },

  settings: null,
};

/* ---------------------------------------------------------------------- */
/* Status polling + header render                                         */
/* ---------------------------------------------------------------------- */

async function pollStatusTick() {
  let data = null;
  try {
    data = await api("GET", "/api/status");
  } catch (e) {
    // api() already toasted; keep polling at the slow interval
    schedulePoll(10000);
    return;
  }
  const prevKey = state.lastJobKey;
  state.status = data;
  renderHeader();

  const job = data.job;
  const curKey = job ? `${job.id}:${job.state}` : null;
  // A job counts as finished when it was running last time we looked, or
  // when it is a job we have never seen and is already over (fast jobs can
  // complete before the first poll). The first tick after page load only
  // records what is there, so a stale finished job never toasts.
  if (state.statusObserved && job && curKey !== prevKey && job.state !== "running") {
    const prevId = prevKey ? prevKey.split(":")[0] : null;
    const prevWasRunning = !!prevKey && prevKey.endsWith(":running");
    if (prevWasRunning || job.id !== prevId) {
      onJobFinished(job);
    }
  }
  state.statusObserved = true;
  state.lastJobKey = curKey;

  schedulePoll(job && job.state === "running" ? 2000 : 10000);
}

function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollStatusTick, ms);
}

function pollStatusSoon() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollStatusTick, 150);
}

function onJobFinished(job) {
  const kindLabel = JOB_KIND_LABELS[job.kind] || job.kind;
  if (job.state === "done") {
    const msg = job.progress && job.progress.message ? job.progress.message : "completed";
    toast(`${kindLabel} finished: ${msg}`, "success");
  } else if (job.state === "error") {
    toast(`${kindLabel} failed: ${job.error || "unknown error"}`, "error");
  } else if (job.state === "cancelled") {
    toast(`${kindLabel} cancelled.`, "info");
  }
  refreshActiveTab();
}

function renderHeader() {
  const s = state.status;
  const modeBadge = qs("#modeBadge");
  const accountEmail = qs("#accountEmail");
  const jobPill = qs("#jobPill");
  const jobPillText = qs("#jobPillText");
  const jobPillBar = qs("#jobPillBar");

  if (!s) return;

  modeBadge.classList.toggle("hidden", s.mode !== "demo");
  accountEmail.textContent = s.email || (s.mode === "demo" ? "Demo mailbox" : "Not signed in");

  const job = s.job;
  if (job && job.state === "running") {
    jobPill.classList.remove("hidden", "job-error");
    const pct = job.progress && job.progress.total > 0
      ? Math.min(100, Math.round((job.progress.done / job.progress.total) * 100))
      : 0;
    const label = JOB_KIND_LABELS[job.kind] || job.kind;
    const msg = job.progress && job.progress.message ? job.progress.message : "";
    jobPillText.textContent = `${label}… ${msg}`.trim();
    jobPillBar.style.width = pct + "%";
  } else if (job && job.state === "error") {
    jobPill.classList.remove("hidden");
    jobPill.classList.add("job-error");
    jobPillText.textContent = `${JOB_KIND_LABELS[job.kind] || job.kind} failed`;
    jobPillBar.style.width = "100%";
  } else {
    jobPill.classList.add("hidden");
  }

  // Keep the Overview scan controls (Scan vs Cancel) in sync even when Overview isn't visible yet.
  syncScanControls();
}

/* ---------------------------------------------------------------------- */
/* Tabs                                                                    */
/* ---------------------------------------------------------------------- */

const TAB_LOADERS = {
  overview: loadOverview,
  subscriptions: loadSubscriptions,
  organize: loadOrganize,
  activity: loadActivity,
  settings: loadSettings,
};

function activateTab(tab, opts) {
  const skipHistory = opts && opts.skipHistory;
  state.activeTab = tab;
  if (!skipHistory) storageSet("inboxCleanup.activeTab", tab);

  qsa(".tab").forEach((btn) => {
    const active = btn.dataset.tab === tab;
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  qsa(".panel").forEach((panel) => {
    panel.hidden = panel.id !== `panel-${tab}`;
  });

  const loader = TAB_LOADERS[tab];
  if (loader) loader();
}

function refreshActiveTab() {
  const loader = TAB_LOADERS[state.activeTab];
  if (loader) loader();
}

/* ---------------------------------------------------------------------- */
/* Shared bits: unsubscribe badge, status badge, meters                    */
/* ---------------------------------------------------------------------- */

function unsubBadgeHtml(kind) {
  const map = {
    one_click: "One-click", mailto: "Mailto", http: "Link", none: "None",
  };
  const cls = kind === "none" ? "pill pill-neutral" : "pill";
  return `<span class="${cls}">${esc(map[kind] || kind)}</span>`;
}

function senderStatusBadgeHtml(status) {
  const map = {
    active: { cls: "pill-neutral", icon: "•", text: "Active" },
    unsubscribed: { cls: "pill-warning", icon: "⏸", text: "Unsubscribed" },
    blocked: { cls: "pill-critical", icon: "⛔", text: "Blocked" },
    kept: { cls: "pill-good", icon: "✓", text: "Kept" },
  };
  const m = map[status] || { cls: "pill-neutral", icon: "", text: status };
  return `<span class="pill ${m.cls}">${m.icon ? esc(m.icon) + " " : ""}${esc(m.text)}</span>`;
}

function logStatusBadgeHtml(status) {
  const map = {
    done: { cls: "pill-good", icon: "✓", text: "Done" },
    simulated: { cls: "pill-neutral", icon: "◐", text: "Simulated" },
    failed: { cls: "pill-critical", icon: "✕", text: "Failed" },
    manual: { cls: "pill-warning", icon: "✋", text: "Manual" },
  };
  const m = map[status] || { cls: "pill-neutral", icon: "", text: status };
  return `<span class="pill ${m.cls}">${m.icon ? esc(m.icon) + " " : ""}${esc(m.text)}</span>`;
}

function openMeterHtml(opened, total, openRate) {
  const rate = total > 0 ? (openRate != null ? openRate : opened / total) : 0;
  const pct = Math.round(rate * 100);
  return `<span class="meter-inline">
    <span class="meter-track"><span class="meter-fill" style="width:${pct}%"></span></span>
    <span>${pct}%</span>
  </span>`;
}

function confidenceMeterHtml(confidence) {
  const pct = Math.round((confidence || 0) * 100);
  const low = confidence < 0.6;
  return `<span class="meter-inline">
    <span class="meter-track${low ? " warning-track" : ""}"><span class="meter-fill${low ? " warning" : ""}" style="width:${pct}%"></span></span>
    <span>${pct}%</span>
  </span>`;
}

/* ======================================================================
   OVERVIEW TAB
   ====================================================================== */

function overviewShell() {
  return `
    <div class="card overview-scan-bar">
      <h2 class="section-title">Scan your mailbox</h2>
      <div class="filters" style="align-items:end;">
        <div class="filter-field">
          <label for="scanWindow">Scan window</label>
          <select id="scanWindow">
            <option value="3">Last 3 months</option>
            <option value="6">Last 6 months</option>
            <option value="12" selected>Last 12 months</option>
            <option value="24">Last 24 months</option>
            <option value="0">All mail</option>
          </select>
        </div>
        <button type="button" class="btn btn-primary" id="scanStartBtn" data-action="scan-start">Scan</button>
        <button type="button" class="btn btn-danger hidden" id="scanCancelBtn" data-action="scan-cancel">Cancel</button>
      </div>
      <p class="job-progress-line hidden" id="scanProgressLine" aria-live="polite"></p>
    </div>
    <div id="overviewBody"></div>
  `;
}

function syncScanControls() {
  const btnStart = qs("#scanStartBtn");
  const btnCancel = qs("#scanCancelBtn");
  const progressLine = qs("#scanProgressLine");
  if (!btnStart) return; // Overview panel not built yet
  const job = state.status && state.status.job;
  const running = job && job.state === "running";
  btnStart.classList.toggle("hidden", !!running);
  btnCancel.classList.toggle("hidden", !running);
  if (running && job.kind === "scan") {
    progressLine.classList.remove("hidden");
    const msg = job.progress && job.progress.message ? job.progress.message : "";
    const done = job.progress ? job.progress.done : 0;
    const total = job.progress ? job.progress.total : 0;
    progressLine.textContent = `Scanning… ${msg || (total > 0 ? `${done}/${total}` : "")}`.trim();
  } else {
    progressLine.classList.add("hidden");
  }
}

async function loadOverview() {
  const panel = qs("#panel-overview");
  if (!panel.dataset.built) {
    panel.innerHTML = overviewShell();
    panel.dataset.built = "1";
    qs("#scanWindow").value = state.scanWindow;
  }
  syncScanControls();

  // Make sure we have a fresh status for the empty-state check.
  if (!state.status) {
    try { state.status = await api("GET", "/api/status"); } catch (e) { /* toasted already */ }
  }

  const body = qs("#overviewBody");
  const messagesCount = state.status && state.status.counts ? state.status.counts.messages : 0;

  if (!messagesCount) {
    body.innerHTML = `
      <div class="card empty-state">
        <p><strong>No messages scanned yet.</strong></p>
        <p class="muted">Choose a scan window above and click "Scan" to analyze your mailbox. Nothing is deleted or changed by scanning — it only reads message metadata.</p>
      </div>`;
    return;
  }

  if (state.overviewLoading) return;
  state.overviewLoading = true;
  try {
    state.overview = await api("GET", "/api/overview");
  } catch (e) {
    state.overviewLoading = false;
    return;
  }
  state.overviewLoading = false;
  renderOverviewBody();
}

function renderOverviewBody() {
  const body = qs("#overviewBody");
  const ov = state.overview;
  if (!body || !ov) return;

  const unreadShare = ov.messages_total > 0 ? ov.unread_total / ov.messages_total : 0;
  const tiles = [
    { label: "Messages scanned", value: fmtInt(ov.messages_total) },
    { label: "Unread share", value: fmtPct(unreadShare) },
    { label: "Senders", value: fmtInt(ov.senders_total) },
    { label: "Subscription senders", value: fmtInt(ov.subscription_senders) },
    { label: "Never-opened senders", value: fmtInt(ov.never_opened_senders) },
    { label: "Messages trashed so far", value: fmtInt((ov.actions && ov.actions.trashed_messages) || 0) },
  ];

  const maxTop = Math.max(1, ...(ov.top_senders || []).map((s) => s.total));
  const topSendersHtml = (ov.top_senders || []).length
    ? (ov.top_senders || []).map((s) => {
        const pct = Math.round((s.total / maxTop) * 100);
        const label = s.display_name || s.sender;
        return `
        <div class="bar-row">
          <div class="bar-row-label" title="${esc(s.sender)}">${esc(label)}</div>
          <div class="bar-row-track"><div class="bar-row-fill" style="width:${pct}%"></div></div>
          <div class="bar-row-value">${fmtInt(s.total)}<span class="bar-row-sub">${fmtPct(s.open_rate)} opened</span></div>
        </div>`;
      }).join("")
    : `<p class="muted">No sender data yet.</p>`;

  const byCat = ov.by_category || {};
  const maxCat = Math.max(1, ...CATEGORIES.map((c) => byCat[c] || 0));
  const byCategoryHtml = CATEGORIES.map((c) => {
    const count = byCat[c] || 0;
    const pct = Math.round((count / maxCat) * 100);
    return `
      <div class="bar-row">
        <div class="bar-row-label">${esc(CATEGORY_LABELS[c])}</div>
        <div class="bar-row-track"><div class="bar-row-fill" style="width:${pct}%"></div></div>
        <div class="bar-row-value">${fmtInt(count)}</div>
      </div>`;
  }).join("");

  body.innerHTML = `
    <div class="stat-grid">
      ${tiles.map((t) => `
        <div class="stat-tile">
          <span class="stat-label">${esc(t.label)}</span>
          <span class="stat-value">${t.value}</span>
        </div>`).join("")}
    </div>
    <div class="two-col">
      <div class="card">
        <h2 class="section-title">Top senders by volume</h2>
        <div class="bar-list">${topSendersHtml}</div>
      </div>
      <div class="card">
        <h2 class="section-title">By category</h2>
        <div class="bar-list">${byCategoryHtml}</div>
      </div>
    </div>
  `;
}

async function handleScanStart() {
  const windowMonths = Number(qs("#scanWindow").value);
  state.scanWindow = qs("#scanWindow").value;
  try {
    await api("POST", "/api/scan", { window_months: windowMonths, full: false });
  } catch (e) {
    return; // toasted already (incl. 409 busy)
  }
  toast("Scan started.", "success");
  pollStatusSoon();
}

async function handleScanCancel() {
  try {
    await api("POST", "/api/jobs/cancel", undefined);
  } catch (e) { return; }
  toast("Cancel requested.");
  pollStatusSoon();
}

/* ======================================================================
   SUBSCRIPTIONS TAB
   ====================================================================== */

function subscriptionsShell() {
  const f = state.senders.filters;
  return `
    <div class="filters card" id="subsFilters">
      <div class="filter-field">
        <label for="fMinTotal">Min emails</label>
        <input type="number" id="fMinTotal" min="0" value="${f.min_total}">
      </div>
      <div class="filter-field">
        <label for="fMaxOpenRate">Max open %</label>
        <input type="number" id="fMaxOpenRate" min="0" max="100" value="${f.max_open_rate_pct}">
      </div>
      <div class="filter-field">
        <label for="fLastOpenedDays">Last opened before (days)</label>
        <input type="number" id="fLastOpenedDays" min="0" value="${f.last_opened_before_days}">
      </div>
      <div class="filter-field filter-checkbox" style="align-self:center;">
        <label><input type="checkbox" id="fIncludeNeverOpened" ${f.include_never_opened ? "checked" : ""}> Include never-opened</label>
      </div>
      <div class="filter-field">
        <label for="fHasUnsub">Unsubscribe link</label>
        <select id="fHasUnsub">
          <option value="any">Any</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>
      <div class="filter-field">
        <label for="fCategory">Category</label>
        <select id="fCategory">
          <option value="all">All</option>
          ${CATEGORIES.map((c) => `<option value="${c}">${esc(CATEGORY_LABELS[c])}</option>`).join("")}
        </select>
      </div>
      <div class="filter-field">
        <label for="fStatus">Status</label>
        <select id="fStatus">
          <option value="active">Active</option>
          <option value="unsubscribed">Unsubscribed</option>
          <option value="blocked">Blocked</option>
          <option value="kept">Kept</option>
          <option value="all">All</option>
        </select>
      </div>
      <div class="filter-field filter-checkbox" style="align-self:center;">
        <label><input type="checkbox" id="fSubscriptionOnly" ${f.subscription_only ? "checked" : ""}> Subscriptions only</label>
      </div>
      <div class="filter-field filter-search">
        <label for="fSearch">Search</label>
        <input type="search" id="fSearch" placeholder="Sender, name, or domain" value="${esc(f.search)}">
      </div>
      <div class="filter-field">
        <label for="fSort">Sort by</label>
        <select id="fSort">
          <option value="total">Emails</option>
          <option value="open_rate">Open %</option>
          <option value="last_received">Last received</option>
          <option value="last_opened">Last opened</option>
          <option value="per_month">Per month</option>
          <option value="sender">Sender</option>
        </select>
      </div>
      <button type="button" class="btn btn-icon" id="fOrderToggle" aria-label="Toggle sort order" title="Toggle sort order">↓</button>
      <button type="button" class="btn btn-secondary" data-action="filters-reset">Reset to defaults</button>
    </div>

    <div class="table-card">
      <div class="table-meta">
        <span id="senderMatchCount" aria-live="polite"></span>
      </div>
      <div class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" id="selectAllSenders" aria-label="Select all loaded senders"></th>
              <th>Sender</th>
              <th class="num">Emails</th>
              <th>Open %</th>
              <th>Last received</th>
              <th>Last opened</th>
              <th class="num">Per month</th>
              <th>Unsubscribe</th>
              <th>Category</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="sendersTbody"></tbody>
        </table>
      </div>
      <div class="table-footer">
        <button type="button" class="btn btn-secondary hidden" id="loadMoreSenders" data-action="load-more-senders">Load more</button>
      </div>
    </div>

    <div class="bulk-bar hidden" id="bulkBar">
      <div class="bulk-bar-inner">
        <span class="bulk-count" id="bulkSelectedCount"></span>
        <div class="bulk-actions">
          <div class="bulk-action-group">
            <button type="button" class="btn btn-primary" data-action="bulk-stop">Stop</button>
            <label class="inline-check"><input type="checkbox" id="bulkUnsub" checked> Unsubscribe</label>
            <label class="inline-check"><input type="checkbox" id="bulkBlock" checked> Block</label>
          </div>
          <div class="bulk-action-group">
            <button type="button" class="btn btn-danger" data-action="bulk-trash">Trash older than</button>
            <input type="number" id="bulkDays" class="bulk-days-input" value="30" min="0" aria-label="Trash older than, in days">
            <span class="muted">days (0 = all)</span>
            <label class="inline-check"><input type="checkbox" id="bulkSkipImportant" checked> Skip Important</label>
          </div>
          <button type="button" class="btn btn-secondary" data-action="bulk-keep">Keep</button>
        </div>
      </div>
    </div>
  `;
}

function applyFilterInputsToSelects() {
  const f = state.senders.filters;
  qs("#fHasUnsub").value = f.has_unsubscribe;
  qs("#fCategory").value = f.category;
  qs("#fStatus").value = f.status;
  qs("#fSort").value = f.sort;
  qs("#fOrderToggle").textContent = f.order === "asc" ? "↑" : "↓";
}

async function loadSubscriptions() {
  const panel = qs("#panel-subscriptions");
  if (!state.senders.built) {
    panel.innerHTML = subscriptionsShell();
    applyFilterInputsToSelects();
    state.senders.built = true;
  }
  await fetchSenders({ reset: true });
}

function currentSenderQuery() {
  const f = state.senders.filters;
  const params = {
    min_total: f.min_total,
    include_never_opened: f.include_never_opened,
    subscription_only: f.subscription_only,
    search: f.search,
    sort: f.sort,
    order: f.order,
    limit: SENDERS_PAGE_LIMIT,
    offset: state.senders.offset,
  };
  if (f.max_open_rate_pct !== "" && f.max_open_rate_pct != null) {
    params.max_open_rate = Number(f.max_open_rate_pct) / 100;
  }
  if (f.last_opened_before_days !== "" && f.last_opened_before_days != null) {
    params.last_opened_before_days = f.last_opened_before_days;
  }
  if (f.has_unsubscribe !== "any") {
    params.has_unsubscribe = f.has_unsubscribe === "yes";
  }
  if (f.category !== "all") {
    params.categories = f.category;
  }
  params.status = f.status === "all" ? SENDER_STATUSES.join(",") : f.status;
  return params;
}

async function fetchSenders({ reset }) {
  if (reset) {
    state.senders.offset = 0;
    state.senders.items = [];
  }
  state.senders.loading = true;
  let data;
  try {
    data = await apiGet("/api/senders", currentSenderQuery());
  } catch (e) {
    state.senders.loading = false;
    return;
  }
  state.senders.loading = false;
  if (reset) {
    state.senders.items = data.items;
  } else {
    state.senders.items = state.senders.items.concat(data.items);
  }
  state.senders.total = data.total;
  renderSendersTable();
}

function renderSendersTable() {
  const tbody = qs("#sendersTbody");
  const matchCount = qs("#senderMatchCount");
  if (!tbody) return;
  matchCount.textContent = `${fmtInt(state.senders.total)} senders match`;

  if (!state.senders.items.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">No senders match these filters.</td></tr>`;
  } else {
    tbody.innerHTML = state.senders.items.map(senderRowHtml).join("");
  }

  const loadMore = qs("#loadMoreSenders");
  loadMore.classList.toggle("hidden", state.senders.items.length >= state.senders.total);

  const selectAll = qs("#selectAllSenders");
  const loadedSenders = state.senders.items.map((s) => s.sender);
  const allSelected = loadedSenders.length > 0 && loadedSenders.every((s) => state.senders.selected.has(s));
  selectAll.checked = allSelected;

  syncCheckedRowStyles();
  renderBulkBar();
}

function senderRowHtml(s) {
  const checked = state.senders.selected.has(s.sender) ? "checked" : "";
  const expanded = state.senders.expanded.has(s.sender);
  return `
    <tr class="sender-row${state.senders.selected.has(s.sender) ? " selected" : ""}" data-sender="${esc(s.sender)}">
      <td><input type="checkbox" class="sender-check" data-sender="${esc(s.sender)}" aria-label="Select ${esc(s.display_name || s.sender)}" ${checked}></td>
      <td>
        <div class="sender-cell">
          <span class="sender-name">${esc(s.display_name || s.sender)}</span>
          <span class="sender-email">${esc(s.sender)}</span>
          <span class="sender-domain">${esc(s.domain)}</span>
        </div>
      </td>
      <td class="num">${fmtInt(s.total)}</td>
      <td>${openMeterHtml(s.opened, s.total, s.open_rate)}</td>
      <td>${esc(relTime(s.last_received))}</td>
      <td>${s.last_opened ? esc(relTime(s.last_opened)) : "never"}</td>
      <td class="num">${fmtPerMonth(s.per_month)}</td>
      <td>${unsubBadgeHtml(s.unsubscribe_kind)}</td>
      <td>${esc(CATEGORY_LABELS[s.category] || s.category)}</td>
      <td>${senderStatusBadgeHtml(s.status)}</td>
    </tr>
    <tr class="sender-expansion" data-sender-expansion="${esc(s.sender)}" ${expanded ? "" : "hidden"}>
      <td colspan="10">
        <div class="expansion-content">
          <div class="sample-subjects">
            <h4>Recent subjects</h4>
            ${(s.sample_subjects || []).length
              ? `<ul>${s.sample_subjects.map((subj) => `<li>${esc(subj)}</li>`).join("")}</ul>`
              : `<p class="muted">No subjects recorded.</p>`}
          </div>
          <button type="button" class="btn btn-link" data-action="show-recent" data-sender="${esc(s.sender)}">Show recent</button>
          <div class="recent-messages" data-recent-for="${esc(s.sender)}"></div>
        </div>
      </td>
    </tr>
  `;
}

function syncCheckedRowStyles() {
  qsa(".sender-row").forEach((row) => {
    row.classList.toggle("selected", state.senders.selected.has(row.dataset.sender));
  });
}

function renderBulkBar() {
  const bar = qs("#bulkBar");
  const count = state.senders.selected.size;
  bar.classList.toggle("hidden", count === 0);
  qs("#bulkSelectedCount").textContent = `${count} selected`;
}

function persistFilters() {
  storageSet("inboxCleanup.subscriptionFilters", state.senders.filters);
}

const debouncedSearchReload = debounce(() => fetchSenders({ reset: true }), 300);

function onSubscriptionsFilterChange(e) {
  const t = e.target;
  const f = state.senders.filters;
  if (t.id === "fMinTotal") f.min_total = Number(t.value) || 0;
  else if (t.id === "fMaxOpenRate") f.max_open_rate_pct = t.value === "" ? "" : Number(t.value);
  else if (t.id === "fLastOpenedDays") f.last_opened_before_days = t.value === "" ? "" : Number(t.value);
  else if (t.id === "fIncludeNeverOpened") f.include_never_opened = t.checked;
  else if (t.id === "fHasUnsub") f.has_unsubscribe = t.value;
  else if (t.id === "fCategory") f.category = t.value;
  else if (t.id === "fStatus") f.status = t.value;
  else if (t.id === "fSubscriptionOnly") f.subscription_only = t.checked;
  else return;
  persistFilters();
  fetchSenders({ reset: true });
}

function onSubscriptionsSearchInput(e) {
  state.senders.filters.search = e.target.value;
  persistFilters();
  debouncedSearchReload();
}

function onSubscriptionsSortChange(e) {
  state.senders.filters.sort = e.target.value;
  persistFilters();
  fetchSenders({ reset: true });
}

function onOrderToggle() {
  const f = state.senders.filters;
  f.order = f.order === "asc" ? "desc" : "asc";
  qs("#fOrderToggle").textContent = f.order === "asc" ? "↑" : "↓";
  persistFilters();
  fetchSenders({ reset: true });
}

function onFiltersReset() {
  state.senders.filters = Object.assign({}, SUBS_DEFAULT_FILTERS);
  persistFilters();
  const panel = qs("#panel-subscriptions");
  panel.innerHTML = subscriptionsShell();
  applyFilterInputsToSelects();
  fetchSenders({ reset: true });
}

function onSelectAllSenders(e) {
  const loadedSenders = state.senders.items.map((s) => s.sender);
  if (e.target.checked) {
    loadedSenders.forEach((s) => state.senders.selected.add(s));
  } else {
    loadedSenders.forEach((s) => state.senders.selected.delete(s));
  }
  renderSendersTable();
}

function onSenderCheckChange(e) {
  const sender = e.target.dataset.sender;
  if (e.target.checked) state.senders.selected.add(sender);
  else state.senders.selected.delete(sender);
  syncCheckedRowStyles();
  renderBulkBar();
  const selectAll = qs("#selectAllSenders");
  const loadedSenders = state.senders.items.map((s) => s.sender);
  selectAll.checked = loadedSenders.length > 0 && loadedSenders.every((s) => state.senders.selected.has(s));
}

function onSenderRowClick(row, e) {
  if (e.target.closest("input,button,a,select")) return;
  const sender = row.dataset.sender;
  const expansion = qs(`tr.sender-expansion[data-sender-expansion="${cssEscape(sender)}"]`);
  if (!expansion) return;
  const willExpand = expansion.hidden;
  expansion.hidden = !willExpand;
  if (willExpand) state.senders.expanded.add(sender);
  else state.senders.expanded.delete(sender);
}

function cssEscape(value) {
  if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
  return String(value).replace(/["\\]/g, "\\$&");
}

async function onShowRecent(sender, btn) {
  const container = qs(`[data-recent-for="${cssEscape(sender)}"]`);
  if (!container) return;
  container.innerHTML = `<p class="muted">Loading…</p>`;
  let data;
  try {
    data = await api("GET", `/api/senders/${encodeURIComponent(sender)}/messages?limit=20`);
  } catch (e) {
    container.innerHTML = `<p class="muted">Could not load recent messages.</p>`;
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    container.innerHTML = `<p class="muted">No recent messages found.</p>`;
    return;
  }
  container.innerHTML = items.map((m) => `
    <div class="recent-row">
      ${m.is_unread ? '<span class="unread-dot" title="Unread">●</span>' : '<span class="muted" title="Read">○</span>'}
      <span class="subj">${esc(m.subject || "(no subject)")}</span>
      <span class="muted">${esc(fmtDateTime(m.date))}</span>
    </div>
  `).join("");
}

async function handleLoadMoreSenders() {
  state.senders.offset += SENDERS_PAGE_LIMIT;
  await fetchSenders({ reset: false });
}

/* ---- bulk actions: preview -> confirm -> execute ---- */

function actionPreviewRowsHtml(action, items) {
  let inner;
  if (action === "stop") {
    inner = `<table class="data-table"><thead><tr><th>Sender</th><th>Method</th><th>Unsubscribe</th><th>Block</th></tr></thead><tbody>
      ${items.map((it) => `<tr>
        <td>${esc(it.display_name || it.sender)}</td>
        <td>${unsubBadgeHtml(it.unsubscribe_method)}</td>
        <td>${it.will_unsubscribe ? "Yes" : "No"}</td>
        <td>${it.will_block ? "Yes" : "No"}</td>
      </tr>`).join("")}
    </tbody></table>`;
  } else if (action === "purge") {
    inner = `<table class="data-table"><thead><tr><th>Sender</th><th class="num">Will trash</th><th class="num">Skip starred</th><th class="num">Skip important</th></tr></thead><tbody>
      ${items.map((it) => `<tr>
        <td>${esc(it.display_name || it.sender)}</td>
        <td class="num">${fmtInt(it.purge_count)}</td>
        <td class="num">${fmtInt(it.skipped_starred)}</td>
        <td class="num">${fmtInt(it.skipped_important)}</td>
      </tr>`).join("")}
    </tbody></table>`;
  } else {
    // keep / reactivate
    inner = `<table class="data-table"><thead><tr><th>Sender</th></tr></thead><tbody>
      ${items.map((it) => `<tr><td>${esc(it.display_name || it.sender)}</td></tr>`).join("")}
    </tbody></table>`;
  }
  return `<div class="table-scroll">${inner}</div>`;
}

function totalsHtml(totals) {
  const entries = Object.entries(totals || {});
  if (!entries.length) return "";
  return `<div class="preview-totals">${entries.map(([k, v]) => `<span>${esc(titleCaseKey(k))}: <strong>${fmtInt(v)}</strong></span>`).join("")}</div>`;
}

async function runBulkAction(actionRequest, titleLabel) {
  let preview;
  try {
    preview = await api("POST", "/api/actions/preview", actionRequest);
  } catch (e) {
    return; // toasted (incl. 409)
  }
  const bodyHtml = `
    <p>${esc(titleLabel)} for ${preview.items.length} sender(s):</p>
    ${actionPreviewRowsHtml(preview.action, preview.items)}
    ${totalsHtml(preview.totals)}
  `;
  openModal({
    title: "Confirm action",
    bodyHtml,
    footerButtons: [
      { label: "Cancel", className: "btn-secondary", onClick: () => closeModal() },
      {
        label: "Confirm",
        className: "btn-primary",
        onClick: async () => {
          try {
            await api("POST", "/api/actions/execute", actionRequest);
          } catch (e) {
            return;
          }
          closeModal();
          // The senders are now being processed; a stale selection must not
          // leak into the next action (rows that leave the list would
          // otherwise stay selected invisibly).
          state.senders.selected.clear();
          renderSendersTable();
          renderBulkBar();
          toast("Action started.", "success");
          pollStatusSoon();
        },
      },
    ],
  });
}

function handleBulkStop() {
  const senders = Array.from(state.senders.selected);
  if (!senders.length) return;
  const req = {
    action: "stop",
    senders,
    unsubscribe: qs("#bulkUnsub").checked,
    block: qs("#bulkBlock").checked,
  };
  runBulkAction(req, "Stop");
}

function handleBulkTrash() {
  const senders = Array.from(state.senders.selected);
  if (!senders.length) return;
  const days = Number(qs("#bulkDays").value) || 0;
  const req = {
    action: "purge",
    senders,
    purge_older_than_days: days,
    skip_important: qs("#bulkSkipImportant").checked,
  };
  runBulkAction(req, "Trash messages older than " + (days === 0 ? "any age" : days + " days"));
}

function handleBulkKeep() {
  const senders = Array.from(state.senders.selected);
  if (!senders.length) return;
  const req = { action: "keep", senders };
  runBulkAction(req, "Keep");
}

/* ======================================================================
   ORGANIZE TAB
   ====================================================================== */

function organizeShell() {
  return `
    <div class="card">
      <h2 class="section-title">Categories</h2>
      <div class="category-editor-head">
        <span>Key</span><span>Label name</span><span>Description</span><span></span>
      </div>
      <div id="categoryRows"></div>
      <div style="margin-top:10px; display:flex; gap:8px;">
        <button type="button" class="btn btn-secondary" data-action="category-add">Add category</button>
        <button type="button" class="btn btn-primary" data-action="category-save">Save categories</button>
      </div>
    </div>

    <div class="card">
      <h2 class="section-title">Classify mail</h2>
      <div class="filters" style="align-items:end;">
        <div class="filter-field">
          <label for="orgScope">Scope</label>
          <select id="orgScope">
            <option value="inbox">Inbox only</option>
            <option value="all">All mail</option>
          </select>
        </div>
        <div class="filter-field">
          <label for="orgDays">Days</label>
          <input type="number" id="orgDays" value="365" min="1">
        </div>
        <button type="button" class="btn btn-primary" id="classifyBtn" data-action="organize-classify" disabled>Classify</button>
      </div>
      <div class="estimate-result" id="estimateCard"></div>
      <p class="job-progress-line hidden" id="classifyProgressLine" aria-live="polite"></p>
    </div>

    <div class="card">
      <h2 class="section-title">Results</h2>
      <div class="summary-chips" id="categoryChips"></div>
      <div class="table-scroll" style="margin-top:10px;">
        <table class="data-table">
          <thead>
            <tr><th>Subject</th><th>Sender</th><th>Date</th><th>Category</th><th>Confidence</th><th>Model</th><th>Applied</th></tr>
          </thead>
          <tbody id="resultsTbody"></tbody>
        </table>
      </div>
      <div class="table-footer">
        <button type="button" class="btn btn-secondary hidden" id="loadMoreResults" data-action="load-more-results">Load more</button>
      </div>
      <div style="margin-top:12px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; border-top:1px solid var(--hairline); padding-top:12px;">
        <label class="inline-check"><input type="checkbox" id="applyArchive"> Archive (remove from inbox)</label>
        <label class="inline-check"><input type="checkbox" id="applyMarkRead"> Mark read</label>
        <button type="button" class="btn btn-primary" data-action="organize-apply-open">Apply labels</button>
      </div>
    </div>
  `;
}

async function loadOrganize() {
  const panel = qs("#panel-organize");
  if (!state.organize.built) {
    panel.innerHTML = organizeShell();
    state.organize.built = true;
  }
  try {
    const cats = await api("GET", "/api/organize/categories");
    state.organize.categories = cats.items;
  } catch (e) { /* toasted */ }
  renderCategoryRows();
  syncClassifyProgress();
  await fetchEstimate();
  await fetchResults({ reset: true });
}

function renderCategoryRows() {
  const wrap = qs("#categoryRows");
  if (!wrap) return;
  wrap.innerHTML = state.organize.categories.map((c, idx) => {
    const locked = c.key === "skip";
    return `
    <div class="category-editor-row${locked ? " locked" : ""}" data-idx="${idx}">
      <input type="text" data-field="key" value="${esc(c.key)}" ${locked ? "disabled" : ""} aria-label="Category key">
      <input type="text" data-field="label_name" value="${esc(c.label_name || "")}" ${locked ? "disabled" : ""} placeholder="(no label)" aria-label="Label name">
      <input type="text" data-field="description" value="${esc(c.description || "")}" ${locked ? "disabled" : ""} aria-label="Description">
      ${locked ? '<span class="muted">fixed</span>' : `<button type="button" class="btn btn-icon" data-action="category-remove" data-idx="${idx}" aria-label="Remove category">&times;</button>`}
    </div>`;
  }).join("");
}

function onCategoryFieldInput(e) {
  const row = e.target.closest(".category-editor-row");
  if (!row) return;
  const idx = Number(row.dataset.idx);
  const field = e.target.dataset.field;
  if (!field) return;
  state.organize.categories[idx][field] = e.target.value;
}

function handleCategoryAdd() {
  state.organize.categories.push({ key: "", label_name: "", description: "" });
  renderCategoryRows();
}

function handleCategoryRemove(idx) {
  state.organize.categories.splice(idx, 1);
  renderCategoryRows();
}

async function handleCategorySave() {
  try {
    const res = await api("PUT", "/api/organize/categories", { items: state.organize.categories });
    state.organize.categories = res.items;
  } catch (e) { return; }
  renderCategoryRows();
  renderCategorySelectsInResults();
  toast("Categories saved.", "success");
}

function syncClassifyProgress() {
  const line = qs("#classifyProgressLine");
  if (!line) return;
  const job = state.status && state.status.job;
  if (job && job.state === "running" && job.kind === "classify") {
    line.classList.remove("hidden");
    const done = job.progress ? job.progress.done : 0;
    const total = job.progress ? job.progress.total : 0;
    const msg = job.progress && job.progress.message ? job.progress.message : "";
    line.textContent = `Classifying… ${msg || (total > 0 ? `${done}/${total}` : "")}`.trim();
  } else {
    line.classList.add("hidden");
  }
}

async function fetchEstimate() {
  const scope = qs("#orgScope") ? qs("#orgScope").value : state.organize.scope;
  const days = qs("#orgDays") ? Number(qs("#orgDays").value) || 365 : state.organize.days;
  state.organize.scope = scope;
  state.organize.days = days;
  let est;
  try {
    est = await apiGet("/api/organize/estimate", { scope, days });
  } catch (e) {
    return;
  }
  state.organize.estimate = est;
  renderEstimateCard();
}

function renderEstimateCard() {
  const card = qs("#estimateCard");
  const btn = qs("#classifyBtn");
  const est = state.organize.estimate;
  if (!card || !est) return;
  card.innerHTML = `
    <div class="estimate-item"><span class="val">${fmtInt(est.candidates)}</span><span class="lbl">Candidates</span></div>
    <div class="estimate-item"><span class="val">${fmtInt(est.already_classified)}</span><span class="lbl">Already classified</span></div>
    <div class="estimate-item"><span class="val">${fmtMoney(est.est_cost_usd)}</span><span class="lbl">Estimated cost</span></div>
    <div class="estimate-item"><span class="val">${esc(est.model)}</span><span class="lbl">Model</span></div>
    <div class="estimate-item"><span class="val">${esc(est.escalate_model)}</span><span class="lbl">Escalation model</span></div>
  `;
  btn.disabled = !est.candidates;
}

async function handleClassify() {
  const req = { scope: state.organize.scope, days: state.organize.days, reclassify: false };
  try {
    await api("POST", "/api/organize/classify", req);
  } catch (e) { return; }
  toast("Classification started.", "success");
  pollStatusSoon();
}

async function fetchResults({ reset }) {
  if (reset) {
    state.organize.resultsOffset = 0;
  }
  const params = {
    limit: RESULTS_PAGE_LIMIT,
    offset: state.organize.resultsOffset,
  };
  if (state.organize.filterCategory) params.category = state.organize.filterCategory;
  let data;
  try {
    data = await apiGet("/api/organize/results", params);
  } catch (e) { return; }
  state.organize.results = data;
  if (reset || !state.organize._items) {
    state.organize._items = data.items;
  } else {
    state.organize._items = state.organize._items.concat(data.items);
  }
  renderCategoryChips();
  renderResultsTable();
}

function renderCategoryChips() {
  const wrap = qs("#categoryChips");
  const results = state.organize.results;
  if (!wrap || !results) return;
  const summary = results.summary || {};
  const cats = state.organize.categories.length ? state.organize.categories : Object.keys(summary).map((k) => ({ key: k, label_name: k }));
  const chips = [`<button type="button" class="chip" data-cat="" aria-pressed="${!state.organize.filterCategory}">All (${fmtInt(results.total)})</button>`]
    .concat(cats.map((c) => {
      const count = summary[c.key] || 0;
      return `<button type="button" class="chip" data-cat="${esc(c.key)}" aria-pressed="${state.organize.filterCategory === c.key}">${esc(c.label_name || c.key)} (${fmtInt(count)})</button>`;
    }));
  wrap.innerHTML = chips.join("");
}

function categorySelectOptionsHtml(selected) {
  return state.organize.categories.map((c) =>
    `<option value="${esc(c.key)}" ${c.key === selected ? "selected" : ""}>${esc(c.label_name || c.key)}</option>`
  ).join("");
}

function renderResultsTable() {
  const tbody = qs("#resultsTbody");
  const items = state.organize._items || [];
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No classified messages yet. Run "Classify" above.</td></tr>`;
  } else {
    tbody.innerHTML = items.map((it) => `
      <tr data-message-id="${esc(it.message_id)}">
        <td class="subject-cell">${esc(it.subject || "(no subject)")}</td>
        <td class="sender-muted">${esc(it.display_name || it.sender)}</td>
        <td>${esc(fmtDateTime(it.date))}</td>
        <td>
          <select class="result-category-select" data-message-id="${esc(it.message_id)}">
            ${categorySelectOptionsHtml(it.category)}
          </select>
        </td>
        <td>${confidenceMeterHtml(it.confidence)}${it.confidence < 0.6 && !it.overridden ? '<span class="pill pill-warning review-badge">⚑ Review</span>' : ""}</td>
        <td class="sender-muted">${esc(it.model)}</td>
        <td>${it.applied ? '<span class="pill pill-good">✓ Applied</span>' : '<span class="muted">—</span>'}</td>
      </tr>
    `).join("");
  }
  const loadMore = qs("#loadMoreResults");
  const results = state.organize.results;
  loadMore.classList.toggle("hidden", !results || items.length >= results.total);
}

function renderCategorySelectsInResults() {
  qsa(".result-category-select").forEach((sel) => {
    const current = sel.value;
    sel.innerHTML = categorySelectOptionsHtml(current);
  });
}

async function handleResultCategoryChange(e) {
  const sel = e.target;
  const messageId = sel.dataset.messageId;
  const newCategory = sel.value;
  try {
    await api("PUT", `/api/organize/results/${encodeURIComponent(messageId)}`, { category: newCategory });
  } catch (e2) { return; }
  const item = (state.organize._items || []).find((it) => it.message_id === messageId);
  if (item) {
    item.category = newCategory;
    item.confidence = 1.0;
    item.overridden = true;
  }
  renderResultsTable();
  toast("Category updated.", "success");
}

function handleCategoryChipClick(cat) {
  state.organize.filterCategory = cat || null;
  qsa("#categoryChips .chip").forEach((c) => c.setAttribute("aria-pressed", c.dataset.cat === (cat || "") ? "true" : "false"));
  fetchResults({ reset: true });
}

async function handleLoadMoreResults() {
  state.organize.resultsOffset += RESULTS_PAGE_LIMIT;
  await fetchResults({ reset: false });
}

function handleApplyOpen() {
  const archive = qs("#applyArchive").checked;
  const markRead = qs("#applyMarkRead").checked;
  const summary = (state.organize.results && state.organize.results.summary) || {};
  const rows = Object.entries(summary)
    .filter(([k]) => k !== "skip")
    .map(([k, v]) => {
      const def = state.organize.categories.find((c) => c.key === k);
      return `<tr><td>${esc(def ? (def.label_name || def.key) : k)}</td><td class="num">${fmtInt(v)}</td></tr>`;
    }).join("");
  const bodyHtml = `
    <p>Apply labels to classified messages${archive ? ", archive them" : ""}${markRead ? ", and mark them read" : ""}. The "skip" category is never labeled.</p>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>Category</th><th class="num">Messages</th></tr></thead><tbody>${rows || '<tr><td colspan="2" class="muted">No categorized messages yet.</td></tr>'}</tbody></table></div>
  `;
  openModal({
    title: "Apply labels",
    bodyHtml,
    footerButtons: [
      { label: "Cancel", className: "btn-secondary", onClick: () => closeModal() },
      {
        label: "Apply",
        className: "btn-primary",
        onClick: async () => {
          try {
            await api("POST", "/api/organize/apply", { categories: null, archive, mark_read: markRead });
          } catch (e) { return; }
          closeModal();
          toast("Applying labels…", "success");
          pollStatusSoon();
        },
      },
    ],
  });
}

/* ======================================================================
   ACTIVITY TAB
   ====================================================================== */

function activityShell() {
  return `
    <div class="table-card">
      <div class="table-scroll">
        <table class="data-table">
          <thead>
            <tr><th>Time</th><th>Action</th><th>Sender</th><th>Detail</th><th class="num">Count</th><th>Status</th><th></th></tr>
          </thead>
          <tbody id="activityTbody"></tbody>
        </table>
      </div>
    </div>
  `;
}

async function loadActivity() {
  const panel = qs("#panel-activity");
  if (!panel.dataset.built) {
    panel.innerHTML = activityShell();
    panel.dataset.built = "1";
  }
  let data;
  try {
    data = await apiGet("/api/actions/log", { limit: 100, offset: 0 });
  } catch (e) { return; }
  state.activity = data;
  renderActivityTable();
}

function renderDetail(entry) {
  const d = entry.detail || {};
  const parts = [];
  // The unsubscribe method may be reported as "method" or "kind" (backends
  // vary); render it as the same badge used in the Subscriptions table.
  const method = d.method || d.kind;
  if (method) parts.push(unsubBadgeHtml(method));
  if (d.url) parts.push(`<a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Open link</a>`);
  if (d.to) parts.push(`To: ${esc(d.to)}`);
  if (d.filter_id) parts.push(`Filter: ${esc(d.filter_id)}`);
  if (d.label) parts.push(`Label: ${esc(d.label)}`);
  const known = new Set(["method", "kind", "url", "to", "filter_id", "label"]);
  Object.keys(d).forEach((k) => {
    if (known.has(k)) return;
    parts.push(`${esc(titleCaseKey(k))}: ${esc(d[k])}`);
  });
  return parts.length ? parts.join(" · ") : '<span class="muted">—</span>';
}

function renderActivityTable() {
  const tbody = qs("#activityTbody");
  if (!tbody) return;
  const items = state.activity.items || [];
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No activity yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map((it) => `
    <tr data-log-id="${it.id}">
      <td title="${esc(it.ts)}">${esc(fmtDateTime(it.ts))}</td>
      <td>${esc(titleCaseKey(it.action))}</td>
      <td class="sender-muted">${esc(it.sender || "—")}</td>
      <td>${renderDetail(it)}</td>
      <td class="num">${fmtInt(it.message_count)}</td>
      <td>${logStatusBadgeHtml(it.status)}</td>
      <td>${it.undoable && !it.undone ? `<button type="button" class="btn btn-secondary" data-action="undo-action" data-id="${it.id}">Undo</button>` : (it.undone ? '<span class="muted">Undone</span>' : "")}</td>
    </tr>
  `).join("");
}

async function handleUndo(id) {
  try {
    await api("POST", `/api/actions/undo/${encodeURIComponent(id)}`, undefined);
  } catch (e) { return; }
  toast("Action undone.", "success");
  await loadActivity();
}

/* ======================================================================
   SETTINGS TAB
   ====================================================================== */

function settingsShell() {
  return `
    <div class="card settings-grid" style="display:flex; flex-direction:column; gap:16px;">
      <div style="display:flex; gap:16px; flex-wrap:wrap;">
        <div class="field">
          <label for="setWindow">Default scan window (months)</label>
          <input type="number" id="setWindow" min="0">
        </div>
        <div class="field">
          <label for="setPurgeDays">Default purge older-than (days)</label>
          <input type="number" id="setPurgeDays" min="0">
        </div>
        <div class="field checkbox">
          <label><input type="checkbox" id="setSkipImportant"> Skip important when trashing</label>
        </div>
      </div>
      <fieldset class="field-group">
        <legend>Default subscription filters</legend>
        <div class="field"><label for="setDfMinTotal">Min emails</label><input type="number" id="setDfMinTotal" min="0"></div>
        <div class="field"><label for="setDfMaxOpenRate">Max open %</label><input type="number" id="setDfMaxOpenRate" min="0" max="100"></div>
        <div class="field"><label for="setDfLastOpenedDays">Last opened before (days)</label><input type="number" id="setDfLastOpenedDays" min="0"></div>
        <div class="field checkbox"><label><input type="checkbox" id="setDfIncludeNeverOpened"> Include never-opened</label></div>
        <div class="field checkbox"><label><input type="checkbox" id="setDfSubscriptionOnly"> Subscription senders only</label></div>
      </fieldset>
      <div style="display:flex; gap:16px; flex-wrap:wrap;">
        <div class="field"><label>Classify model</label><input type="text" id="setClassifyModel" disabled></div>
        <div class="field"><label>Escalation model</label><input type="text" id="setEscalateModel" disabled></div>
        <div class="field"><label>Mode</label><input type="text" id="setMode" disabled></div>
      </div>
      <div>
        <button type="button" class="btn btn-primary" data-action="settings-save">Save settings</button>
      </div>
    </div>
    <div class="card" id="authCard"></div>
  `;
}

async function loadSettings() {
  const panel = qs("#panel-settings");
  if (!panel.dataset.built) {
    panel.innerHTML = settingsShell();
    panel.dataset.built = "1";
  }
  let s;
  try {
    s = await api("GET", "/api/settings");
  } catch (e) { return; }
  state.settings = s;
  qs("#setWindow").value = s.window_months;
  qs("#setPurgeDays").value = s.purge_older_than_days;
  qs("#setSkipImportant").checked = !!s.skip_important;
  const df = s.default_filters || {};
  qs("#setDfMinTotal").value = df.min_total != null ? df.min_total : 3;
  qs("#setDfMaxOpenRate").value = df.max_open_rate != null ? Math.round(df.max_open_rate * 100) : 10;
  qs("#setDfLastOpenedDays").value = df.last_opened_before_days != null ? df.last_opened_before_days : 90;
  qs("#setDfIncludeNeverOpened").checked = df.include_never_opened !== false;
  qs("#setDfSubscriptionOnly").checked = df.subscription_only !== false;
  qs("#setClassifyModel").value = s.classify_model || "";
  qs("#setEscalateModel").value = s.escalate_model || "";
  qs("#setMode").value = state.status ? state.status.mode : "";

  renderAuthCard();
}

function renderAuthCard() {
  const card = qs("#authCard");
  if (!card || !state.status) return;
  const s = state.status;
  if (s.mode === "demo") {
    card.innerHTML = `
      <h2 class="section-title">Switch to live mode</h2>
      <p class="muted">This is a demo mailbox with synthetic data — nothing here touches a real Gmail account.
      To connect a real mailbox: set <code>DEMO=0</code> in the environment, add a Google OAuth
      <code>credentials.json</code> file to the project root, then restart the server.</p>
    `;
  } else if (s.authenticated) {
    card.innerHTML = `
      <h2 class="section-title">Account</h2>
      <p>Signed in as <strong>${esc(s.email || "")}</strong></p>
      <button type="button" class="btn btn-secondary" data-action="auth-logout">Sign out</button>
    `;
  } else {
    card.innerHTML = `
      <h2 class="section-title">Account</h2>
      <p class="muted">Not signed in yet.</p>
      <button type="button" class="btn btn-primary" data-action="auth-login">Sign in with Google</button>
      <p class="muted">A browser window will open to complete Google sign-in; this can take up to a minute.</p>
    `;
  }
}

async function handleSettingsSave() {
  const payload = {
    window_months: Number(qs("#setWindow").value) || 0,
    purge_older_than_days: Number(qs("#setPurgeDays").value) || 0,
    skip_important: qs("#setSkipImportant").checked,
    default_filters: {
      min_total: Number(qs("#setDfMinTotal").value) || 0,
      max_open_rate: Number(qs("#setDfMaxOpenRate").value) / 100,
      last_opened_before_days: Number(qs("#setDfLastOpenedDays").value) || 0,
      include_never_opened: qs("#setDfIncludeNeverOpened").checked,
      subscription_only: qs("#setDfSubscriptionOnly").checked,
    },
  };
  let s;
  try {
    s = await api("PUT", "/api/settings", payload);
  } catch (e) { return; }
  state.settings = s;
  toast("Settings saved.", "success");
}

async function handleAuthLogin(btn) {
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    await api("POST", "/api/auth/login", undefined);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Sign in with Google";
    return;
  }
  toast("Signed in.", "success");
  state.status = await api("GET", "/api/status");
  renderHeader();
  renderAuthCard();
}

async function handleAuthLogout() {
  try {
    await api("POST", "/api/auth/logout", undefined);
  } catch (e) { return; }
  toast("Signed out.", "success");
  state.status = await api("GET", "/api/status");
  renderHeader();
  renderAuthCard();
}

/* ======================================================================
   EVENT DELEGATION
   ====================================================================== */

function onDocumentClick(e) {
  const tabBtn = e.target.closest(".tab");
  if (tabBtn) {
    activateTab(tabBtn.dataset.tab);
    return;
  }

  const actionEl = e.target.closest("[data-action]");
  const action = actionEl && actionEl.dataset.action;

  if (action === "modal-close") return closeModal();
  if (action === "scan-start") return handleScanStart();
  if (action === "scan-cancel") return handleScanCancel();
  if (action === "filters-reset") return onFiltersReset();
  if (action === "load-more-senders") return handleLoadMoreSenders();
  if (action === "bulk-stop") return handleBulkStop();
  if (action === "bulk-trash") return handleBulkTrash();
  if (action === "bulk-keep") return handleBulkKeep();
  if (action === "show-recent") return onShowRecent(actionEl.dataset.sender, actionEl);
  if (action === "category-add") return handleCategoryAdd();
  if (action === "category-remove") return handleCategoryRemove(Number(actionEl.dataset.idx));
  if (action === "category-save") return handleCategorySave();
  if (action === "organize-classify") return handleClassify();
  if (action === "load-more-results") return handleLoadMoreResults();
  if (action === "organize-apply-open") return handleApplyOpen();
  if (action === "settings-save") return handleSettingsSave();
  if (action === "auth-login") return handleAuthLogin(actionEl);
  if (action === "auth-logout") return handleAuthLogout();
  if (action === "undo-action") return handleUndo(actionEl.dataset.id);

  const chip = e.target.closest(".chip");
  if (chip && chip.closest("#categoryChips")) {
    return handleCategoryChipClick(chip.dataset.cat);
  }

  const senderRow = e.target.closest("tr.sender-row");
  if (senderRow) return onSenderRowClick(senderRow, e);
}

function onDocumentChange(e) {
  const t = e.target;
  if (t.id === "selectAllSenders") return onSelectAllSenders(e);
  if (t.classList.contains("sender-check")) return onSenderCheckChange(e);
  if (t.matches("#subsFilters select, #subsFilters input") && t.id !== "fSearch") return onSubscriptionsFilterChange(e);
  if (t.id === "orgScope") return fetchEstimate();
  if (t.id === "orgDays") return fetchEstimate();
  if (t.classList.contains("result-category-select")) return handleResultCategoryChange(e);
  if (t.matches(".category-editor-row input")) return onCategoryFieldInput(e);
}

function onDocumentInput(e) {
  const t = e.target;
  if (t.id === "fSearch") return onSubscriptionsSearchInput(e);
  if (t.matches(".category-editor-row input")) return onCategoryFieldInput(e);
}

document.addEventListener("click", (e) => {
  if (e.target.id === "fOrderToggle") return onOrderToggle();
  onDocumentClick(e);
});
document.addEventListener("change", onDocumentChange);
document.addEventListener("input", onDocumentInput);

/* ======================================================================
   INIT
   ====================================================================== */

function init() {
  activateTab(state.activeTab, { skipHistory: true });
  pollStatusTick();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
