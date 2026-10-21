// Settings → Import & export: download your history as CSV, GPX or GeoJSON, and import a file from
// another app. An import runs on the server after the upload; this page follows it until it ends.
import { refreshAll } from "../app/refresh.js";
import { api } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { shiftDateKey, zonedMidnight } from "../core/format.js";

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
async function download(kind) {
  const token = sessionStorage.getItem("geologs-token");
  const response = await fetch(`/api/export.${kind}?${range()}`, {
    credentials: "same-origin",
    headers: token ? { Authorization: "Bearer " + token } : {},
  });
  if (!response.ok) throw Error((await response.json().catch(() => ({}))).error || `HTTP ${response.status}`);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = `geologs-${$("export-start").value || "all"}${$("export-end").value ? "-to-" + $("export-end").value : ""}.${kind}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function renderImport(status) {
  const box = $("import-status");
  const running = status.state === "running";
  $("import-go").disabled = running;
  box.hidden = status.state === "idle";
  box.className = `callout${status.state === "failed" ? " warning" : status.state === "done" ? " success" : ""}`;
  const counts = `${Number(status.inserted || 0).toLocaleString()} new location${status.inserted === 1 ? "" : "s"}${status.duplicates ? `, ${Number(status.duplicates).toLocaleString()} already here` : ""}`;
  box.innerHTML = running
    ? `<strong>${status.phase === "uploading" ? "Uploading…" : status.phase === "rebuilding" ? "Working out stays and trips…" : "Importing…"}</strong><span>${safe(counts)} so far.</span>`
    : status.state === "done"
      ? `<strong>Imported ${safe(status.format || "")} as ${safe(status.source || "")}</strong><span>${safe(counts)}${status.skipped ? `; ${Number(status.skipped).toLocaleString()} without a time or place skipped` : ""}.</span>`
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
      await download(button.dataset.export);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
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
