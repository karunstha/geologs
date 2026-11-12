// Settings → Import & export: download your history as CSV, GPX or GeoJSON, or the whole archive as a
// backup, and import a file from another app or server. An import runs on the server after the
// upload; this page follows it until it ends. Data retention (how long raw locations are kept) is here too.
import { refreshAll } from "../app/refresh.js";
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { localDateKey, longDate, shiftDateKey, zonedMidnight } from "../core/format.js";

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

const yearsText = (years) => (years ? `${years} year${years === 1 ? "" : "s"}` : "Forever");
let retention = null;

function retentionNote(status, years) {
  const preview = status.preview;
  if (!years) return status.frozen_before_date ? "Keep every raw location from now on." : "Forever: nothing is frozen.";
  if (!preview.moves) return `Days before ${longDate(preview.freeze_date || status.frozen_before_date)} are frozen; nothing older is left to thin.`;
  return `Days before ${longDate(preview.freeze_date)} freeze; about ${Number(preview.samples_removed).toLocaleString()} raw samples are removed.`;
}

function renderRetention(status) {
  retention = status;
  const select = $("retention-years");
  if (!select.options.length)
    select.innerHTML = status.choices.map((years) => `<option value="${years}">${yearsText(years)}</option>`).join("");
  select.value = String(status.years);
  $("retention-note").textContent = retentionNote(status, status.years);
  $("retention-go").disabled = true;
  const box = $("retention-status");
  box.hidden = !status.frozen_before_date;
  box.innerHTML = status.frozen_before_date
    ? `<strong>Days before ${safe(longDate(status.frozen_before_date))} are frozen</strong><span>They show what was found for them, and cannot be edited.</span>`
    : "";
}

async function showRetention() {
  try {
    renderRetention(await api("/api/retention"));
  } catch (_error) {
    /* signed out, or offline: the page says so elsewhere */
  }
}

// Called when the page is opened: an import started earlier (or in another tab) may still be running.
export function showData() {
  follow();
  showRetention();
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
  $("retention-years").onchange = async () => {
    const years = Number($("retention-years").value);
    $("retention-go").disabled = years === retention?.years;
    try {
      const preview = await api("/api/retention?" + new URLSearchParams({ years }));
      $("retention-note").textContent = retentionNote(preview, years);
    } catch (error) {
      toast(error.message);
    }
  };
  $("retention-form").onsubmit = async (event) => {
    event.preventDefault();
    const years = Number($("retention-years").value);
    $("retention-go").disabled = true;
    try {
      let result = await post("/api/retention", { years });
      if (!result.saved) {
        const preview = result.preview;
        const question = preview.moves
          ? `Freeze the days before ${longDate(preview.freeze_date)} and remove about ${Number(preview.samples_removed).toLocaleString()} raw samples? They cannot be brought back, and frozen days cannot be edited.`
          : `Keep raw locations for ${yearsText(years).toLowerCase()}? Days older than that will freeze as they age, and their raw samples will be removed.`;
        if (!confirm(question)) {
          renderRetention(retention);
          return;
        }
        result = await post("/api/retention", { years, confirm: true });
      }
      renderRetention(result);
      toast(years ? `Raw locations are kept for ${yearsText(years).toLowerCase()}` : "Raw locations are kept forever");
    } catch (error) {
      toast(error.message);
      $("retention-go").disabled = false;
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
