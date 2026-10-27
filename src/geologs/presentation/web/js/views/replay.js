// Home's Replay button, and sharing a replay: a link anyone can open (js/share.js) to watch the day,
// with the start, the end and Home cut out unless you choose otherwise, a picture of it for chat apps to
// show with the link, and the code to embed it in a page. The player is js/replay/player.js.
import { api, post } from "../core/api.js";
import { $, safe, toast } from "../core/dom.js";
import { dayTitle, distanceText, formatTime } from "../core/format.js";
import { state } from "../core/state.js";
import { blobBase64, drawCard } from "../replay/capture.js";
import { openReplay, pauseReplay, replayDialog } from "../replay/player.js";
import { refreshAccount } from "./account.js";

const HIDE_CHOICES_M = [0, 200, 500, 1000];
const EXPIRY_CHOICES = [
  [1, "1 day"],
  [7, "7 days"],
  [30, "30 days"],
  ["", "Until I turn it off"],
];

function hideLabel(metres) {
  return metres ? `Hide ${distanceText(metres / 1000)} around them` : "Show everything";
}

// A Copy button for a field's text; it says so itself (a toast would sit under the replay's dialog).
function copier(button, text) {
  button.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch (_error) {
      button.previousElementSibling?.select?.();
    }
  };
}

// The picture chat apps show for a link: drawn here from what the link itself shows (its own frozen,
// trimmed replay, never more of the day), then kept with the link.
async function addPicture(made, result) {
  const shared = await fetch(`/api/shared/${encodeURIComponent(made.token)}`, { credentials: "omit", cache: "no-store" });
  if (!shared.ok) return;
  const data = await shared.json();
  const card = await drawCard(data, data.title);
  if (!card) return;
  await post("/api/shares/preview", { id: made.id, image: await blobBase64(card) });
  const image = result.querySelector(".replay-share-card");
  image.src = URL.createObjectURL(card);
  image.hidden = false;
}

// The Share panel, over the paused replay: what to keep private, then the link, shown once. Toasts would
// sit under the replay's dialog, so the panel says itself what happened.
function openSharePanel(day) {
  pauseReplay();
  $("replay-share-panel")?.remove();
  const panel = document.createElement("form");
  panel.id = "replay-share-panel";
  panel.className = "replay-share-panel replay-panel";
  panel.setAttribute("aria-labelledby", "replay-share-title");
  panel.innerHTML = `
    <div class="replay-share-head"><h3 id="replay-share-title">Share this replay</h3><button type="button" class="icon-button" data-close aria-label="Close sharing"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>
    <p class="replay-share-note">Anyone with the link can watch it, without signing in. It shows the day as it is now.</p>
    <label class="field"><span>Title</span><input name="title" class="input" maxlength="80" placeholder="${safe(dayTitle(day.date))}"></label>
    <label class="field"><span>The start, the end and Home</span><select name="hide_m" class="input">${HIDE_CHOICES_M.map(
      (metres) => `<option value="${metres}"${metres === 500 ? " selected" : ""}>${safe(hideLabel(metres))}</option>`,
    ).join("")}</select></label>
    <fieldset class="replay-share-choice"><legend>Times</legend>
      <label><input type="radio" name="times" value="elapsed" checked> Time since the start</label>
      <label><input type="radio" name="times" value="clock"> Time of day</label>
    </fieldset>
    <label class="replay-share-check"><input type="checkbox" name="names" checked> Show place names</label>
    <label class="field"><span>The link works for</span><select name="expires_days" class="input">${EXPIRY_CHOICES.map(
      ([value, label]) => `<option value="${value}"${value === 7 ? " selected" : ""}>${label}</option>`,
    ).join("")}</select></label>
    <p class="replay-share-error" role="alert" hidden></p>
    <button class="button primary" type="submit">Create link</button>
    <div class="replay-share-result" role="status" hidden></div>`;
  replayDialog().append(panel);
  panel.querySelector("[data-close]").onclick = () => panel.remove();
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    panel.remove();
  });
  panel.onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(panel),
      submit = panel.querySelector('button[type="submit"]'),
      error = panel.querySelector(".replay-share-error");
    submit.disabled = true;
    error.hidden = true;
    try {
      const made = await post("/api/shares", {
        date: day.date,
        timezone: day.timezone,
        ...(day.source ? { source: day.source } : {}),
        title: form.get("title"),
        hide_m: +form.get("hide_m"),
        times: form.get("times"),
        names: form.get("names") === "on",
        expires_days: form.get("expires_days") ? +form.get("expires_days") : null,
      });
      const link = location.origin + made.path;
      const result = panel.querySelector(".replay-share-result");
      result.hidden = false;
      const embed = `<iframe src="${link}" width="640" height="480" style="border:0;border-radius:12px" allow="fullscreen" loading="lazy" referrerpolicy="no-referrer" title="${safe(made.title)}"></iframe>`;
      result.innerHTML = `<strong>Copy the link now</strong><span>It is shown only once. ${
        made.expires_at ? `It works until ${safe(formatTime(made.expires_at))}` : "It works until you turn it off"
      }, under Account → Shared links.</span><div class="copy-field"><input class="input mono" readonly value="${safe(link)}" aria-label="Link to the replay"><button type="button" class="button secondary small" data-copy-link>Copy</button><a class="button ghost small" href="${safe(made.path)}" target="_blank" rel="noopener">Open</a></div><details class="replay-share-embed"><summary>Embed in a page</summary><div class="copy-field"><input class="input mono" readonly value="${safe(embed)}" aria-label="Code to embed the replay"><button type="button" class="button secondary small" data-copy-embed>Copy</button></div></details><img class="replay-share-card" alt="The picture shown with the link" hidden>`;
      copier(result.querySelector("[data-copy-link]"), link);
      copier(result.querySelector("[data-copy-embed]"), embed);
      result.querySelector("input").select();
      // Without a picture, a chat app shows the link with its title and totals alone.
      addPicture(made, result).catch(() => {});
      submit.hidden = true;
      refreshAccount().catch(() => {});
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      submit.disabled = false;
    }
  };
  panel.querySelector('input[name="title"]').focus();
}

export function initReplay() {
  // Home's Replay button: the day shown, once it has a way to replay.
  const button = $("replay-day");
  const offer = (day) =>
    !!day && (day.trips?.length > 0 || day.stays?.some((stay) => stay.activity || stay.internal_moves?.length));
  document.addEventListener("geologs:day-drawn", () => (button.hidden = !offer(state.day)));
  button.onclick = async () => {
    const day = state.day;
    if (!day) return;
    button.disabled = true;
    try {
      const query = new URLSearchParams({ date: day.date, timezone: day.timezone });
      if (day.source) query.set("source", day.source);
      const problem = openReplay(await api("/api/replay?" + query), {
        title: dayTitle(day.date),
        share: () => openSharePanel(day),
      });
      if (problem) toast(problem);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };
}
