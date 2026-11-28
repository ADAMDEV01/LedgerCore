"use strict";

/* LedgerCore SPA. All dynamic text is inserted with textContent, never innerHTML,
   so API data cannot inject markup (the CSP also forbids inline scripts). */

let BASE_CURRENCY = "USD";
const state = { user: null, view: "dashboard", accounts: [], navigationId: 0, dataVersion: 0 };
const viewCache = new Map();
const readCache = new Map();
const VIEW_CACHE_TTL_MS = 30_000;
const READ_CACHE_TTL_MS = 30_000;
let viewPrefetchPromise = null;
let viewPrefetchGeneration = 0;

function invalidateViewData() {
  state.dataVersion += 1;
  readCache.clear();
  for (const entry of viewCache.values()) entry.updatedAt = 0;
}

const $ = (selector) => document.querySelector(selector);
const isoToday = () => new Date().toISOString().slice(0, 10);
const fmt = (value) =>
  Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const svgEl = (tag, attrs = {}, text) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (text !== undefined) el.textContent = text;
  return el;
};

function toast(message, kind = "") {
  const node = h("div", { class: `toast ${kind}`, text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 5000);
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function errorMessage(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    }
  } catch (_) { /* fall through */ }
  return `Request failed (${res.status})`;
}

function idempotencyStorageKey(path, body) {
  return `ledgercore:idempotency:${path}:${JSON.stringify(body ?? null)}`;
}

function financialWrite(path, method) {
  return method === "POST" && (/^\/journal-entries(?:\/|$)/.test(path) || /^\/invoices(?:\/|$)/.test(path));
}

let refreshPromise = null;
const REFRESH_LOCK_NAME = "ledgercore-refresh-session";
const REFRESH_LEASE_KEY = "ledgercore:refresh-session-lease";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function performRefresh() {
  let response;
  try {
    response = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken() },
      credentials: "same-origin",
    });
  } catch (_) {
    return false;
  }
  if (!response.ok) return false;
  try {
    const data = await response.clone().json();
    if (data && data.user) state.user = data.user;
  } catch (_) { /* the caller only needs the refreshed cookies */ }
  return true;
}

async function sessionIsAlreadyFresh() {
  try {
    const response = await fetch("/api/v1/auth/me", { credentials: "same-origin" });
    if (!response.ok) return false;
    state.user = await response.json();
    return true;
  } catch (_) {
    return false;
  }
}

async function withFallbackRefreshLease(callback) {
  // navigator.locks is preferred. This short lease is a compatibility fallback for browsers
  // without Web Locks; the randomized backoff and ownership re-check reduce localStorage races.
  const owner = crypto.randomUUID();
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    let current = null;
    try { current = JSON.parse(localStorage.getItem(REFRESH_LEASE_KEY) || "null"); }
    catch (_) { current = null; }
    if (!current || current.expiresAt <= Date.now()) {
      await sleep(25 + Math.floor(Math.random() * 75));
      try {
        current = JSON.parse(localStorage.getItem(REFRESH_LEASE_KEY) || "null");
        if (!current || current.expiresAt <= Date.now()) {
          localStorage.setItem(REFRESH_LEASE_KEY, JSON.stringify({ owner, expiresAt: Date.now() + 12000 }));
          await sleep(40);
          const claimed = JSON.parse(localStorage.getItem(REFRESH_LEASE_KEY) || "null");
          if (claimed && claimed.owner === owner) {
            try { return await callback(); }
            finally {
              const latest = JSON.parse(localStorage.getItem(REFRESH_LEASE_KEY) || "null");
              if (latest && latest.owner === owner) localStorage.removeItem(REFRESH_LEASE_KEY);
            }
          }
        }
      } catch (_) {
        // Storage can be disabled by browser policy. Fall through to a local refresh attempt.
        return callback();
      }
    }
    await sleep(80 + Math.floor(Math.random() * 80));
  }
  // Do not wait forever if another tab crashed while holding a lease.
  return sessionIsAlreadyFresh();
}

async function refreshAcrossTabs() {
  const runUnderLock = async () => {
    // Another tab may have completed the refresh while this tab waited for the lock.
    if (await sessionIsAlreadyFresh()) return true;
    return performRefresh();
  };
  if (navigator.locks && typeof navigator.locks.request === "function") {
    return navigator.locks.request(REFRESH_LOCK_NAME, { mode: "exclusive" }, runUnderLock);
  }
  return withFallbackRefreshLease(runUnderLock);
}

function refreshSession() {
  if (!refreshPromise) {
    refreshPromise = refreshAcrossTabs().finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  const cacheable = method === "GET" && !path.startsWith("/auth/");
  if (cacheable) {
    const cached = readCache.get(path);
    if (cached && Date.now() - cached.updatedAt < READ_CACHE_TTL_MS) return cached.data;
  }
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  let pendingKey = null;
  if (financialWrite(path, method)) {
    pendingKey = idempotencyStorageKey(path, body);
    let key = sessionStorage.getItem(pendingKey);
    if (!key) {
      key = crypto.randomUUID();
      sessionStorage.setItem(pendingKey, key);
    }
    headers["Idempotency-Key"] = key;
  }
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && retry && !path.startsWith("/auth/")) {
    const refreshed = await refreshSession();
    if (refreshed) return api(path, { method, body, retry: false });
    showLogin();
    throw new Error("Session expired. Please sign in again.");
  }
  if (!res.ok) {
    if (pendingKey && res.status < 500) sessionStorage.removeItem(pendingKey);
    throw new Error(await errorMessage(res));
  }
  const data = await res.json();
  if (pendingKey) sessionStorage.removeItem(pendingKey);
  if (method === "GET") {
    if (cacheable) readCache.set(path, { data, updatedAt: Date.now() });
  } else {
    invalidateViewData();
    if (!path.startsWith("/auth/")) void refreshProof(true);
  }
  return data;
}

/* ---------- dialogs ---------- */
function askDialog(title, fields, submitLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { "aria-label": title });
    const inputs = {};
    const form = h("form", { method: "dialog" }, h("h3", { text: title }));
    for (const f of fields) {
      const id = `dlg-${f.name}`;
      const input = h("input", {
        id, name: f.name, type: f.type || "text", value: f.value ?? "",
        step: f.step, min: f.min, required: f.required !== false, placeholder: f.placeholder,
      });
      inputs[f.name] = input;
      form.append(h("label", { for: id, text: f.label }), input);
    }
    const cancel = h("button", { type: "button", text: "Cancel" });
    const ok = h("button", { type: "submit", class: danger ? "danger" : "primary", text: submitLabel });
    form.append(h("div", { class: "buttons" }, cancel, ok));
    dialog.append(form);
    document.body.append(dialog);
    const finish = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    cancel.addEventListener("click", () => finish(null));
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(null); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const values = {};
      for (const [name, input] of Object.entries(inputs)) values[name] = input.value.trim();
      finish(values);
    });
    dialog.showModal();
    Object.values(inputs)[0]?.focus();
  });
}

/* ---------- auth ---------- */
function showLogin() {
  state.user = null;
  state.navigationId += 1;
  viewPrefetchGeneration += 1;
  viewPrefetchPromise = null;
  viewCache.clear();
  readCache.clear();
  $("#view").replaceChildren();
  $("#view").setAttribute("aria-busy", "false");
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form").hidden = false;
  $("#register-form").hidden = true;
  $("#login-password").value = "";
}

$("#show-register").addEventListener("click", () => {
  $("#login-form").hidden = true;
  $("#register-form").hidden = false;
  $("#register-error").textContent = "";
});

$("#show-login").addEventListener("click", () => showLogin());

$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#register-error").textContent = "";
  try {
    const data = await api("/auth/register", {
      method: "POST",
      body: {
        company_name: $("#register-company").value.trim(),
        full_name: $("#register-name").value.trim(),
        email: $("#register-email").value.trim(),
        password: $("#register-password").value,
      },
    });
    state.user = data.user;
    await showShell();
  } catch (err) {
    $("#register-error").textContent = err.message;
  }
});

async function ensureServerSettings() {
  const serverSettings = await api("/settings");
  if (
    !serverSettings
    || typeof serverSettings.base_currency !== "string"
    || !/^[A-Z]{3}$/.test(serverSettings.base_currency)
  ) {
    throw new Error("The server returned an invalid base-currency setting.");
  }
  BASE_CURRENCY = serverSettings.base_currency;
}

async function showShell() {
  await ensureServerSettings();
  $("#login").hidden = true;
  $("#shell").hidden = false;
  const name = state.user.full_name.trim();
  $("#account-label").textContent = name;
  $("#account-avatar").textContent = name.split(/\s+/).map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  $("#whoami").textContent = `${state.user.role} account`;
  $("#role-banner").hidden = state.user.role !== "VIEWER";
  buildNav();
  go(state.view);
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const data = await api("/auth/login", {
      method: "POST",
      body: { email: $("#login-email").value.trim(), password: $("#login-password").value },
    });
    state.user = data.user;
    showShell();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  showLogin();
});

/* ---------- navigation ---------- */
const canWrite = () => ["ACCOUNTANT", "CFO"].includes(state.user.role);
const isCfo = () => state.user.role === "CFO";

const VIEWS = [
  ["dashboard", "Dashboard", () => true],
  ["journal", "Journal", () => true],
  ["invoices", "Invoices", () => true],
  ["accounts", "Accounts", () => true],
  ["reports", "Reports", () => true],
  ["periods", "Periods", isCfo],
  ["audit", "Audit trail", isCfo],
  ["users", "Users", isCfo],
];

const NAV_GROUPS = [
  { label: "Workspace", ids: ["dashboard", "journal", "invoices", "accounts", "reports"] },
  { label: "Admin", ids: ["periods", "audit", "users"] },
];
const NAV_GLYPHS = {
  dashboard: "M3 3h8v8H3z M13 3h8v5h-8z M13 10h8v11h-8z M3 13h8v8H3z",
  journal: "M5 3h14v18H5z M8 7h8 M8 11h8 M8 15h5",
  invoices: "M6 3h9l4 4v14H6z M15 3v5h4 M9 12h7 M9 16h7",
  accounts: "M3 9h18L12 4 3 9z M5 10v8 M9 10v8 M15 10v8 M19 10v8 M3 19h18",
  reports: "M4 20V11 M10 20V5 M16 20v-8 M22 20H2",
  periods: "M4 5h16v16H4z M8 3v4 M16 3v4 M4 10h16",
  audit: "M12 3 4 6v5c0 5 3.4 8.2 8 10 4.6-1.8 8-5 8-10V6z M8 12l2.5 2.5L16 9",
  users: "M16 20v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20 M9.5 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z M17 4.5a3.5 3.5 0 0 1 0 6.8 M17 14.5a4 4 0 0 1 3 3.8V20",
};

function buildNav() {
  const nav = $("#nav");
  const allowed = new Map(VIEWS.filter(([, , canSee]) => canSee()).map(([id, label]) => [id, label]));
  const groups = NAV_GROUPS.map((group) => {
    const items = group.ids.filter((id) => allowed.has(id));
    if (!items.length) return null;
    return h("section", { class: "nav-group", "aria-label": group.label },
      h("div", { class: "nav-heading", text: group.label }),
      ...items.map((id) => h("button", {
        type: "button", "data-view": id, "aria-label": allowed.get(id),
        onclick: () => { setMobileNav(false); go(id); },
      }, h("svg", { viewBox: "0 0 24 24", class: "nav-icon", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
        h("path", { d: NAV_GLYPHS[id] })), h("span", { text: allowed.get(id) }))));
  }).filter(Boolean);
  nav.replaceChildren(...groups);
}

function setMobileNav(open) {
  const rail = document.querySelector(".rail");
  rail.classList.toggle("mobile-open", open);
  $("#nav-overlay").hidden = !open;
  $("#mobile-menu").setAttribute("aria-expanded", String(open));
}

$("#mobile-menu").addEventListener("click", () => setMobileNav(true));
$("#nav-overlay").addEventListener("click", () => setMobileNav(false));
$("#refresh-view").addEventListener("click", () => go(state.view, true));
$("#command-button").addEventListener("click", openCommandPalette);
window.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openCommandPalette();
  }
  if (event.key === "Escape") setMobileNav(false);
});

function openCommandPalette() {
  let dialog = document.querySelector("#quick-search");
  if (!dialog) {
    dialog = h("dialog", { id: "quick-search", class: "quick-search", "aria-label": "Search pages" });
    const input = h("input", { type: "search", placeholder: "Search pages…", "aria-label": "Search pages" });
    const results = h("div", { class: "quick-search-results" });
    const render = () => {
      const query = input.value.trim().toLowerCase();
      const pages = VIEWS.filter(([, , allowed]) => allowed()).filter(([, label]) => label.toLowerCase().includes(query));
      results.replaceChildren(...pages.map(([id, label]) => h("button", {
        type: "button", class: "quick-search-item", onclick: () => { dialog.close(); go(id); },
      }, label)));
      if (!pages.length) results.replaceChildren(h("p", { class: "muted", text: "No matching pages." }));
    };
    input.addEventListener("input", render);
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
    dialog.append(h("div", { class: "quick-search-heading", text: "Jump to a page" }), input, results);
    document.body.append(dialog);
    render();
    dialog._searchInput = input;
  }
  if (!dialog.open) dialog.showModal();
  dialog._searchInput.value = "";
  dialog._searchInput.dispatchEvent(new Event("input"));
  dialog._searchInput.focus();
}

function hasUnsubmittedChanges(root) {
  return [...root.querySelectorAll("input, select, textarea")].some((field) => {
    if (field instanceof HTMLInputElement && ["checkbox", "radio"].includes(field.type)) {
      return field.checked !== field.defaultChecked;
    }
    if (field instanceof HTMLSelectElement) {
      return [...field.options].some((option) => option.selected !== option.defaultSelected);
    }
    return field.value !== field.defaultValue;
  });
}

async function go(view, forceRefresh = false) {
  state.view = view;
  const navigationId = ++state.navigationId;
  const selectedView = VIEWS.find(([id]) => id === view);
  $("#current-location").textContent = selectedView?.[1] ?? "Workspace";
  for (const b of document.querySelectorAll("#nav button")) {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }

  const root = $("#view");
  const cached = viewCache.get(view);
  if (cached) {
    // Keep the last rendered section on screen while fresh data loads.
    root.replaceChildren(cached.node);
    if (typeof cached.node.activate === "function") cached.node.activate();
    root.setAttribute("aria-busy", "false");
    if (!forceRefresh && Date.now() - cached.updatedAt < VIEW_CACHE_TTL_MS) {
      refreshProof();
      return;
    }
  } else {
    root.setAttribute("aria-busy", "true");
    // Keep the current section visible while an uncached first visit is prepared.
    if (!root.firstElementChild) {
      root.replaceChildren(h("div", { class: "loading-skeletons", "aria-label": "Loading page" }, ...Array.from({ length: 3 }, () => h("div", { class: "loading-skeleton" }))));
    }
  }

  try {
    const content = await RENDERERS[view]();
    if (navigationId !== state.navigationId) return;
    if (cached && hasUnsubmittedChanges(root)) {
      // Never discard a partially completed form while refreshing stale data.
      viewCache.set(view, { node: root.firstElementChild, updatedAt: Date.now() });
    } else {
      root.replaceChildren(content);
      viewCache.set(view, { node: content, updatedAt: Date.now() });
      if (typeof content.activate === "function") content.activate();
      root.focus();
    }
  } catch (err) {
    if (navigationId !== state.navigationId) return;
    if (!cached) root.replaceChildren(h("p", { class: "error", text: err.message }));
    else toast("Showing the last loaded data; refresh failed.", "bad");
  } finally {
    if (navigationId === state.navigationId) {
      root.setAttribute("aria-busy", "false");
      $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    }
  }
  refreshProof();
  if (state.user && view === "dashboard") void prefetchViews();
}

async function prefetchViews() {
  const userId = state.user?.id;
  if (!userId) return;
  if (viewPrefetchPromise) return viewPrefetchPromise;

  const generation = ++viewPrefetchGeneration;
  const dataVersion = state.dataVersion;
  const staging = h("div", { hidden: true, "aria-hidden": "true" });
  document.body.append(staging);

  const allowedViews = new Set(VIEWS.filter(([, , allowed]) => allowed()).map(([view]) => view));
  const views = Object.keys(RENDERERS).filter((view) =>
    view !== state.view && allowedViews.has(view));
  const jobs = views.map(async (view) => {
    if (viewCache.has(view)) return;
    try {
      const content = await RENDERERS[view]();
      if (generation !== viewPrefetchGeneration ||
          state.user?.id !== userId ||
          state.dataVersion !== dataVersion ||
          viewCache.has(view)) return;
      staging.append(content);
      viewCache.set(view, { node: content, updatedAt: Date.now() });
    } catch (_) {
      // Preloading is opportunistic; the regular navigation path handles failures.
    }
  });

  const run = (async () => {
    await Promise.allSettled(jobs);
    if (generation === viewPrefetchGeneration && state.user?.id === userId) {
      try {
        await api(`/reports/trial-balance?as_of=${isoToday()}`);
      } catch (_) {
        // The report will load normally when the user opens it.
      }
    }
    staging.remove();
  })();
  viewPrefetchPromise = run;
  try {
    await run;
  } finally {
    if (generation === viewPrefetchGeneration) viewPrefetchPromise = null;
  }
}

let proofUpdatedAt = 0;
let proofRefreshPromise = null;

async function refreshProof(force = false) {
  if (!force && Date.now() - proofUpdatedAt < 15_000) return;
  if (proofRefreshPromise) return proofRefreshPromise;
  proofRefreshPromise = (async () => {
    try {
      const tb = await api("/reports/trial-balance");
      $("#proof-dr").textContent = fmt(tb.total_debit);
      $("#proof-cr").textContent = fmt(tb.total_credit);
      const state_ = $("#proof-state");
      state_.replaceChildren(document.createTextNode(tb.is_balanced ? "Balanced" : "OUT OF BALANCE"));
      state_.className = tb.is_balanced ? "proof-state" : "proof-state bad";
      if (!tb.is_balanced) state_.append(h("button", { type: "button", class: "proof-review", text: "Review issue", onclick: () => go("reports") }));
    } catch (_) {
      /* The proof is advisory; retry after the short backoff. */
    } finally {
      proofUpdatedAt = Date.now();
      proofRefreshPromise = null;
    }
  })();
  return proofRefreshPromise;
}

async function loadAccounts() {
  const data = await api("/accounts?page_size=500");
  state.accounts = data.items;
  return data.items;
}

/* ---------- shared UI pieces ---------- */
function pager(data, onPage) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  return h("div", { class: "pager" },
    h("button", { type: "button", class: "small", text: "Previous", disabled: data.page <= 1, onclick: () => onPage(data.page - 1) }),
    h("span", { class: "muted", text: `Page ${data.page} of ${pages} · ${data.total} total` }),
    h("button", { type: "button", class: "small", text: "Next", disabled: data.page >= pages, onclick: () => onPage(data.page + 1) }));
}

const chip = (value) => h("span", { class: `chip ${value}`, text: value.replaceAll("_", " ") });
const th = (text, num = false) => h("th", { class: num ? "num" : "", text });
const td = (content, num = false) => h("td", { class: num ? "num" : "" }, content);
const tableOf = (headers, rows) =>
  h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, headers)), h("tbody", {}, rows)));

async function act(promise, success) {
  try {
    const result = await promise;
    if (success) toast(success);
    return result;
  } catch (err) {
    toast(err.message, "bad");
    return null;
  }
}

/* ---------- dashboard ---------- */
function barChart(data) {
  const hasActivity = data.some((row) => Number(row.revenue) !== 0 || Number(row.expenses) !== 0);
  if (!hasActivity) {
    return h("div", { class: "chart-empty-state", role: "status" },
      h("strong", { text: "No transactions yet" }),
      h("p", { class: "muted", text: "Post your first journal entry to see revenue and expenses here." }),
      canWrite() ? h("button", { type: "button", class: "primary", text: "New journal entry", onclick: () => openView("journal", () => renderJournal(1, true)) }) : null);
  }
  const W = 640, H = 230, pad = { l: 86, r: 10, t: 10, b: 32 };
  const max = Math.max(1, ...data.flatMap((d) => [Number(d.revenue), Number(d.expenses)]));
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
  const svg = svgEl("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", width: "100%",
    "aria-label": "Revenue and expenses by month" });
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + plotH - (plotH * i) / 4;
    svg.append(svgEl("line", { class: "axis", x1: pad.l, x2: W - pad.r, y1: y, y2: y }));
    svg.append(svgEl("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end" }, `${BASE_CURRENCY} ${Math.round((max * i) / 4).toLocaleString("en-US")}`));
  }
  const group = plotW / data.length, bar = Math.min(26, group / 3);
  data.forEach((d, i) => {
    const x = pad.l + group * i + group / 2;
    [["rev", Number(d.revenue), -bar], ["exp", Number(d.expenses), 2]].forEach(([cls, value, dx]) => {
      const height = (value / max) * plotH;
      svg.append(svgEl("rect", { class: `bar-${cls}`, x: x + dx, y: pad.t + plotH - height, width: bar, height }));
    });
    const periodDate = new Date(`${d.period}-01T12:00:00`);
    const periodLabel = Number.isNaN(periodDate.getTime()) ? d.period : periodDate.toLocaleDateString("en-US", { month: "short", year: "numeric" });
    svg.append(svgEl("text", { class: "chart-axis-label", x, y: H - 8, "text-anchor": "middle" }, periodLabel));
  });
  return svg;
}

async function renderDashboard() {
  const [s, period, accounts, invoices, entries] = await Promise.all([
    api("/dashboard/summary"),
    api("/accounting-period"),
    api("/accounts?page=1&page_size=1"),
    api("/invoices?page=1&page_size=1"),
    api("/journal-entries?page=1&page_size=1"),
  ]);
  const formatBase = (value) => `${BASE_CURRENCY} ${fmt(value)}`;
  const periodDate = new Date(`${period.period}-01T12:00:00`);
  const periodLabel = Number.isNaN(periodDate.getTime()) ? period.period : periodDate.toLocaleDateString("en-US", { month: "short", year: "numeric" });
  const kpi = (label, value, detail, glyph, tone = "") =>
    h("article", { class: `kpi ${tone}` },
      h("div", { class: "kpi-top" }, h("div", { class: "label", text: label }), h("span", { class: "kpi-icon", "aria-hidden": "true" }, glyph)),
      h("div", { class: "value tabular-nums", text: value }),
      h("p", { class: "kpi-detail", text: detail }));
  const steps = [
    { title: "Chart of accounts", detail: "Review the accounts available for posting.", complete: accounts.total > 0, action: "Open accounts", onClick: () => go("accounts") },
    { title: "First invoice", detail: "Create and issue the first customer invoice.", complete: invoices.total > 0, action: "New invoice", onClick: () => openView("invoices", () => renderInvoices(1, "", true)) },
    { title: "First journal entry", detail: "Post a balanced debit-and-credit entry.", complete: entries.total > 0, action: "New journal entry", onClick: () => openView("journal", () => renderJournal(1, true)) },
  ];
  const completed = steps.filter((step) => step.complete).length;
  return h("div", {},
    h("div", { class: "head dashboard-head" },
      h("div", {}, h("h2", { text: "Dashboard" }),
        h("div", { class: "dashboard-context" },
          h("span", { class: "context-pill", text: `Period: ${periodLabel} · ${period.status === "OPEN" ? "Open" : "Closed"}` }),
          h("span", { class: "context-pill", text: `Base currency: ${BASE_CURRENCY}` }))),
      h("div", { class: "actions" },
        canWrite() ? h("button", { type: "button", class: "secondary", text: "New invoice", onclick: () => openView("invoices", () => renderInvoices(1, "", true)) }) : null,
        canWrite() ? h("button", { type: "button", class: "primary", text: "New journal entry", onclick: () => openView("journal", () => renderJournal(1, true)) }) : null)),
    completed < steps.length ? h("section", { class: "panel onboarding", "aria-labelledby": "setup-title" },
      h("div", { class: "onboarding-head" }, h("div", {}, h("span", { class: "eyebrow", text: "Getting started" }), h("h3", { id: "setup-title", text: "Set up your accounting workspace" })), h("span", { class: "onboarding-progress", text: `${completed} of 3 complete` })),
      h("ol", { class: "setup-steps" }, ...steps.map((step, index) => h("li", { class: step.complete ? "setup-step complete" : "setup-step" },
        h("span", { class: "setup-number", text: step.complete ? "✓" : String(index + 1), "aria-label": step.complete ? "Complete" : `Step ${index + 1}` }),
        h("div", { class: "setup-copy" }, h("strong", { text: step.title }), h("p", { class: "muted", text: step.detail }),
          step.complete ? h("span", { class: "step-complete", text: "Completed" }) : canWrite() ? h("button", { type: "button", class: "text-action", text: step.action, onclick: step.onClick }) : h("span", { class: "muted step-restricted", text: "Requires Accountant or CFO access" })))))) : null,
    h("div", { class: "kpis" },
      kpi("Cash", formatBase(s.cash), "Bank + cash accounts", "¤"),
      kpi("Receivables", formatBase(s.accounts_receivable), "Unpaid customer balances", "↗"),
      kpi("Net income", formatBase(s.net_income), "Revenue less expenses", "∑"),
      kpi("Overdue invoices", String(s.overdue_invoices), "Invoices past their due date", "!", Number(s.overdue_invoices) > 0 ? "warning" : "")),
    h("div", { class: "panel" },
      h("h3", { text: "Revenue and expenses, last 6 months" }),
      barChart(s.monthly),
      h("div", { class: "legend" },
        h("span", {}, h("i", { class: "swatch rev" }), "Revenue"),
        h("span", {}, h("i", { class: "swatch exp" }), "Expenses"))),
    h("p", { class: s.balance_sheet_balanced ? "ledger-summary" : "ledger-summary bad", text: s.balance_sheet_balanced
      ? "Accounting equation holds: assets = liabilities + equity."
      : "Accounting equation does NOT hold. Review the trial balance before posting more entries." }));
}

/* ---------- journal ---------- */
async function openView(view, renderer) {
  state.view = view;
  const navigationId = ++state.navigationId;
  const selectedView = VIEWS.find(([id]) => id === view);
  $("#current-location").textContent = selectedView?.[1] ?? "Workspace";
  for (const button of document.querySelectorAll("#nav button")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  setMobileNav(false);
  const root = $("#view");
  root.setAttribute("aria-busy", "true");
  try {
    const content = await renderer();
    if (navigationId !== state.navigationId) return;
    root.replaceChildren(content);
    viewCache.set(view, { node: content, updatedAt: Date.now() });
    if (typeof content.activate === "function") content.activate();
    root.focus();
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  } catch (err) {
    if (navigationId === state.navigationId) toast(err.message, "bad");
  } finally {
    if (navigationId === state.navigationId) root.setAttribute("aria-busy", "false");
  }
  refreshProof();
}

const swap = async (promise) => {
  const navigationId = state.navigationId;
  const view = state.view;
  try {
    const content = await promise;
    if (navigationId !== state.navigationId || view !== state.view) return;
    $("#view").replaceChildren(content);
    viewCache.set(view, { node: content, updatedAt: Date.now() });
    if (typeof content.activate === "function") content.activate();
  } catch (err) {
    if (navigationId === state.navigationId && view === state.view) toast(err.message, "bad");
  }
  refreshProof();
};

function entryForm(onPosted) {
  const desc = h("input", { id: "je-desc", required: true, maxlength: 500, placeholder: "What happened?" });
  const date = h("input", { id: "je-date", type: "date", value: isoToday(), required: true });
  const linesBox = h("div");
  const totals = h("div", { class: "balance-line" });
  const post = h("button", { type: "submit", class: "primary", text: "Post entry", disabled: true });
  const rows = [];
  const cents = (input) => Math.round((Number(input.value) || 0) * 100);

  function recalc() {
    const dr = rows.reduce((a, r) => a + cents(r.dr), 0);
    const cr = rows.reduce((a, r) => a + cents(r.cr), 0);
    const balanced = dr === cr && dr > 0;
    totals.replaceChildren(
      h("span", { text: `Debits ${fmt(dr / 100)}` }),
      h("span", { text: `Credits ${fmt(cr / 100)}` }),
      h("span", { class: `chip ${balanced ? "ok" : "bad"}`, text: balanced ? "Balanced" : `Difference ${fmt(Math.abs(dr - cr) / 100)}` }));
    post.disabled = !balanced;
  }

  function addLine() {
    const sel = h("select", { "aria-label": "Account", required: true },
      h("option", { value: "", text: "Select account…" }),
      state.accounts.filter((a) => a.is_active).map((a) => h("option", { value: a.id, text: `${a.code} · ${a.name}` })));
    const dr = h("input", { type: "number", min: "0", step: "0.01", placeholder: "Debit", "aria-label": "Debit" });
    const cr = h("input", { type: "number", min: "0", step: "0.01", placeholder: "Credit", "aria-label": "Credit" });
    dr.addEventListener("input", () => { if (dr.value) cr.value = ""; recalc(); });
    cr.addEventListener("input", () => { if (cr.value) dr.value = ""; recalc(); });
    const row = { sel, dr, cr };
    const remove = h("button", { type: "button", class: "small", text: "Remove", onclick: () => {
      if (rows.length <= 2) return;
      rows.splice(rows.indexOf(row), 1); box.remove(); recalc();
    } });
    const box = h("div", { class: "line-row" }, sel, dr, cr, remove);
    rows.push(row);
    linesBox.append(box);
  }

  addLine(); addLine(); recalc();
  const form = h("form", { class: "panel" },
    h("h3", { text: "New journal entry" }),
    h("div", { class: "grid" },
      h("div", {}, h("label", { for: "je-desc", text: "Description" }), desc),
      h("div", {}, h("label", { for: "je-date", text: "Date" }), date)),
    h("label", { text: "Lines (each line is either a debit or a credit)" }),
    linesBox,
    h("button", { type: "button", class: "small", text: "Add line", onclick: () => { addLine(); recalc(); } }),
    totals, post);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const lines = rows.filter((r) => r.sel.value && (cents(r.dr) || cents(r.cr))).map((r) => ({
      account_id: Number(r.sel.value),
      debit: (cents(r.dr) / 100).toFixed(2),
      credit: (cents(r.cr) / 100).toFixed(2),
    }));
    const res = await act(api("/journal-entries", { method: "POST",
      body: { description: desc.value.trim(), entry_date: date.value, lines } }), "Entry posted");
    if (res) onPosted();
  });
  return form;
}

async function renderJournal(page = 1, showForm = false) {
  const [data] = await Promise.all([api(`/journal-entries?page=${page}&page_size=10`), loadAccounts()]);
  const root = h("div", {});
  const formSlot = h("div", {});
  const toggle = h("button", { type: "button", class: "primary", text: "New entry", onclick: () => {
    if (formSlot.firstChild) formSlot.replaceChildren();
    else formSlot.replaceChildren(entryForm(() => swap(renderJournal(1))));
  } });
  root.append(h("div", { class: "head" }, h("h2", { text: "Journal" }), canWrite() && toggle), formSlot);
  if (showForm && canWrite()) toggle.click();

  const rows = data.items.map((e) => h("tr", {},
    td(h("strong", { text: e.reference_no })),
    td(e.entry_date),
    td([e.description, e.reverses_entry_id ? h("div", { class: "muted", text: `Reverses entry #${e.reverses_entry_id}` }) : null]),
    td(h("div", { class: "lines" }, e.lines.map((l) => Number(l.debit) > 0
      ? h("div", { text: `${l.account_code} ${l.account_name}  ${fmt(l.debit)}` })
      : h("div", { class: "cr", text: `${l.account_code} ${l.account_name}  ${fmt(l.credit)}` })))),
    td([chip(e.source), e.is_reversed ? chip("VOID") : null]),
    td(isCfo() && !e.is_reversed && !e.reverses_entry_id && !["INVOICE", "PAYMENT"].includes(e.source)
      ? h("button", { type: "button", class: "small danger", text: "Reverse", onclick: async () => {
          const v = await askDialog(`Reverse ${e.reference_no}`, [{ name: "reason", label: "Reason", placeholder: "Why is this being reversed?" }], "Reverse entry", true);
          if (v && await act(api(`/journal-entries/${e.id}/reverse`, { method: "POST", body: { reason: v.reason } }), "Entry reversed")) swap(renderJournal(page));
        } })
      : "")));
  root.append(h("div", { class: "panel" },
    tableOf([th("Ref"), th("Date"), th("Description"), th("Lines"), th("Status"), th("")], rows),
    pager(data, (p) => swap(renderJournal(p)))));
  return root;
}

/* ---------- invoices ---------- */
function invoiceForm(onDone) {
  const f = (id, label, attrs) => h("div", {}, h("label", { for: id, text: label }), h("input", { id, ...attrs }));
  const due = new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);
  const form = h("form", { class: "panel" },
    h("h3", { text: "New invoice" }),
    h("div", { class: "grid" },
      f("inv-client", "Client name", { required: true, maxlength: 160 }),
      f("inv-email", "Client email (optional)", { type: "email" }),
      f("inv-ccy", "Currency", { value: BASE_CURRENCY, maxlength: 3, required: true }),
      f("inv-rate", `Rate to ${BASE_CURRENCY}`, { type: "number", step: "0.000001", min: "0.000001", value: "1" }),
      f("inv-amount", "Net amount", { type: "number", step: "0.01", min: "0.01", required: true }),
      f("inv-tax", "Tax %", { type: "number", step: "0.01", min: "0", max: "100", value: "0" }),
      f("inv-due", "Due date", { type: "date", value: due, required: true })),
    h("div", { class: "head" }, h("span", {}), h("button", { type: "submit", class: "primary", text: "Issue invoice" })));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const val = (id) => $(`#${id}`).value.trim();
    const body = {
      client_name: val("inv-client"), currency: val("inv-ccy").toUpperCase(), exchange_rate: val("inv-rate"),
      amount: Number(val("inv-amount")).toFixed(2), tax_rate: Number(val("inv-tax") || 0).toFixed(2), due_date: val("inv-due"),
    };
    if (val("inv-email")) body.client_email = val("inv-email");
    if (await act(api("/invoices", { method: "POST", body }), "Invoice issued")) onDone();
  });
  return form;
}

async function renderInvoices(page = 1, status = "", showForm = false) {
  const query = `/invoices?page=${page}&page_size=10${status ? `&status=${status}` : ""}`;
  const data = await api(query);
  const filter = h("select", { "aria-label": "Filter by status", onchange: (e) => swap(renderInvoices(1, e.target.value)) },
    ["", "ISSUED", "PARTIALLY_PAID", "PAID", "OVERDUE", "VOID"].map((s) =>
      h("option", { value: s, text: s ? s.replaceAll("_", " ") : "All statuses", selected: s === status })));
  const formSlot = h("div", {});
  const toggle = h("button", { type: "button", class: "primary", text: "New invoice", onclick: () => {
    if (formSlot.firstChild) formSlot.replaceChildren();
    else formSlot.replaceChildren(invoiceForm(() => swap(renderInvoices(1))));
  } });
  if (showForm && canWrite()) toggle.click();
  const rows = data.items.map((i) => h("tr", {},
    td(h("strong", { text: i.invoice_number })),
    td(i.client_name),
    td(i.currency),
    td(fmt(i.total_amount), true),
    td(fmt(i.balance_due), true),
    td(i.due_date),
    td(chip(i.status)),
    td(h("div", { class: "row-actions" },
      canWrite() && !["PAID", "VOID"].includes(i.status) ? h("button", { type: "button", class: "small", text: "Record payment", onclick: async () => {
        const fields = [{ name: "amount", label: `Amount (${i.currency})`, type: "number", step: "0.01", min: "0.01", value: i.balance_due }];
        if (i.currency !== BASE_CURRENCY) fields.push({ name: "payment_rate", label: `Rate to ${BASE_CURRENCY} on payment date`, type: "number", step: "0.000001", min: "0.000001", value: i.exchange_rate });
        const v = await askDialog(`Payment on ${i.invoice_number}`, fields, "Record payment");
        if (!v) return;
        const body = { amount: Number(v.amount).toFixed(2) };
        if (v.payment_rate) body.payment_rate = v.payment_rate;
        if (await act(api(`/invoices/${i.id}/payments`, { method: "POST", body }), "Payment recorded")) swap(renderInvoices(page, status));
      } }) : null,
      isCfo() && !["PAID", "VOID"].includes(i.status) && Number(i.amount_paid) === 0 ? h("button", { type: "button", class: "small danger", text: "Void", onclick: async () => {
        const v = await askDialog(`Void ${i.invoice_number}?`, [], "Void invoice", true);
        if (v && await act(api(`/invoices/${i.id}/void`, { method: "POST" }), "Invoice voided")) swap(renderInvoices(page, status));
      } }) : null))));
  return h("div", {},
    h("div", { class: "head" }, h("h2", { text: "Invoices" }), h("div", { class: "actions" }, filter, canWrite() && toggle)),
    formSlot,
    h("div", { class: "panel" },
      tableOf([th("Number"), th("Client"), th("Ccy"), th("Total", true), th("Balance due", true), th("Due"), th("Status"), th("")], rows),
      pager(data, (p) => swap(renderInvoices(p, status)))));
}

/* ---------- accounts ---------- */
async function renderAccounts() {
  const accounts = await loadAccounts();
  const rows = accounts.map((a) => h("tr", {}, td(a.code), td(a.name), td(chip(a.account_type)), td(fmt(a.balance), true)));
  const form = isCfo() ? h("form", { class: "panel" },
    h("h3", { text: "Add account" }),
    h("div", { class: "grid" },
      h("div", {}, h("label", { for: "acc-code", text: "Code (4 to 10 digits)" }), h("input", { id: "acc-code", pattern: "[0-9]{4,10}", required: true })),
      h("div", {}, h("label", { for: "acc-name", text: "Name" }), h("input", { id: "acc-name", required: true, maxlength: 120 })),
      h("div", {}, h("label", { for: "acc-type", text: "Type" }),
        h("select", { id: "acc-type" }, ["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"].map((t) => h("option", { value: t, text: t })))),
      h("button", { type: "submit", class: "primary", text: "Add account" }))) : null;
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { code: $("#acc-code").value.trim(), name: $("#acc-name").value.trim(), account_type: $("#acc-type").value };
    if (await act(api("/accounts", { method: "POST", body }), "Account created")) go("accounts");
  });
  return h("div", {}, h("div", { class: "head" }, h("h2", { text: "Chart of accounts" })), form,
    h("div", { class: "panel" }, tableOf([th("Code"), th("Name"), th("Type"), th("Balance", true)], rows)));
}

/* ---------- reports ---------- */
const reportTable = (headers, rows, totalRow) =>
  tableOf(headers, [...rows, totalRow]);
const amountRows = (items) => items.map((r) => h("tr", {}, td(r.code), td(r.name), td(fmt(r.amount), true)));
const totalRow = (label, value, span = 2) =>
  h("tr", { class: "total" }, h("td", { colspan: span, text: label }), td(fmt(value), true));

async function renderReports(tab = "trial") {
  const body = h("div", {});
  const controls = h("div", { class: "grid" });
  const today = isoToday();
  const monthStart = `${today.slice(0, 8)}01`;
  const input = (id, label, value) => h("div", {}, h("label", { for: id, text: label }), h("input", { id, type: "date", value }));
  if (tab === "income") controls.append(input("rep-from", "From", monthStart), input("rep-to", "To", today));
  else controls.append(input("rep-asof", "As of", today));

  const run = async () => {
    body.replaceChildren(h("p", { class: "muted", text: "Loading…" }));
    try {
      if (tab === "trial") {
        const asOf = $("#rep-asof").value;
        const r = await api(`/reports/trial-balance?as_of=${asOf}`);
        body.replaceChildren(
          h("a", { href: `/api/v1/reports/trial-balance/export?as_of=${asOf}`, text: "Download CSV" }),
          reportTable([th("Code"), th("Account"), th("Debit", true), th("Credit", true)],
            r.rows.map((x) => h("tr", {}, td(x.code), td(x.name), td(fmt(x.debit), true), td(fmt(x.credit), true))),
            h("tr", { class: "total" }, h("td", { colspan: 2, text: "Total" }), td(fmt(r.total_debit), true), td(fmt(r.total_credit), true))),
          h("p", { class: `chip ${r.is_balanced ? "ok" : "bad"}`, text: r.is_balanced ? "Debits equal credits" : "OUT OF BALANCE" }));
      } else if (tab === "income") {
        const r = await api(`/reports/income-statement?date_from=${$("#rep-from").value}&date_to=${$("#rep-to").value}`);
        body.replaceChildren(
          h("h3", { text: "Revenue" }), reportTable([th("Code"), th("Account"), th("Amount", true)], amountRows(r.revenue), totalRow("Total revenue", r.total_revenue)),
          h("h3", { text: "Expenses" }), reportTable([th("Code"), th("Account"), th("Amount", true)], amountRows(r.expenses), totalRow("Total expenses", r.total_expenses)),
          h("h3", { text: `Net income: ${fmt(r.net_income)}` }));
      } else {
        const r = await api(`/reports/balance-sheet?as_of=${$("#rep-asof").value}`);
        const section = (title, items, total, extra = []) => [
          h("h3", { text: title }),
          reportTable([th("Code"), th("Account"), th("Amount", true)], [...amountRows(items), ...extra], totalRow(`Total ${title.toLowerCase()}`, total))];
        body.replaceChildren(
          ...section("Assets", r.assets, r.total_assets),
          ...section("Liabilities", r.liabilities, r.total_liabilities),
          ...section("Equity", r.equity, r.total_equity,
            [h("tr", {}, td(""), td("Current earnings (unclosed)"), td(fmt(r.current_earnings), true))]),
          h("p", { class: `chip ${r.is_balanced ? "ok" : "bad"}`, text: r.is_balanced ? "Assets = Liabilities + Equity" : "OUT OF BALANCE" }));
      }
    } catch (err) { body.replaceChildren(h("p", { class: "error", text: err.message })); }
  };
  const tabBtn = (id, label) => h("button", { type: "button", text: label, "aria-pressed": String(tab === id), onclick: () => swap(renderReports(id)) });
  const root = h("div", {},
    h("div", { class: "head" }, h("h2", { text: "Reports" })),
    h("div", { class: "tabs" }, tabBtn("trial", "Trial balance"), tabBtn("income", "Income statement"), tabBtn("balance", "Balance sheet")),
    h("div", { class: "panel" }, controls, h("div", { class: "head" }, h("span", {}), h("button", { type: "button", class: "primary", text: "Run report", onclick: run })), body));
  let activated = false;
  root.activate = () => {
    if (activated) return;
    activated = true;
    void run();
  };
  return root;
}

/* ---------- periods, audit, users (CFO) ---------- */
async function renderPeriods() {
  const periods = await api("/periods");
  const toggleRow = (p) => h("tr", {}, td(p.period), td(chip(p.status)), td(p.closed_by || ""),
    td(h("button", { type: "button", class: "small", text: p.status === "CLOSED" ? "Reopen" : "Close",
      onclick: async () => { if (await act(api(`/periods/${p.period}/${p.status === "CLOSED" ? "reopen" : "close"}`, { method: "POST" }), "Period updated")) go("periods"); } })));
  const form = h("form", { class: "panel" },
    h("h3", { text: "Close a period" }),
    h("p", { class: "muted", text: "Closed periods reject new postings. Reopening is recorded in the audit trail." }),
    h("div", { class: "grid" },
      h("div", {}, h("label", { for: "per-month", text: "Month" }), h("input", { id: "per-month", type: "month", required: true, value: isoToday().slice(0, 7) })),
      h("button", { type: "submit", class: "primary", text: "Close period" })));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (await act(api(`/periods/${$("#per-month").value}/close`, { method: "POST" }), "Period closed")) go("periods");
  });
  return h("div", {}, h("div", { class: "head" }, h("h2", { text: "Accounting periods" })), form,
    h("div", { class: "panel" }, tableOf([th("Period"), th("Status"), th("By"), th("")], periods.map(toggleRow))));
}

async function renderAudit(page = 1) {
  const data = await api(`/audit-logs?page=${page}&page_size=20`);
  const result = h("p", { class: "muted", text: "Recompute the hash chain to prove no row was altered or removed." });
  const verify = h("button", { type: "button", class: "primary", text: "Verify integrity", onclick: async () => {
    const r = await act(api("/audit-logs/verify"));
    if (!r) return;
    result.className = `chip ${r.valid ? "ok" : "bad"}`;
    result.textContent = r.valid ? `Chain intact: ${r.checked} entries verified` : `Chain broken at entry #${r.first_invalid_seq}`;
  } });
  const rows = data.items.map((a) => h("tr", {}, td(String(a.seq)), td(new Date(a.created_at).toLocaleString()), td(a.actor), td(chip(a.action)), td(a.entity), td(a.details), td(a.row_hash.slice(0, 10))));
  return h("div", {}, h("div", { class: "head" }, h("h2", { text: "Audit trail" }), verify), result,
    h("div", { class: "panel" }, tableOf([th("#"), th("When"), th("Actor"), th("Action"), th("Entity"), th("Details"), th("Hash")], rows),
      pager(data, (p) => swap(renderAudit(p)))));
}

async function renderUsers() {
  const users = await api("/users");
  const rows = users.map((u) => {
    const self = u.id === state.user.id;
    const role = h("select", { "aria-label": `Role for ${u.email}`, disabled: self,
      onchange: async (e) => { if (!await act(api(`/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } }), "Role updated")) go("users"); } },
      ["VIEWER", "ACCOUNTANT", "CFO"].map((r) => h("option", { value: r, text: r, selected: r === u.role })));
    return h("tr", {}, td(u.full_name), td(u.email), td(role), td(h("span", { class: `chip ${u.is_active ? "ok" : "bad"}`, text: u.is_active ? "Active" : "Disabled" })),
      td(self ? "" : h("button", { type: "button", class: `small ${u.is_active ? "danger" : ""}`, text: u.is_active ? "Deactivate" : "Activate",
        onclick: async () => { if (await act(api(`/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } }), "User updated")) go("users"); } })));
  });
  const field = (id, label, attrs) => h("div", {}, h("label", { for: id, text: label }), h("input", { id, ...attrs }));
  const form = h("form", { class: "panel" }, h("h3", { text: "Add user" }),
    h("div", { class: "grid" },
      field("usr-name", "Full name", { required: true, minlength: 2 }),
      field("usr-email", "Email", { type: "email", required: true }),
      field("usr-pass", "Password (12+ characters)", { type: "password", required: true, minlength: 12, autocomplete: "new-password" }),
      h("div", {}, h("label", { for: "usr-role", text: "Role" }), h("select", { id: "usr-role" }, ["VIEWER", "ACCOUNTANT", "CFO"].map((r) => h("option", { value: r, text: r })))),
      h("button", { type: "submit", class: "primary", text: "Create user" })));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { full_name: $("#usr-name").value.trim(), email: $("#usr-email").value.trim(), password: $("#usr-pass").value, role: $("#usr-role").value };
    if (await act(api("/users", { method: "POST", body }), "User created")) go("users");
  });
  return h("div", {}, h("div", { class: "head" }, h("h2", { text: "Users" })), form,
    h("div", { class: "panel" }, tableOf([th("Name"), th("Email"), th("Role"), th("Active"), th("")], rows)));
}

const RENDERERS = {
  dashboard: renderDashboard,
  journal: () => renderJournal(),
  invoices: () => renderInvoices(),
  accounts: renderAccounts,
  reports: () => renderReports(),
  periods: renderPeriods,
  audit: () => renderAudit(),
  users: renderUsers,
};

/* ---------- boot ---------- */
(async function boot() {
  try {
    state.user = await api("/auth/me");
  } catch (_) {
    try {
      if (!(await refreshSession())) throw new Error("no session");
      if (!state.user) state.user = await api("/auth/me", { retry: false });
    } catch (_e) {
      showLogin();
      return;
    }
  }
  try {
    await showShell();
  } catch (error) {
    showLogin();
    $("#login-error").textContent = error instanceof Error ? error.message : "Could not load server settings.";
  }
})();
