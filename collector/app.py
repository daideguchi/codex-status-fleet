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
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji"; margin: 16px; }
      h1 { font-size: 18px; margin: 0 0 8px; }
      .meta { font-size: 12px; opacity: 0.8; margin-bottom: 12px; }
      .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
      button { padding: 6px 10px; border-radius: 8px; border: 1px solid #8884; background: #8882; cursor: pointer; }
      button:hover { background: #8883; }
      input, textarea, select { padding: 6px 10px; border-radius: 8px; border: 1px solid #8884; background: #8881; }
      textarea { width: 100%; min-height: 140px; resize: vertical; }
      table { width: 100%; border-collapse: collapse; font-size: 13px; }
      th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #8883; vertical-align: top; }
      th { position: sticky; top: 0; background: Canvas; z-index: 1; }
      .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; border: 1px solid #8884; font-size: 12px; }
      .ok { color: #0a7; border-color: #0a74; }
      .bad { color: #d55; border-color: #d554; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
      .muted { opacity: 0.8; }
      .right { text-align: right; }
      .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
      .modal { position: fixed; inset: 0; background: #0007; display: none; align-items: center; justify-content: center; padding: 16px; }
      .modal.open { display: flex; }
      .card { background: Canvas; border: 1px solid #8884; border-radius: 12px; padding: 12px; width: min(720px, 100%); }
      .card h2 { font-size: 14px; margin: 0 0 8px; }
      .small { font-size: 12px; opacity: 0.85; }
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
      <label class="muted">Filter <input id="filter" placeholder="label / email / provider" /></label>
      <span id="summary" class="muted"></span>
      <span id="status" class="muted"></span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Label</th>
          <th>Provider</th>
          <th>Account / Note</th>
          <th>Plan / Model</th>
          <th>Limits</th>
          <th>Last update</th>
          <th>State</th>
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
    <script>
      const $ = (id) => document.getElementById(id);
      const rowsEl = $("rows");
      const statusEl = $("status");
      const summaryEl = $("summary");
      const refreshBtn = $("refresh");
      const addBtn = $("add");
      const addKeysBtn = $("addKeys");
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
      const keysModel = $("keysModel");
      const keysNote = $("keysNote");
      const keysEnabled = $("keysEnabled");
      const keysFound = $("keysFound");
      const keysPreview = $("keysPreview");
      let cachedItems = [];
      let lastUpdateText = "";
      let refreshing = false;

      function fmtPct(p) {
        if (p === null || p === undefined) return "-";
        if (typeof p !== "number") return "-";
        return `${p}%`;
      }
      function fmtTs(iso) {
        if (!iso) return "-";
        try { return new Date(iso).toLocaleString(); } catch { return String(iso); }
      }
      function safe(v, fallback="-") {
        if (v === null || v === undefined || v === "") return fallback;
        return String(v);
      }
      function esc(v) {
        return String(v).replace(/[&<>"']/g, (c) => {
          if (c === "&") return "&amp;";
          if (c === "<") return "&lt;";
          if (c === ">") return "&gt;";
          if (c === "\"") return "&quot;";
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

        const isAnthropic = providerLc.startsWith("anthropic") || providerLc.startsWith("claude");
        const planOrModel = isAnthropic ? safe(model) : safe(plan);

        let state = "";
        const cls = classify(item);
        if (cls === "disabled") state = pill("disabled", false);
        else if (cls === "pending") state = pill("pending", false);
        else if (cls === "no_parsed") state = pill("no parsed", false);
        else if (cls === "auth_required") state = pill("auth required", false);
        else if (cls === "probe_error") state = pill("probe_error", false);
        else state = pill("ok", true);

        const expectedMatch = norm.expected_email_match;
        let accountLineHtml = esc(safe(email, isAnthropic ? "(api key)" : "(unknown)"));
        if (!norm.account_email && (norm.expected_email || regEmail)) {
          accountLineHtml += " " + pill("expected", true);
        } else if (typeof expectedMatch === "boolean") {
          accountLineHtml += " " + (expectedMatch ? pill("email ok", true) : pill("email mismatch", false));
        }

        let accountHtml = `<div>${accountLineHtml}</div>`;
        if (regNote) {
          accountHtml += `<div class="small muted">${esc(regNote)}</div>`;
        }

        const providerHtml = providerRaw ? `<span class="pill mono">${esc(providerRaw)}</span>` : "-";

        const limitLines = [];
        const addLimit = (name, w) => {
          if (!w || typeof w !== "object") return;
          const left = fmtPct(w.leftPercent);
          const reset = fmtTs(w.resetsAtIsoUtc);
          let counts = "";
          if (w.remaining !== null && w.remaining !== undefined && w.limit !== null && w.limit !== undefined) {
            counts = ` ${w.remaining}/${w.limit}`;
          }
          const parts = [name + ":", left];
          if (counts) parts.push(counts.trim());
          if (reset !== "-" && reset !== "") parts.push("resets", reset);
          limitLines.push(parts.join(" "));
        };

        addLimit("5h", windows["5h"]);
        addLimit("weekly", windows["weekly"]);
        addLimit("requests", windows["requests"]);
        addLimit("tokens", windows["tokens"]);
        for (const k of Object.keys(windows || {})) {
          if (k === "5h" || k === "weekly" || k === "requests" || k === "tokens") continue;
          addLimit(k, windows[k]);
        }
        const limitsText = limitLines.length ? limitLines.join("\\n") : "-";
        const limitsHtml = `<pre class="mono small" style="margin:0; white-space: pre-wrap;">${esc(limitsText)}</pre>`;

        return `
          <tr>
            <td class="mono"><a class="mono" href="/latest/${encodeURIComponent(safe(item.account_label))}">${esc(safe(item.account_label))}</a></td>
            <td>${providerHtml}</td>
            <td>${accountHtml}</td>
            <td class="mono">${esc(planOrModel)}</td>
            <td>${limitsHtml}</td>
            <td class="mono">${esc(fmtTs(lastUpdate))}</td>
            <td>${state}</td>
            <td><button data-label="${encodeURIComponent(safe(item.account_label))}">Update</button></td>
          </tr>
        `;
      }

      function renderFromCache() {
        const items = Array.isArray(cachedItems) ? cachedItems : [];

        const q = (filterEl.value || "").trim().toLowerCase();
        const filtered = q
          ? items.filter((it) => {
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
          : items;

        rowsEl.innerHTML = filtered.map(rowHtml).join("");
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
        statusEl.textContent = `${filtered.length}/${items.length} rows${suffix}`;
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

      async function updateNow(label=null) {
        if (refreshing) return;
        refreshing = true;
        refreshBtn.disabled = true;

        const started = Date.now();
        statusEl.textContent = "Refreshing (fetching latest limits)...";
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

      refreshBtn.addEventListener("click", updateNow);
      addBtn.addEventListener("click", () => { setKeysModalOpen(false); setModalOpen(true); });
      addKeysBtn.addEventListener("click", () => { setModalOpen(false); setKeysModalOpen(true); });
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
      document.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        if (addModal.classList.contains("open")) setModalOpen(false);
        if (keysModal.classList.contains("open")) setKeysModalOpen(false);
      });
      filterEl.addEventListener("input", renderFromCache);
      rowsEl.addEventListener("click", (ev) => {
        const t = ev.target;
        const btn = t && t.closest ? t.closest("button[data-label]") : null;
        if (!btn) return;
        const labelEnc = btn.getAttribute("data-label");
        const label = labelEnc ? decodeURIComponent(labelEnc) : "";
        if (!label) return;
        updateNow(label);
      });

      loadLatest().then(() => {
        if (isReloadNavigation()) updateNow();
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
    anthropic_model: str | None = None


@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(UI_HTML)


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
