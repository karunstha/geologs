// Settings → Import & export: download your history as CSV, GPX or GeoJSON, or the whole archive as a
// backup, and import a file from another app or server. An import runs on the server after the
// upload; this page follows it until it ends.
import { refreshAll } from "../app/refresh.js";
import { api } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { localDateKey, shiftDateKey, zonedMidnight } from "../core/format.js";

let polling = 0;

// The range as the server reads it: from midnight of the first day to midnight after the last, in
// the archive's timezone.
function range() {
  const query = new URLSearchParams();
  const start = $("export-start").value,
    end = $("export-end").value;
  if (start && end && end < start) throw Error("The last day comes before the first");
  if (start) query.set("start", zonedMidnight(start).toISOString());
  if (end) query.set("end", zonedMidnight(shiftDateKey(end, 1)).toISOString());
  return query;
}

// Downloads go through fetch, so a browser signed in with a token (not a cookie) can export too.
async function download(path, filename) {
  const token = sessionStorage.getItem("geologs-token");
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: token ? { Authorization: "Bearer " + token } : {},
  });
  if (!response.ok) throw Error((await response.json().catch(() => ({}))).error || `HTTP ${response.status}`);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function exportFile(kind) {
  const start = $("export-start").value,
    end = $("export-end").value;
  return download(`/api/export.${kind}?${range()}`, `geologs-${start || "all"}${end ? "-to-" + end : ""}.${kind}`);
}

// What a restored backup brought besides locations, in the words the rest of Settings uses.
const RESTORED = [
  [["places"], "saved place", "saved places"],
  [["place_labels"], "place name", "place names"],
  [["stay_edits", "mode_overrides"], "correction", "corrections"],
  [["reminders"], "reminder", "reminders"],
  [["known_paths"], "learned route", "learned routes"],
  [["daily_summaries"], "daily summary", "daily summaries"],
  [["ingest_tokens", "api_tokens"], "token", "tokens"],
];

function restoredSummary(restored) {
  const parts = RESTORED.map(([tables, one, many]) => {
    const count = tables.reduce((sum, table) => sum + Number(restored[table] || 0), 0);
    return count ? `${count.toLocaleString()} ${count === 1 ? one : many}` : "";
  }).filter(Boolean);
  return parts.length ? `; also ${parts.join(", ")}` : "";
}

// The same history under another tracker name, usually an earlier import of part of it: worth
// removing, or every day there counts it twice.
function overlapNote(overlaps) {
  const names = Object.keys(overlaps || {});
  if (!names.length) return "";
  const listed = names.map((name) => `${Number(overlaps[name]).toLocaleString()} under “${name}”`).join(", ");
  return ` These locations are also here ${listed}. If that came from an earlier import of the same history, remove that tracker in Settings → Devices so it isn't counted twice.`;
}

function renderImport(status) {
  const box = $("import-status");
  const running = status.state === "running";
  $("import-go").disabled = running;
  box.hidden = status.state === "idle";
  box.className = `callout${status.state === "failed" ? " warning" : status.state === "done" ? " success" : ""}`;
  const counts = `${Number(status.inserted || 0).toLocaleString()} new location${status.inserted === 1 ? "" : "s"}${status.duplicates ? `, ${Number(status.duplicates).toLocaleString()} already here` : ""}`;
  box.innerHTML = running
    ? status.phase === "restoring"
      ? `<strong>Restoring the backup…</strong><span>${Number(status.read || 0).toLocaleString()} rows read so far.</span>`
      : `<strong>${status.phase === "uploading" ? "Uploading…" : status.phase === "rebuilding" ? "Working out stays and trips…" : "Importing…"}</strong><span>${safe(counts)} so far.</span>`
    : status.state === "done" && status.format === "backup"
      ? `<strong>Restored the backup</strong><span>${safe(counts)}${safe(restoredSummary(status.restored || {}))}.${safe(overlapNote(status.overlaps))}</span>`
      : status.state === "done"
      ? `<strong>Imported ${safe(status.format || "")} as ${safe(status.source || "")}</strong><span>${safe(counts)}${status.skipped ? `; ${Number(status.skipped).toLocaleString()} without a time or place skipped` : ""}.${safe(overlapNote(status.overlaps))}</span>`
      : `<strong>The import stopped</strong><span>${safe(status.error || "")}</span>`;
}

async function follow() {
  clearTimeout(polling);
  try {
    const status = await api("/api/import");
    renderImport(status);
    if (status.state === "running") polling = setTimeout(follow, 1500);
    else if (status.state === "done" && $("import-go").dataset.waiting) {
      delete $("import-go").dataset.waiting;
      toast("Import finished");
      await refreshAll();
    }
  } catch (_error) {
    /* signed out, or offline: the page says so elsewhere */
  }
}

// Called when the page is opened: an import started earlier (or in another tab) may still be running.
export function showData() {
  follow();
}

export function initData() {
  $("export-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      await exportFile(button.dataset.export);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
  $("backup-go").onclick = async () => {
    $("backup-go").disabled = true;
    try {
      await download("/api/backup", `geologs-backup-${localDateKey()}.json.gz`);
    } catch (error) {
      toast(error.message);
    } finally {
      $("backup-go").disabled = false;
    }
  };
  $("import-form").onsubmit = async (event) => {
    event.preventDefault();
    const file = $("import-file").files[0];
    if (!file) return;
    renderImport({ state: "running", phase: "uploading" });
    $("import-go").dataset.waiting = "1";
    try {
      await api("/api/import?" + new URLSearchParams({ source: $("import-source").value.trim() }), {
        method: "POST",
        body: file,
        headers: { "Content-Type": "application/octet-stream" },
      });
      $("import-form").reset();
    } catch (error) {
      toast(error.message);
    }
    follow();
  };
}
