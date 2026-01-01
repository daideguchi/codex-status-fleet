import json
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "/data/status.db")
REFRESHER_REFRESH_URL = os.getenv("REFRESHER_URL", "http://refresher:8090/refresh").rstrip("/")
REFRESHER_BASE_URL = os.getenv(
    "REFRESHER_BASE_URL", REFRESHER_REFRESH_URL.rsplit("/", 1)[0]
).rstrip("/")
REFRESH_TIMEOUT_SEC = float(os.getenv("REFRESH_TIMEOUT_SEC", "180"))

app = FastAPI(title="Codex Status Collector")

_db_lock = threading.Lock()

UI_HTML = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Codex Status Fleet</title>
    <style>
      :root { color-scheme: light dark; }
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji"; margin: 10px; }
      h1 { font-size: 16px; margin: 0 0 6px; }
      .meta { font-size: 12px; opacity: 0.8; margin-bottom: 8px; }
      .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
      button { padding: 5px 9px; border-radius: 8px; border: 1px solid #8884; background: #8882; cursor: pointer; }
      button:hover { background: #8883; }
      input, textarea, select { padding: 5px 9px; border-radius: 8px; border: 1px solid #8884; background: #8881; }
      textarea { width: 100%; min-height: 140px; resize: vertical; }
      table { width: 100%; border-collapse: collapse; font-size: 11px; line-height: 1.0; }
      th, td { text-align: left; padding: 2px 6px; border-bottom: 1px solid #8883; vertical-align: middle; white-space: nowrap; }
      th { position: sticky; top: 0; background: Canvas; z-index: 1; }
      th.sortable { cursor: pointer; user-select: none; }
      th.sortable:hover { background: #8881; }
      th.sortable[data-dir="asc"]::after { content: " ▲"; opacity: 0.65; font-size: 11px; }
      th.sortable[data-dir="desc"]::after { content: " ▼"; opacity: 0.65; font-size: 11px; }
      .pill { display: inline-block; padding: 1px 6px; border-radius: 999px; border: 1px solid #8884; font-size: 12px; }
      .ok { color: #0a7; border-color: #0a74; }
      .bad { color: #d55; border-color: #d554; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-variant-numeric: tabular-nums; }
      .muted { opacity: 0.8; }
      .nowrap { white-space: nowrap; }
      .right { text-align: right; }
      .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .modal { position: fixed; inset: 0; background: #0007; display: none; align-items: center; justify-content: center; padding: 16px; }
      .modal.open { display: flex; }
      .card { background: Canvas; border: 1px solid #8884; border-radius: 12px; padding: 12px; width: min(720px, 100%); }
      .card h2 { font-size: 14px; margin: 0 0 8px; }
      .small { font-size: 12px; opacity: 0.85; }
      .limits { display: inline-flex; flex-wrap: nowrap; gap: 8px; align-items: center; white-space: nowrap; }
      .limit { --pct: 0; --fill: #8886; width: 112px; display: inline-flex; justify-content: space-between; gap: 8px; align-items: center; padding: 0 7px; border-radius: 999px; border: 1px solid #8883; background: #8881; position: relative; overflow: hidden; white-space: nowrap; }
      .limit.more { width: auto; justify-content: flex-start; gap: 0; }
      .limit::before { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, var(--fill) calc(var(--pct) * 1%), transparent 0); opacity: 0.45; pointer-events: none; }
      .limit > * { position: relative; }
      .limit .name { opacity: 0.9; }
      .limit.ok { --fill: #0a7; border-color: #0a74; }
      .limit.warn { --fill: #d9a200; border-color: #d9a244; }
      .limit.bad { --fill: #d55; border-color: #d554; }
      .pct { font-weight: 800; }
      .pct.ok { color: #0a7; }
      .pct.warn { color: #d9a200; }
      .pct.bad { color: #d55; }
      .credit { display: inline-block; padding: 0 7px; border-radius: 999px; border: 1px solid #8883; background: #8881; }
      .credit.ok { color: #0a7; border-color: #0a74; }
      .credit.warn { color: #d9a200; border-color: #d9a244; }
      .credit.bad { color: #d55; border-color: #d554; }
      .tablebtn { padding: 1px 7px; border-radius: 6px; font-size: 12px; }
      .hide-resets .col-resets { display: none; }
      .hide-credits .col-credits { display: none; }
      tr.row-warn td { background: rgba(217, 162, 0, 0.08); }
      tr.row-bad td { background: rgba(213, 85, 85, 0.10); }
    </style>
  </head>
  <body>
    <h1>Codex Status Fleet</h1>
    <div class="meta">
      JSON: <a href="/latest">/latest</a> / <a href="/registry">/registry</a> / <a href="/healthz">/healthz</a>
    </div>
	    <div class="toolbar">
	      <button id="refresh">Update now</button>
	      <button id="add">Add accounts</button>
	      <button id="addKeys">Add Claude keys</button>
	      <button id="addFw">Add Fireworks keys</button>
	      <label class="muted">View
	        <select id="viewMode">
	          <option value="all" selected>All</option>
	          <option value="subscription">Subscription</option>
	          <option value="credits">Credits</option>
	        </select>
	      </label>
	      <button id="resetSort" title="Reset sorting to default order">Default order</button>
	      <label class="muted">Filter <input id="filter" placeholder="email / note / provider" /></label>
	      <span id="summary" class="muted"></span>
	      <span id="status" class="muted"></span>
	    </div>
	    <table>
	      <thead>
	        <tr>
	          <th class="sortable" data-sort="provider">Provider</th>
	          <th class="sortable" data-sort="account">Account / Note</th>
	          <th class="sortable" data-sort="plan">Plan / Model</th>
	          <th class="sortable" data-sort="limits">Limits</th>
	          <th class="sortable col-resets" data-sort="resets">Resets</th>
	          <th class="sortable col-credits" data-sort="credits">Credits</th>
	          <th class="sortable" data-sort="state">State</th>
	          <th>Action</th>
	        </tr>
	      </thead>
	      <tbody id="rows"></tbody>
	    </table>
    <div id="addModal" class="modal" role="dialog" aria-modal="true" aria-hidden="true">
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h2>Add accounts</h2>
          <button id="addClose">Close</button>
        </div>
        <div class="small">Paste emails (one per line) or paste /status output. Emails are extracted automatically.</div>
        <div style="height: 8px"></div>
        <textarea id="addText" placeholder="user@example.com&#10;another@example.com"></textarea>
        <div style="height: 8px"></div>
        <div class="row">
          <label class="muted">Plan
            <select id="addPlan">
              <option value="plus" selected>plus</option>
              <option value="pro">pro</option>
              <option value="team">team</option>
              <option value="enterprise">enterprise</option>
            </select>
          </label>
          <label class="muted"><input id="addEnabled" type="checkbox" checked /> enabled</label>
          <span id="addFound" class="small"></span>
        </div>
        <div style="height: 8px"></div>
        <pre id="addPreview" class="mono small" style="white-space: pre-wrap; margin: 0;"></pre>
        <div style="height: 10px"></div>
        <div class="row" style="justify-content: flex-end;">
          <button id="addCancel">Cancel</button>
          <button id="addSubmit">Add</button>
        </div>
      </div>
    </div>
    <div id="keysModal" class="modal" role="dialog" aria-modal="true" aria-hidden="true">
      <div class="card">
        <div class="row" style="justify-content: space-between;">
          <h2>Add Claude (Anthropic) keys</h2>
          <button id="keysClose">Close</button>
        </div>
        <div class="small">Paste Anthropic API keys (sk-ant-...). Keys are stored under accounts/&lt;label&gt;/.secrets/anthropic_api_key.txt.</div>
        <div style="height: 8px"></div>
        <textarea id="keysText" placeholder="sk-ant-..."></textarea>
        <div style="height: 8px"></div>
	        <div class="row">
	          <label class="muted">Label prefix <input id="keysPrefix" value="claude" /></label>
	          <label class="muted">Email <input id="keysEmail" placeholder="user@example.com" /></label>
	          <label class="muted">Model <input id="keysModel" placeholder="claude-3-5-haiku-latest" /></label>
	          <label class="muted">Note <input id="keysNote" placeholder="team /用途" /></label>
	          <label class="muted"><input id="keysEnabled" type="checkbox" checked /> enabled</label>
	          <span id="keysFound" class="small"></span>
	        </div>
        <div style="height: 8px"></div>
        <pre id="keysPreview" class="mono small" style="white-space: pre-wrap; margin: 0;"></pre>
        <div style="height: 10px"></div>
	        <div class="row" style="justify-content: flex-end;">
	          <button id="keysCancel">Cancel</button>
	          <button id="keysSubmit">Add</button>
	        </div>
	      </div>
	    </div>
	    <div id="fwModal" class="modal" role="dialog" aria-modal="true" aria-hidden="true">
	      <div class="card">
	        <div class="row" style="justify-content: space-between;">
	          <h2>Add Fireworks keys</h2>
	          <button id="fwClose">Close</button>
	        </div>
	        <div class="small">Paste Fireworks API keys (one per line). Keys are stored under accounts/&lt;label&gt;/.secrets/fireworks_api_key.txt.</div>
	        <div style="height: 8px"></div>
	        <textarea id="fwText" placeholder="YOUR_FIREWORKS_API_KEY"></textarea>
	        <div style="height: 8px"></div>
	        <div class="row">
	          <label class="muted">Label prefix <input id="fwPrefix" value="fireworks" /></label>
	          <label class="muted">Email <input id="fwEmail" placeholder="user@example.com" /></label>
	          <label class="muted">Model <input id="fwModel" placeholder="accounts/fireworks/models/llama-v3p1-8b-instruct" /></label>
	          <label class="muted">Base URL <input id="fwBaseUrl" placeholder="https://api.fireworks.ai/inference/v1" /></label>
	          <label class="muted">Note <input id="fwNote" placeholder="team /用途" /></label>
	          <label class="muted"><input id="fwEnabled" type="checkbox" checked /> enabled</label>
	          <span id="fwFound" class="small"></span>
	        </div>
	        <div style="height: 8px"></div>
	        <pre id="fwPreview" class="mono small" style="white-space: pre-wrap; margin: 0;"></pre>
	        <div style="height: 10px"></div>
	        <div class="row" style="justify-content: flex-end;">
	          <button id="fwCancel">Cancel</button>
	          <button id="fwSubmit">Add</button>
	        </div>
	      </div>
	    </div>
	    <div id="noteModal" class="modal" role="dialog" aria-modal="true" aria-hidden="true">
	      <div class="card">
	        <div class="row" style="justify-content: space-between;">
	          <h2>Edit note</h2>
	          <button id="noteClose">Close</button>
	        </div>
	        <div class="small">Append a short comment to this account's note (saved into accounts.json + registry).</div>
	        <div style="height: 8px"></div>
	        <div class="row">
	          <span class="muted">Target</span>
	          <span id="noteTarget" class="mono"></span>
	        </div>
	        <div style="height: 8px"></div>
	        <div class="row">
	          <label class="muted">Append <input id="noteAppend" placeholder="comment" /></label>
	          <label class="muted">Separator <input id="noteSep" value=" · " /></label>
	        </div>
	        <div style="height: 8px"></div>
	        <div class="small muted">Current</div>
	        <pre id="noteCurrent" class="mono small" style="white-space: pre-wrap; margin: 0;"></pre>
	        <div style="height: 10px"></div>
	        <div class="row" style="justify-content: flex-end;">
	          <button id="noteCancel">Cancel</button>
	          <button id="noteSave">Save</button>
	        </div>
	      </div>
	    </div>
	    <script>
	      const $ = (id) => document.getElementById(id);
      const rowsEl = $("rows");
      const statusEl = $("status");
	      const summaryEl = $("summary");
	      const refreshBtn = $("refresh");
	      const addBtn = $("add");
	      const addKeysBtn = $("addKeys");
	      const addFwBtn = $("addFw");
	      const viewModeEl = $("viewMode");
	      const resetSortBtn = $("resetSort");
	      const filterEl = $("filter");
      const addModal = $("addModal");
      const addClose = $("addClose");
      const addCancel = $("addCancel");
      const addSubmit = $("addSubmit");
      const addText = $("addText");
      const addPlan = $("addPlan");
      const addEnabled = $("addEnabled");
      const addFound = $("addFound");
      const addPreview = $("addPreview");
	      const keysModal = $("keysModal");
      const keysClose = $("keysClose");
      const keysCancel = $("keysCancel");
      const keysSubmit = $("keysSubmit");
		      const keysText = $("keysText");
		      const keysPrefix = $("keysPrefix");
		      const keysEmail = $("keysEmail");
		      const keysModel = $("keysModel");
		      const keysNote = $("keysNote");
		      const keysEnabled = $("keysEnabled");
		      const keysFound = $("keysFound");
		      const keysPreview = $("keysPreview");
		      const fwModal = $("fwModal");
		      const fwClose = $("fwClose");
		      const fwCancel = $("fwCancel");
		      const fwSubmit = $("fwSubmit");
		      const fwText = $("fwText");
		      const fwPrefix = $("fwPrefix");
		      const fwEmail = $("fwEmail");
		      const fwModel = $("fwModel");
		      const fwBaseUrl = $("fwBaseUrl");
		      const fwNote = $("fwNote");
		      const fwEnabled = $("fwEnabled");
		      const fwFound = $("fwFound");
		      const fwPreview = $("fwPreview");
		      const noteModal = $("noteModal");
		      const noteClose = $("noteClose");
		      const noteCancel = $("noteCancel");
		      const noteSave = $("noteSave");
		      const noteTarget = $("noteTarget");
		      const noteAppend = $("noteAppend");
		      const noteSep = $("noteSep");
		      const noteCurrent = $("noteCurrent");
	      let cachedItems = [];
	      let lastUpdateText = "";
	      let refreshing = false;
	      let viewMode = "all"; // all | subscription | credits
	      let noteLabel = "";
	      let sortKey = null;
	      let sortDir = "asc"; // asc | desc

	      const SORT_DEFAULT_DIR = {
	        provider: "asc",
	        account: "asc",
	        plan: "asc",
	        limits: "desc",
	        resets: "asc",
	        credits: "desc",
	        state: "desc",
	      };

	      function loadViewMode() {
	        try {
	          const v = localStorage.getItem("codex_status_fleet_view");
	          if (v === "all" || v === "subscription" || v === "credits") viewMode = v;
	        } catch {}
	      }

	      function saveViewMode() {
	        try {
	          localStorage.setItem("codex_status_fleet_view", viewMode);
	        } catch {}
	      }

	      function applyViewMode() {
	        try {
	          document.body.classList.remove("hide-resets", "hide-credits");
	          if (viewMode === "subscription") document.body.classList.add("hide-credits");
	          else if (viewMode === "credits") document.body.classList.add("hide-resets");
	          if (viewModeEl) viewModeEl.value = viewMode;
	        } catch {}
	      }

	      function loadSort() {
	        try {
	          const raw = localStorage.getItem("codex_status_fleet_sort");
	          if (!raw) return;
	          const obj = JSON.parse(raw);
	          const k = obj && obj.key ? String(obj.key) : null;
	          const d = obj && obj.dir ? String(obj.dir) : null;
	          if (k && Object.prototype.hasOwnProperty.call(SORT_DEFAULT_DIR, k)) sortKey = k;
	          if (d === "asc" || d === "desc") sortDir = d;
	        } catch {}
	      }

	      function saveSort() {
	        try {
	          localStorage.setItem("codex_status_fleet_sort", JSON.stringify({ key: sortKey, dir: sortDir }));
	        } catch {}
	      }

	      function setSort(key) {
	        const k = key ? String(key) : null;
	        if (!k) {
	          sortKey = null;
	          sortDir = "asc";
	          saveSort();
	          applySortIndicators();
	          renderFromCache();
	          return;
	        }

	        if (sortKey === k) {
	          sortDir = (sortDir === "asc") ? "desc" : "asc";
	        } else {
	          sortKey = k;
	          sortDir = SORT_DEFAULT_DIR[k] || "asc";
	        }
	        saveSort();
	        applySortIndicators();
	        renderFromCache();
	      }

	      function applySortIndicators() {
	        const ths = document.querySelectorAll("th.sortable");
	        for (const th of ths) {
	          const k = th.getAttribute("data-sort");
	          if (k && sortKey === k) {
	            th.setAttribute("data-dir", sortDir);
	            th.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
	          } else {
	            th.removeAttribute("data-dir");
	            th.removeAttribute("aria-sort");
	          }
	        }
	      }

      function fmtPct(p) {
        if (p === null || p === undefined) return "-";
        if (typeof p !== "number") return "-";
        return `${p}%`;
      }
      function clampPct(p) {
        if (p === null || p === undefined) return null;
        const n = Number(p);
        if (!Number.isFinite(n)) return null;
        return Math.max(0, Math.min(100, Math.round(n)));
      }
	      function pctClass(leftPercent) {
	        if (leftPercent === null || leftPercent === undefined) return "";
	        const p = Number(leftPercent);
	        if (!Number.isFinite(p)) return "";
        if (p <= 20) return "bad";
        if (p <= 60) return "warn";
        return "ok";
      }
      function fmtTs(iso) {
        if (!iso) return "-";
        try { return new Date(iso).toLocaleString(); } catch { return String(iso); }
      }
      function pad2(n) {
        return String(n).padStart(2, "0");
      }
      function fmtResetShort(iso) {
        if (!iso) return null;
        try {
          const d = new Date(iso);
          if (!Number.isFinite(d.getTime())) return null;
          const now = new Date();
          const sameDay =
            d.getFullYear() === now.getFullYear() &&
            d.getMonth() === now.getMonth() &&
            d.getDate() === now.getDate();
          const hhmm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
          if (sameDay) return hhmm;
          return `${d.getMonth() + 1}/${d.getDate()} ${hhmm}`;
        } catch {
          return null;
        }
      }
	      function safe(v, fallback="-") {
	        if (v === null || v === undefined || v === "") return fallback;
	        return String(v);
	      }

	      function _norm(item) {
	        const parsed = item && item.parsed ? item.parsed : {};
	        return (parsed && parsed.normalized) ? parsed.normalized : {};
	      }

	      function _reg(item) {
	        return (item && item.registry) ? item.registry : {};
	      }

	      function _lc(s) {
	        return String(s || "").trim().toLowerCase();
	      }

	      function _stateRank(item) {
	        const s = classify(item);
	        if (s === "ok") return 0;
	        if (s === "pending") return 1;
	        if (s === "disabled") return 2;
	        if (s === "auth_required") return 3;
	        if (s === "probe_error") return 4;
	        if (s === "no_parsed") return 4;
	        return 5;
	      }

	      function _windowLeft(item, key) {
	        const windows = (_norm(item).windows) || {};
	        const w = windows && windows[key] ? windows[key] : null;
	        if (!w || typeof w !== "object") return null;
	        let leftPct = clampPct(w.leftPercent);
	        if (leftPct === null) {
	          const used = clampPct(w.usedPercent);
	          if (used !== null) leftPct = clampPct(100 - used);
	        }
	        return leftPct;
	      }

	      function _windowResetEpoch(item, key) {
	        const windows = (_norm(item).windows) || {};
	        const w = windows && windows[key] ? windows[key] : null;
	        if (!w || typeof w !== "object") return null;
	        if (typeof w.resetsAt === "number" && Number.isFinite(w.resetsAt)) return w.resetsAt;
	        const iso = w.resetsAtIsoUtc;
	        if (!iso) return null;
	        const t = Date.parse(iso);
	        if (!Number.isFinite(t)) return null;
	        return Math.floor(t / 1000);
	      }

	      function _resetsEpoch(item) {
	        const a = _windowResetEpoch(item, "5h");
	        const b = _windowResetEpoch(item, "weekly");
	        const an = (typeof a === "number" && Number.isFinite(a)) ? a : null;
	        const bn = (typeof b === "number" && Number.isFinite(b)) ? b : null;
	        if (an === null && bn === null) return null;
	        if (an === null) return bn;
	        if (bn === null) return an;
	        return Math.min(an, bn);
	      }

	      function _accountKey(item) {
	        const n = _norm(item);
	        const r = _reg(item);
	        const email = _lc(n.account_email || n.expected_email || r.expected_email || "");
	        const note = _lc(r.note || "");
	        return (email || "") + "\\n" + (note || "");
	      }

	      function _providerKey(item) {
	        const n = _norm(item);
	        const r = _reg(item);
	        return _lc(r.provider || n.provider || "");
	      }

	      function _planKey(item) {
	        const parsed = item && item.parsed ? item.parsed : {};
	        const n = _norm(item);
	        const r = _reg(item);
	        const provider = _providerKey(item);
	        const isApiProvider = provider.startsWith("anthropic") || provider.startsWith("claude") || provider.startsWith("fireworks");
	        const plan = _lc(n.account_planType || n.rate_planType || n.expected_planType || r.expected_planType || "");
	        const model = _lc((parsed && parsed.model) || n.model || "");
	        return isApiProvider ? model : plan;
	      }

	      function _creditAmount(item) {
	        const provider = _providerKey(item);
	        if (!provider.startsWith("fireworks")) return null;
	        const n = _norm(item);
	        const c = (n && n.credits) ? n.credits : null;
	        if (!c || typeof c !== "object") return null;
	        if (typeof c.amount === "number" && Number.isFinite(c.amount)) return c.amount;
	        const raw = c.amount_raw;
	        if (!raw) return null;
	        const v = Number(raw);
	        if (!Number.isFinite(v)) return null;
	        return v;
	      }

	      function _labelKey(item) {
	        return _lc(item && item.account_label ? item.account_label : "");
	      }

	      function _cmp(a, b) { return a < b ? -1 : (a > b ? 1 : 0); }

	      function _cmpText(a, b, dir) {
	        const ax = _lc(a);
	        const bx = _lc(b);
	        const an = ax ? ax : null;
	        const bn = bx ? bx : null;
	        if (an === null && bn === null) return 0;
	        if (an === null) return 1;
	        if (bn === null) return -1;
	        const c = _cmp(an, bn);
	        return dir === "desc" ? -c : c;
	      }

	      function _cmpNum(a, b, dir) {
	        const an = (typeof a === "number" && Number.isFinite(a)) ? a : null;
	        const bn = (typeof b === "number" && Number.isFinite(b)) ? b : null;
	        if (an === null && bn === null) return 0;
	        if (an === null) return 1;
	        if (bn === null) return -1;
	        const c = _cmp(an, bn);
	        return dir === "desc" ? -c : c;
	      }

	      function _sortComparator(a, b) {
	        const dir = sortDir;
	        const key = sortKey;
	        if (!key) return 0;

	        if (key === "provider") {
	          const c = _cmpText(_providerKey(a), _providerKey(b), dir);
	          return c || _cmpText(_accountKey(a), _accountKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "account") {
	          const c = _cmpText(_accountKey(a), _accountKey(b), dir);
	          return c || _cmpText(_providerKey(a), _providerKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "plan") {
	          const c = _cmpText(_planKey(a), _planKey(b), dir);
	          return c || _cmpText(_accountKey(a), _accountKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "limits") {
	          const aw = _windowLeft(a, "weekly");
	          const bw = _windowLeft(b, "weekly");
	          const c1 = _cmpNum(aw, bw, dir);
	          if (c1) return c1;
	          const a5 = _windowLeft(a, "5h");
	          const b5 = _windowLeft(b, "5h");
	          const c2 = _cmpNum(a5, b5, dir);
	          return c2 || _cmpText(_accountKey(a), _accountKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "resets") {
	          const c = _cmpNum(_resetsEpoch(a), _resetsEpoch(b), dir);
	          return c || _cmpText(_accountKey(a), _accountKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "credits") {
	          const c = _cmpNum(_creditAmount(a), _creditAmount(b), dir);
	          return c || _cmpText(_accountKey(a), _accountKey(b), "asc") || _cmpText(_labelKey(a), _labelKey(b), "asc");
	        }
	        if (key === "state") {
	          const c = _cmpNum(_stateRank(a), _stateRank(b), dir);
	          return c || _cmpNum(_windowLeft(a, "weekly"), _windowLeft(b, "weekly"), "asc") || _cmpText(_accountKey(a), _accountKey(b), "asc");
	        }
	        return 0;
	      }
      function esc(v) {
        return String(v).replace(/[&<>"']/g, (c) => {
          if (c === "&") return "&amp;";
          if (c === "<") return "&lt;";
          if (c === ">") return "&gt;";
          if (c === '"') return "&quot;";
          if (c === "'") return "&#39;";
          return c;
        });
      }
      function pill(text, ok) {
        const cls = ok ? "pill ok" : "pill bad";
        return `<span class="${cls}">${esc(text)}</span>`;
      }

      function setModalOpen(open) {
        if (open) {
          addModal.classList.add("open");
          addModal.setAttribute("aria-hidden", "false");
          renderAddPreview();
          try { addText.focus(); } catch {}
        } else {
          addModal.classList.remove("open");
          addModal.setAttribute("aria-hidden", "true");
        }
      }

      function setKeysModalOpen(open) {
        if (open) {
          keysModal.classList.add("open");
          keysModal.setAttribute("aria-hidden", "false");
          renderKeysPreview();
          try { keysText.focus(); } catch {}
        } else {
          keysModal.classList.remove("open");
          keysModal.setAttribute("aria-hidden", "true");
        }
      }

      function setFwModalOpen(open) {
        if (open) {
          fwModal.classList.add("open");
          fwModal.setAttribute("aria-hidden", "false");
          renderFwPreview();
          try { fwText.focus(); } catch {}
        } else {
          fwModal.classList.remove("open");
          fwModal.setAttribute("aria-hidden", "true");
        }
      }

      function setNoteModalOpen(open) {
        if (open) {
          noteModal.classList.add("open");
          noteModal.setAttribute("aria-hidden", "false");
          try { noteAppend.focus(); } catch {}
        } else {
          noteModal.classList.remove("open");
          noteModal.setAttribute("aria-hidden", "true");
        }
      }

      function _findByLabel(label) {
        const items = Array.isArray(cachedItems) ? cachedItems : [];
        for (const it of items) {
          if (String(it && it.account_label ? it.account_label : "") === String(label || "")) return it;
        }
        return null;
      }

      function openNote(label) {
        noteLabel = String(label || "");
        const it = _findByLabel(noteLabel);
        const parsed = it ? (it.parsed || {}) : {};
        const norm = parsed.normalized || {};
        const reg = it ? (it.registry || {}) : {};
        const email = String(norm.account_email || norm.expected_email || reg.expected_email || "").trim();
        const note = String(reg.note || "").trim();

        if (noteTarget) noteTarget.textContent = email ? `${noteLabel} (${email})` : noteLabel;
        if (noteCurrent) noteCurrent.textContent = note || "";
        if (noteAppend) noteAppend.value = "";

        setModalOpen(false);
        setKeysModalOpen(false);
        setFwModalOpen(false);
        setNoteModalOpen(true);
      }

      async function saveNote() {
        if (!noteLabel) return;
        const text = (noteAppend.value || "").trim();
        if (!text) {
          statusEl.textContent = "Note: enter text to append";
          return;
        }

        noteSave.disabled = true;
        noteCancel.disabled = true;
        noteClose.disabled = true;
        statusEl.textContent = "Saving note...";
        try {
          const payload = {
            account_label: noteLabel,
            append: text,
            separator: (noteSep.value || "").toString(),
            replace: false,
          };
          const res = await fetch("/notes/append", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          let body = null;
          try { body = await res.json(); } catch {}
          if (!res.ok) {
            const msg = (body && (body.detail || body.error)) ? (body.detail || body.error) : `HTTP ${res.status}`;
            throw new Error(msg);
          }

          setNoteModalOpen(false);
          noteLabel = "";
          await loadLatest();
          lastUpdateText = `Note updated — ${new Date().toLocaleTimeString()}`;
          renderFromCache();
        } catch (e) {
          statusEl.textContent = `Note error: ${e}`;
        } finally {
          noteSave.disabled = false;
          noteCancel.disabled = false;
          noteClose.disabled = false;
        }
      }

      function extractEmails(text) {
        const re = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g;
        const m = (text || "").match(re) || [];
        const out = [];
        const seen = new Set();
        for (const raw of m) {
          const e = String(raw).trim().toLowerCase();
          if (!e || seen.has(e)) continue;
          seen.add(e);
          out.push(e);
        }
        return out;
      }

      function extractAnthropicKeys(text) {
        const re = /sk-ant-[A-Za-z0-9_-]+/g;
        const m = (text || "").match(re) || [];
        const out = [];
        const seen = new Set();
        for (const raw of m) {
          const k = String(raw).trim();
          if (!k || seen.has(k)) continue;
          seen.add(k);
          out.push(k);
        }
        return out;
      }

      function extractFireworksKeys(text) {
        // Fireworks key format varies; accept long token-like strings (no whitespace).
        const re = /[A-Za-z0-9_-]{20,}/g;
        const m = (text || "").match(re) || [];
        const out = [];
        const seen = new Set();
        for (const raw of m) {
          const k = String(raw).trim();
          if (!k || seen.has(k)) continue;
          seen.add(k);
          out.push(k);
        }
        return out;
      }

      function maskKey(k) {
        const s = String(k || "").trim();
        if (!s) return "";
        if (s.length <= 18) return s;
        return s.slice(0, 10) + "…" + s.slice(-6);
      }

      function renderAddPreview() {
        const emails = extractEmails(addText.value || "");
        addFound.textContent = emails.length ? `Found ${emails.length}` : "Found 0";
        addPreview.textContent = emails.slice(0, 200).join("\\n");
      }

      function renderKeysPreview() {
        const keys = extractAnthropicKeys(keysText.value || "");
        keysFound.textContent = keys.length ? `Found ${keys.length}` : "Found 0";
        keysPreview.textContent = keys.slice(0, 200).map(maskKey).join("\\n");
      }

      function renderFwPreview() {
        const keys = extractFireworksKeys(fwText.value || "");
        fwFound.textContent = keys.length ? `Found ${keys.length}` : "Found 0";
        fwPreview.textContent = keys.slice(0, 200).map(maskKey).join("\\n");
      }

      function classify(item) {
        const parsed = item.parsed || null;
        const norm = (parsed && parsed.normalized) ? parsed.normalized : {};
        const reg = item.registry || null;
        const lastUpdate = item.ts || "";

        if (reg && reg.enabled === false) return "disabled";
        if (!lastUpdate) return "pending";
        if (!parsed) return "no_parsed";
        const requiresAuth = (norm.requiresAuth === true) || (norm.requiresOpenaiAuth === true);
        if (requiresAuth) return "auth_required";
        if (parsed.probe_error) return "probe_error";
        return "ok";
      }

      function rowHtml(item) {
        const parsed = item.parsed || null;
        const norm = (parsed && parsed.normalized) ? parsed.normalized : {};
        const reg = item.registry || null;
        const windows = norm.windows || {};
        const label = safe(item.account_label);
        const detailsHref = `/latest/${encodeURIComponent(label)}`;

        const regProvider = reg ? (reg.provider || "") : "";
        const regEmail = reg ? (reg.expected_email || "") : "";
        const regPlan = reg ? (reg.expected_planType || "") : "";
        const regNote = reg ? (reg.note || "") : "";

        const email = norm.account_email || norm.expected_email || regEmail || "";
        const plan = norm.account_planType || norm.rate_planType || norm.expected_planType || regPlan || "";
        const model = (parsed && parsed.model) || norm.model || "";
        const lastUpdate = item.ts || "";

        const providerRaw = (regProvider || norm.provider || "").trim();
        const provider = providerRaw || "-";
        const providerLc = providerRaw.toLowerCase();

        const isApiProvider = providerLc.startsWith("anthropic") || providerLc.startsWith("claude") || providerLc.startsWith("fireworks");
        const planOrModel = isApiProvider ? safe(model) : safe(plan);

        let state = "";
        const cls = classify(item);
        if (cls === "disabled") state = pill("disabled", false);
        else if (cls === "pending") state = pill("pending", false);
        else if (cls === "no_parsed") state = pill("no parsed", false);
        else if (cls === "auth_required") state = pill("auth required", false);
        else if (cls === "probe_error") state = pill("probe_error", false);
        else state = pill("ok", true);

        const expectedMatch = norm.expected_email_match;
        let accountLineHtml = esc(safe(email, isApiProvider ? "(api key)" : "(unknown)"));
        if (!norm.account_email && (norm.expected_email || regEmail)) {
          accountLineHtml += " " + pill("expected", true);
        } else if (typeof expectedMatch === "boolean") {
          accountLineHtml += " " + (expectedMatch ? pill("email ok", true) : pill("email mismatch", false));
        }

        const accountTitleParts = [];
        if (label) accountTitleParts.push(label);
        if (email) accountTitleParts.push(email);
        if (regNote) accountTitleParts.push(regNote);
        const accountTitle = accountTitleParts.join(" · ");
        let accountHtml = `<a class="mono" href="${detailsHref}" title="${esc(accountTitle)}">${accountLineHtml}${regNote ? `<span class="muted"> · ${esc(regNote)}</span>` : ""}</a>`;

        const providerHtml = providerRaw ? `<span class="pill mono">${esc(providerRaw)}</span>` : "-";

        const creditClass = (amount) => {
          const n = (typeof amount === "number" && Number.isFinite(amount)) ? amount : null;
          if (n === null) return "";
          if (n <= 1) return "bad";
          if (n <= 5) return "warn";
          return "ok";
        };
        const credits = norm.credits || {};
        const creditAmt =
          (typeof credits.amount === "number" && Number.isFinite(credits.amount))
            ? credits.amount
            : (credits.amount_raw ? Number(credits.amount_raw) : null);
        const creditCur = (credits.currency || "USD").toUpperCase();
        let creditsHtml = "-";
        if (providerLc.startsWith("fireworks")) {
          if (typeof creditAmt === "number" && Number.isFinite(creditAmt)) {
            const cls = creditClass(creditAmt);
            const txt = `${creditCur} ${creditAmt.toFixed(2)}`;
            const title = `balance ${txt}` + (credits.source ? ` (via ${credits.source})` : "");
            creditsHtml = `<span class="credit ${cls} mono" title="${esc(title)}">${esc(txt)}</span>`;
          } else if (credits && credits.error) {
            creditsHtml = `<span class="mono muted" title="balance error">${esc(String(credits.error))}</span>`;
          }
        }

        const leftOf = (w) => {
          if (!w || typeof w !== "object") return null;
          let leftPct = clampPct(w.leftPercent);
          if (leftPct === null) {
            const used = clampPct(w.usedPercent);
            if (used !== null) leftPct = clampPct(100 - used);
          }
          return leftPct;
        };

        const limitBlocks = [];
        const shortName = (name) => {
          if (name === "weekly") return "week";
          if (name === "requests") return "req";
          if (name === "tokens") return "tok";
          return name;
        };
        const addLimitBlock = (name, w) => {
          if (!w || typeof w !== "object") return;

          let leftPct = clampPct(w.leftPercent);
          if (leftPct === null) {
            const used = clampPct(w.usedPercent);
            if (used !== null) leftPct = clampPct(100 - used);
          }
          const cls = pctClass(leftPct);
          const leftText = fmtPct(leftPct);
          const reset = fmtTs(w.resetsAtIsoUtc);

          const metaParts = [];
          if (w.remaining !== null && w.remaining !== undefined && w.limit !== null && w.limit !== undefined) {
            metaParts.push(`${w.remaining}/${w.limit}`);
          }
          if (reset !== "-" && reset !== "") metaParts.push(`resets ${reset}`);
          const title = `${name}: ${leftText}` + (metaParts.length ? ` (${metaParts.join(" · ")})` : "");

          const width = leftPct === null ? 0 : leftPct;
          limitBlocks.push(
            `<span class="limit ${cls}" style="--pct:${width};" title="${esc(title)}">` +
              `<span class="mono name">${esc(shortName(name))}</span>` +
              `<span class="mono pct ${cls}">${esc(leftText)}</span>` +
            `</span>`
          );
        };

        const maxChips = 2;
        const order = ["5h", "weekly", "requests", "tokens"];
        const keys = [];
        const seen = new Set();
        for (const k of order) {
          if (windows[k] && typeof windows[k] === "object") {
            keys.push(k);
            seen.add(k);
          }
        }
        for (const k of Object.keys(windows || {}).sort()) {
          if (seen.has(k)) continue;
          if (windows[k] && typeof windows[k] === "object") keys.push(k);
        }
        const displayKeys = keys.slice(0, maxChips);
        const extraKeys = keys.slice(maxChips);
        for (const k of displayKeys) addLimitBlock(k, windows[k]);
        if (extraKeys.length) {
          limitBlocks.push(`<span class="limit more" title="${esc("more: " + extraKeys.join(", "))}"><span class="mono name">+${extraKeys.length}</span></span>`);
        }
        const limitsHtml = limitBlocks.length ? `<span class="limits">${limitBlocks.join("")}</span>` : "-";

        const resetsParts = [];
        const resetsTitleParts = [];
        const r5 = windows["5h"] && windows["5h"].resetsAtIsoUtc;
        const rw = windows["weekly"] && windows["weekly"].resetsAtIsoUtc;
        if (r5) {
          const short = fmtResetShort(r5) || fmtTs(r5);
          resetsParts.push(`5h ${short}`);
          resetsTitleParts.push(`5h resets ${fmtTs(r5)}`);
        }
        if (rw) {
          const short = fmtResetShort(rw) || fmtTs(rw);
          resetsParts.push(`week ${short}`);
          resetsTitleParts.push(`weekly resets ${fmtTs(rw)}`);
        }
        const resetsText = resetsParts.join(" · ");
        const resetsTitle = resetsTitleParts.join(" | ");
        const resetsHtml = resetsText ? `<span class="mono nowrap" title="${esc(resetsTitle)}">${esc(resetsText)}</span>` : "-";

        const s = classify(item);
        const left5h = leftOf(windows["5h"]);
        const leftWeekly = leftOf(windows["weekly"]);
        let worst = null;
        if (typeof leftWeekly === "number") worst = leftWeekly;
        else if (typeof left5h === "number") worst = left5h;
        let rowCls = "";
        if (s === "auth_required" || s === "probe_error" || s === "no_parsed") rowCls = "row-bad";
        else if (s === "pending") rowCls = "row-warn";
        else {
          let c = pctClass(worst);
          if (!c && providerLc.startsWith("fireworks")) c = creditClass(creditAmt);
          if (c === "bad") rowCls = "row-bad";
          else if (c === "warn") rowCls = "row-warn";
        }

        return `
          <tr class="${rowCls}">
            <td>${providerHtml}</td>
            <td>${accountHtml}</td>
            <td class="mono">${esc(planOrModel)}</td>
            <td>${limitsHtml}</td>
            <td class="mono nowrap col-resets">${resetsHtml}</td>
            <td class="mono nowrap col-credits">${creditsHtml}</td>
            <td>${state}</td>
            <td>
              <button class="tablebtn" title="Update" aria-label="Update" data-action="update" data-label="${encodeURIComponent(safe(item.account_label))}">↻</button>
              <button class="tablebtn" title="Note" aria-label="Note" data-action="note" data-label="${encodeURIComponent(safe(item.account_label))}">✎</button>
            </td>
          </tr>
        `;
      }

	      function renderFromCache() {
	        const items = Array.isArray(cachedItems) ? cachedItems : [];

	        const groupOf = (it) => {
	          const parsed = it.parsed || {};
	          const norm = parsed.normalized || {};
	          const reg = it.registry || {};
	          const provider = String((reg.provider || norm.provider || "")).toLowerCase();
	          if (provider === "codex" || provider === "openai_codex" || provider === "openai") return "subscription";
	          if (provider.startsWith("anthropic") || provider.startsWith("claude") || provider.startsWith("fireworks")) return "credits";
	          return "other";
	        };
	        const inView = (viewMode === "subscription" || viewMode === "credits")
	          ? items.filter((it) => groupOf(it) === viewMode)
	          : items;

	        const q = (filterEl.value || "").trim().toLowerCase();
	        const filtered = q
	          ? inView.filter((it) => {
	              const parsed = it.parsed || {};
	              const norm = parsed.normalized || {};
	              const reg = it.registry || {};
              const email = (norm.account_email || norm.expected_email || reg.expected_email || "").toLowerCase();
              const provider = (reg.provider || norm.provider || "").toLowerCase();
              const note = (reg.note || "").toLowerCase();
              const model = (parsed.model || norm.model || "").toLowerCase();
              const label = (it.account_label || "").toLowerCase();
	              return (
	                label.includes(q) ||
	                email.includes(q) ||
	                provider.includes(q) ||
	                note.includes(q) ||
	                model.includes(q)
	              );
	            })
	          : inView;

	        const sorted = sortKey ? [...filtered].sort(_sortComparator) : filtered;
	        rowsEl.innerHTML = sorted.map(rowHtml).join("");
	        const counts = { ok: 0, auth_required: 0, pending: 0, disabled: 0, errors: 0, total: items.length };
	        for (const it of items) {
	          const s = classify(it);
          if (s === "ok") counts.ok += 1;
          else if (s === "auth_required") counts.auth_required += 1;
          else if (s === "pending") counts.pending += 1;
          else if (s === "disabled") counts.disabled += 1;
          else counts.errors += 1;
	        }
	        summaryEl.textContent = `total:${counts.total} ok:${counts.ok} auth:${counts.auth_required} pending:${counts.pending} err:${counts.errors} disabled:${counts.disabled}`;
	        const suffix = lastUpdateText ? ` — ${lastUpdateText}` : "";
	        statusEl.textContent = `${sorted.length}/${inView.length} rows${suffix}`;
	      }

      async function loadLatest() {
        const started = Date.now();
        statusEl.textContent = "Loading...";
        try {
          const res = await fetch("/latest", { cache: "no-store" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          const items = Array.isArray(data.items) ? data.items : [];
          const ms = Date.now() - started;
          cachedItems = items;
          lastUpdateText = `Updated ${new Date().toLocaleTimeString()} (${ms}ms)`;
          renderFromCache();
        } catch (e) {
          statusEl.textContent = `Error: ${e}`;
        }
      }

      async function syncRegistry() {
        try {
          await fetch("/registry/sync", { method: "POST" });
        } catch {}
      }

      async function addAccounts() {
        const emails = extractEmails(addText.value || "");
        if (!emails.length) {
          addFound.textContent = "Found 0 (paste emails first)";
          return;
        }

        addSubmit.disabled = true;
        addCancel.disabled = true;
        addClose.disabled = true;
        statusEl.textContent = "Adding accounts...";
        try {
          const payload = {
            text: addText.value,
            expected_planType: (addPlan.value || "").trim() || null,
            enabled: !!addEnabled.checked,
          };
          const res = await fetch("/accounts/add", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          let body = null;
          try { body = await res.json(); } catch {}
          if (!res.ok) {
            const msg = (body && (body.detail || body.error)) ? (body.detail || body.error) : `HTTP ${res.status}`;
            throw new Error(msg);
          }

          setModalOpen(false);
          addText.value = "";
          renderAddPreview();

          await loadLatest();
          const added = body && typeof body.added === "number" ? body.added : 0;
          const updated = body && typeof body.updated === "number" ? body.updated : 0;
          lastUpdateText = `Accounts updated — added:${added} updated:${updated} (login may be required)`;
          renderFromCache();
        } catch (e) {
          statusEl.textContent = `Add error: ${e}`;
        } finally {
          addSubmit.disabled = false;
          addCancel.disabled = false;
          addClose.disabled = false;
        }
      }

      async function addClaudeKeys() {
        const keys = extractAnthropicKeys(keysText.value || "");
        if (!keys.length) {
          keysFound.textContent = "Found 0 (paste sk-ant-... first)";
          return;
        }

        keysSubmit.disabled = true;
        keysCancel.disabled = true;
        keysClose.disabled = true;
        statusEl.textContent = "Adding Claude keys...";
        try {
          const payload = {
            text: keysText.value,
            enabled: !!keysEnabled.checked,
            label_prefix: (keysPrefix.value || "").trim() || null,
            expected_email: (keysEmail.value || "").trim() || null,
            note: (keysNote.value || "").trim() || null,
            anthropic_model: (keysModel.value || "").trim() || null,
          };
          const res = await fetch("/anthropic/add_keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          let body = null;
          try { body = await res.json(); } catch {}
          if (!res.ok) {
            const msg = (body && (body.detail || body.error)) ? (body.detail || body.error) : `HTTP ${res.status}`;
            throw new Error(msg);
          }

          setKeysModalOpen(false);
          keysText.value = "";
          keysEmail.value = "";
          keysNote.value = "";
          renderKeysPreview();

          await loadLatest();
          const added = body && typeof body.added === "number" ? body.added : 0;
          const updated = body && typeof body.updated === "number" ? body.updated : 0;
          lastUpdateText = `Claude keys updated — added:${added} updated:${updated}`;
          renderFromCache();
        } catch (e) {
          statusEl.textContent = `Add error: ${e}`;
        } finally {
          keysSubmit.disabled = false;
          keysCancel.disabled = false;
          keysClose.disabled = false;
        }
      }

      async function addFireworksKeys() {
        const keys = extractFireworksKeys(fwText.value || "");
        if (!keys.length) {
          fwFound.textContent = "Found 0 (paste keys first)";
          return;
        }

        fwSubmit.disabled = true;
        fwCancel.disabled = true;
        fwClose.disabled = true;
        statusEl.textContent = "Adding Fireworks keys...";
        try {
          const payload = {
            text: fwText.value,
            enabled: !!fwEnabled.checked,
            label_prefix: (fwPrefix.value || "").trim() || null,
            expected_email: (fwEmail.value || "").trim() || null,
            note: (fwNote.value || "").trim() || null,
            fireworks_model: (fwModel.value || "").trim() || null,
            fireworks_base_url: (fwBaseUrl.value || "").trim() || null,
          };
          const res = await fetch("/fireworks/add_keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          let body = null;
          try { body = await res.json(); } catch {}
          if (!res.ok) {
            const msg = (body && (body.detail || body.error)) ? (body.detail || body.error) : `HTTP ${res.status}`;
            throw new Error(msg);
          }

          setFwModalOpen(false);
          fwText.value = "";
          fwEmail.value = "";
          fwNote.value = "";
          renderFwPreview();

          await loadLatest();
          const added = body && typeof body.added === "number" ? body.added : 0;
          const updated = body && typeof body.updated === "number" ? body.updated : 0;
          lastUpdateText = `Fireworks keys updated — added:${added} updated:${updated}`;
          renderFromCache();
        } catch (e) {
          statusEl.textContent = `Add error: ${e}`;
        } finally {
          fwSubmit.disabled = false;
          fwCancel.disabled = false;
          fwClose.disabled = false;
        }
      }

      async function updateNow(label=null) {
        if (refreshing) return;
        refreshing = true;
        refreshBtn.disabled = true;

        const started = Date.now();
        const labelSuffix = label ? ` (${label})` : "";
        const renderRefreshing = () => {
          const sec = ((Date.now() - started) / 1000).toFixed(1);
          statusEl.textContent = `Refreshing (fetching latest limits)${labelSuffix}… ${sec}s`;
        };
        renderRefreshing();
        const tick = setInterval(renderRefreshing, 250);
        try {
          const url = label ? (`/refresh?label=${encodeURIComponent(label)}`) : "/refresh";
          const res = await fetch(url, { method: "POST" });
          let body = null;
          try { body = await res.json(); } catch {}
          if (!res.ok) {
            const msg = (body && (body.detail || body.error)) ? (body.detail || body.error) : `HTTP ${res.status}`;
            throw new Error(msg);
          }
          const ms = Date.now() - started;
          const s = body && body.summary ? body.summary : null;
          await loadLatest();

          if (s) {
            lastUpdateText = `Updated ${new Date().toLocaleTimeString()} — refresh ok:${s.ok} auth:${s.auth_required} err:${s.errors} (${ms}ms)`;
          } else {
            lastUpdateText = `Updated ${new Date().toLocaleTimeString()} — refresh done (${ms}ms)`;
          }
          renderFromCache();
        } catch (e) {
          await loadLatest();
          statusEl.textContent = `Refresh error: ${e}`;
        } finally {
          clearInterval(tick);
          refreshing = false;
          refreshBtn.disabled = false;
        }
      }

      function isReloadNavigation() {
        try {
          const entries = (performance.getEntriesByType && performance.getEntriesByType("navigation")) || [];
          if (entries.length > 0 && entries[0] && entries[0].type) return entries[0].type === "reload";
          return (performance.navigation && performance.navigation.type === 1) || false;
        } catch {
          return false;
        }
      }

	      const theadEl = document.querySelector("thead");
	      theadEl.addEventListener("click", (ev) => {
	        const t = ev.target;
	        const th = t && t.closest ? t.closest("th.sortable") : null;
	        if (!th) return;
	        const k = th.getAttribute("data-sort");
	        if (!k) return;
	        setSort(k);
	      });

	      refreshBtn.addEventListener("click", updateNow);
      addBtn.addEventListener("click", () => { setKeysModalOpen(false); setFwModalOpen(false); setModalOpen(true); });
      addKeysBtn.addEventListener("click", () => { setModalOpen(false); setFwModalOpen(false); setKeysModalOpen(true); });
      addFwBtn.addEventListener("click", () => { setModalOpen(false); setKeysModalOpen(false); setFwModalOpen(true); });
      addClose.addEventListener("click", () => setModalOpen(false));
      addCancel.addEventListener("click", () => setModalOpen(false));
      addSubmit.addEventListener("click", addAccounts);
      addText.addEventListener("input", renderAddPreview);
      addModal.addEventListener("click", (ev) => {
        if (ev.target === addModal) setModalOpen(false);
      });
      keysClose.addEventListener("click", () => setKeysModalOpen(false));
      keysCancel.addEventListener("click", () => setKeysModalOpen(false));
      keysSubmit.addEventListener("click", addClaudeKeys);
      keysText.addEventListener("input", renderKeysPreview);
      keysModal.addEventListener("click", (ev) => {
        if (ev.target === keysModal) setKeysModalOpen(false);
      });
      fwClose.addEventListener("click", () => setFwModalOpen(false));
      fwCancel.addEventListener("click", () => setFwModalOpen(false));
      fwSubmit.addEventListener("click", addFireworksKeys);
      fwText.addEventListener("input", renderFwPreview);
      fwModal.addEventListener("click", (ev) => {
        if (ev.target === fwModal) setFwModalOpen(false);
      });
      noteClose.addEventListener("click", () => setNoteModalOpen(false));
      noteCancel.addEventListener("click", () => setNoteModalOpen(false));
      noteSave.addEventListener("click", saveNote);
      noteModal.addEventListener("click", (ev) => {
        if (ev.target === noteModal) setNoteModalOpen(false);
      });
      document.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        if (addModal.classList.contains("open")) setModalOpen(false);
        if (keysModal.classList.contains("open")) setKeysModalOpen(false);
        if (fwModal.classList.contains("open")) setFwModalOpen(false);
        if (noteModal.classList.contains("open")) setNoteModalOpen(false);
      });
      filterEl.addEventListener("input", renderFromCache);
      if (resetSortBtn) resetSortBtn.addEventListener("click", () => setSort(null));
      if (viewModeEl) {
        viewModeEl.addEventListener("change", () => {
          const v = (viewModeEl.value || "").trim();
          viewMode = (v === "subscription" || v === "credits") ? v : "all";
          saveViewMode();
          applyViewMode();
          renderFromCache();
        });
      }
      rowsEl.addEventListener("click", (ev) => {
        const t = ev.target;
        const btn = t && t.closest ? t.closest("button[data-action][data-label]") : null;
        if (!btn) return;
        const action = btn.getAttribute("data-action") || "update";
        const labelEnc = btn.getAttribute("data-label");
        const label = labelEnc ? decodeURIComponent(labelEnc) : "";
        if (!label) return;
        if (action === "note") openNote(label);
        else updateNow(label);
      });

	      loadViewMode();
	      applyViewMode();
	      loadSort();
	      applySortIndicators();

	      syncRegistry().finally(() => {
	        loadLatest().then(() => {
	          if (isReloadNavigation()) updateNow();
	        });
	      });
	    </script>
	  </body>
	</html>
	"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS status_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_label TEXT NOT NULL,
              host TEXT,
              ts TEXT NOT NULL,
              raw TEXT NOT NULL,
              parsed_json TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_registry (
              account_label TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 1,
              provider TEXT,
              expected_email TEXT,
              expected_plan_type TEXT,
              note TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_status_events_account_ts ON status_events(account_label, ts)"
        )
        # Lightweight migrations for existing DBs.
        cols = [r[1] for r in con.execute("PRAGMA table_info(accounts_registry)").fetchall()]
        if "provider" not in cols:
            con.execute("ALTER TABLE accounts_registry ADD COLUMN provider TEXT")
        con.commit()


_init_db()


class StatusPayload(BaseModel):
    account_label: str = Field(min_length=1, max_length=200)
    host: str | None = None
    raw: str = Field(min_length=1)
    parsed: dict | None = None
    ts: str | None = None


class RegistryItem(BaseModel):
    account_label: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    provider: str | None = None
    expected_email: str | None = None
    expected_planType: str | None = None
    note: str | None = None


class RegistryPayload(BaseModel):
    accounts: list[RegistryItem]


class AddAccountsPayload(BaseModel):
    text: str | None = None
    emails: list[str] = Field(default_factory=list)
    expected_planType: str | None = None
    enabled: bool = True


class AddAnthropicKeysPayload(BaseModel):
    text: str | None = None
    keys: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str | None = None
    label_prefix: str | None = None
    expected_email: str | None = None
    anthropic_model: str | None = None


class AddFireworksKeysPayload(BaseModel):
    text: str | None = None
    keys: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str | None = None
    label_prefix: str | None = None
    expected_email: str | None = None
    fireworks_model: str | None = None
    fireworks_base_url: str | None = None


class AppendNotePayload(BaseModel):
    account_label: str
    append: str
    separator: str | None = None
    replace: bool = False


@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/refresh")
def refresh_now(label: str | None = None, include_disabled: bool = False):
    if not REFRESHER_REFRESH_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    url = REFRESHER_REFRESH_URL
    params: dict[str, str] = {}
    if label:
        params["label"] = label
    if include_disabled:
        params["include_disabled"] = "true"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/accounts/add")
def accounts_add(payload: AddAccountsPayload):
    if not REFRESHER_BASE_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    url = f"{REFRESHER_BASE_URL}/config/add_accounts"
    data = json.dumps(payload.model_dump(), ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/anthropic/add_keys")
def anthropic_add_keys(payload: AddAnthropicKeysPayload):
    if not REFRESHER_BASE_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    url = f"{REFRESHER_BASE_URL}/config/add_anthropic_keys"
    data = json.dumps(payload.model_dump(), ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/fireworks/add_keys")
def fireworks_add_keys(payload: AddFireworksKeysPayload):
    if not REFRESHER_BASE_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    url = f"{REFRESHER_BASE_URL}/config/add_fireworks_keys"
    data = json.dumps(payload.model_dump(), ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/notes/append")
def notes_append(payload: AppendNotePayload):
    if not REFRESHER_BASE_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    label = (payload.account_label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="account_label is required")
    append_text = (payload.append or "").strip()
    if not append_text:
        raise HTTPException(status_code=400, detail="append is required")

    url = f"{REFRESHER_BASE_URL}/config/note_append"
    data = json.dumps(
        {
            "label": label,
            "append": append_text,
            "separator": payload.separator,
            "replace": bool(payload.replace),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.post("/registry/sync")
def registry_sync():
    if not REFRESHER_BASE_URL:
        raise HTTPException(status_code=501, detail="refresher is disabled")

    url = f"{REFRESHER_BASE_URL}/config/push_registry"
    try:
        req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        detail = parsed.get("detail") if isinstance(parsed, dict) else body
        raise HTTPException(status_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"refresher unreachable: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


def _ensure_registry_account(account_label: str) -> None:
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT account_label FROM accounts_registry WHERE account_label = ? LIMIT 1",
            (account_label,),
        ).fetchone()
        if row is not None:
            return
        con.execute(
            """
            INSERT INTO accounts_registry(account_label, enabled, created_at, updated_at)
            VALUES(?, 1, ?, ?)
            """,
            (account_label, now, now),
        )
        con.commit()


def _upsert_registry_item(item: RegistryItem) -> None:
    now = _now_iso()
    expected_plan_type = item.expected_planType

    with sqlite3.connect(DB_PATH) as con:
        existing = con.execute(
            "SELECT created_at FROM accounts_registry WHERE account_label = ?",
            (item.account_label,),
        ).fetchone()
        created_at = existing[0] if existing else now
        con.execute(
            """
            INSERT INTO accounts_registry(
              account_label, enabled, provider, expected_email, expected_plan_type, note, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_label) DO UPDATE SET
              enabled = excluded.enabled,
              provider = excluded.provider,
              expected_email = excluded.expected_email,
              expected_plan_type = excluded.expected_plan_type,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (
                item.account_label,
                1 if item.enabled else 0,
                item.provider,
                item.expected_email,
                expected_plan_type,
                item.note,
                created_at,
                now,
            ),
        )
        con.commit()


def _registry_row_to_item(row: tuple) -> dict:
    account_label, enabled, provider, expected_email, expected_plan_type, note, created_at, updated_at = row
    return {
        "account_label": account_label,
        "enabled": bool(enabled),
        "provider": provider,
        "expected_email": expected_email,
        "expected_planType": expected_plan_type,
        "note": note,
        "created_at": created_at,
        "updated_at": updated_at,
    }


@app.get("/registry")
def registry_list():
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT account_label, enabled, provider, expected_email, expected_plan_type, note, created_at, updated_at
            FROM accounts_registry
            ORDER BY account_label ASC
            """
        ).fetchall()
    return {"items": [_registry_row_to_item(r) for r in rows]}


@app.post("/registry")
def registry_upsert(payload: RegistryPayload, replace: bool = False):
    if not payload.accounts:
        raise HTTPException(status_code=400, detail="accounts must be non-empty")

    with _db_lock:
        labels = []
        for item in payload.accounts:
            _upsert_registry_item(item)
            labels.append(item.account_label)

        if replace:
            uniq = sorted({l for l in labels if l})
            placeholders = ",".join(["?"] * len(uniq))
            with sqlite3.connect(DB_PATH) as con:
                con.execute(
                    f"DELETE FROM accounts_registry WHERE account_label NOT IN ({placeholders})",
                    tuple(uniq),
                )
                con.commit()

    return {"ok": True, "count": len(payload.accounts)}


@app.post("/ingest")
def ingest(payload: StatusPayload, request: Request):
    ts = payload.ts or _now_iso()
    host = payload.host or (request.client.host if request.client else None)
    parsed_json = (
        json.dumps(payload.parsed, ensure_ascii=False) if payload.parsed is not None else None
    )

    with _db_lock:
        _ensure_registry_account(payload.account_label)
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO status_events(account_label, host, ts, raw, parsed_json) VALUES(?,?,?,?,?)",
                (payload.account_label, host, ts, payload.raw, parsed_json),
            )
            con.commit()

    return {"ok": True, "ts": ts}


def _row_to_item(row: tuple) -> dict:
    account_label, host, ts, raw, parsed_json = row
    return {
        "account_label": account_label,
        "host": host,
        "ts": ts,
        "raw": raw,
        "parsed": json.loads(parsed_json) if parsed_json else None,
    }


@app.get("/latest")
def latest(include_orphans: bool = False):
    latest_query = """
      SELECT e.account_label, e.host, e.ts, e.raw, e.parsed_json
      FROM status_events e
      JOIN (
        SELECT account_label, MAX(ts) AS max_ts
        FROM status_events
        GROUP BY account_label
      ) m
      ON e.account_label = m.account_label AND e.ts = m.max_ts
    """

    with sqlite3.connect(DB_PATH) as con:
        event_rows = con.execute(latest_query).fetchall()
        registry_rows = con.execute(
            """
            SELECT account_label, enabled, provider, expected_email, expected_plan_type, note, created_at, updated_at
            FROM accounts_registry
            ORDER BY account_label ASC
            """
        ).fetchall()

    events_by_label: dict[str, dict] = {_row_to_item(r)["account_label"]: _row_to_item(r) for r in event_rows}
    registry_by_label: dict[str, dict] = {
        _registry_row_to_item(r)["account_label"]: _registry_row_to_item(r) for r in registry_rows
    }

    items: list[dict] = []
    for label, reg in registry_by_label.items():
        ev = events_by_label.pop(label, None)
        items.append(
            {
                "account_label": label,
                "host": ev.get("host") if ev else None,
                "ts": ev.get("ts") if ev else None,
                "raw": ev.get("raw") if ev else None,
                "parsed": ev.get("parsed") if ev else None,
                "registry": reg,
            }
        )

    if include_orphans:
        # Any accounts that posted but are not in registry (legacy / debug)
        for label in sorted(events_by_label.keys()):
            ev = events_by_label[label]
            items.append(
                {
                    "account_label": label,
                    "host": ev.get("host"),
                    "ts": ev.get("ts"),
                    "raw": ev.get("raw"),
                    "parsed": ev.get("parsed"),
                    "registry": None,
                }
            )

    return {"items": items}


@app.get("/latest/{account_label}")
def latest_account(account_label: str):
    with sqlite3.connect(DB_PATH) as con:
        reg_row = con.execute(
            """
            SELECT account_label, enabled, provider, expected_email, expected_plan_type, note, created_at, updated_at
            FROM accounts_registry
            WHERE account_label = ?
            LIMIT 1
            """,
            (account_label,),
        ).fetchone()
        event_row = con.execute(
            """
            SELECT account_label, host, ts, raw, parsed_json
            FROM status_events
            WHERE account_label = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (account_label,),
        ).fetchone()

    if reg_row is None and event_row is None:
        raise HTTPException(status_code=404, detail="not found")

    reg = _registry_row_to_item(reg_row) if reg_row else None
    ev = _row_to_item(event_row) if event_row else None
    return {
        "account_label": account_label,
        "host": ev.get("host") if ev else None,
        "ts": ev.get("ts") if ev else None,
        "raw": ev.get("raw") if ev else None,
        "parsed": ev.get("parsed") if ev else None,
        "registry": reg,
    }


@app.get("/events/{account_label}")
def events_account(account_label: str, limit: int = 50):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1..500")

    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT account_label, host, ts, raw, parsed_json
            FROM status_events
            WHERE account_label = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (account_label, limit),
        ).fetchall()
    return {"items": [_row_to_item(row) for row in rows]}
