// The admin portal: users (what they recorded, never where), invites, the audit log, and the server.
import { api, post } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { ago, plural, zoneOptions } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach } from "../ui/actions.js";
import { statusPill } from "../ui/icons.js";
import { showAccount } from "./account.js";

const ACTIONS = {
  role: "changed the role of",
  disable: "disabled",
  enable: "enabled",
  "reset link": "made a password-reset link for",
  delete: "deleted",
  invite: "invited",
  "revoke invite": "withdrew the invite for",
  "sign-up": "set sign-up",
  "clear failed attempts": "cleared the failed tracker requests",
};

function shortDate(value) {
  return value
    ? new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zoneOptions() })
    : "—";
}

function bytes(size) {
  if (size == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size,
    unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function reveal(id, html) {
  const box = $(id);
  box.hidden = false;
  box.innerHTML = html + '<div class="copy-field"><button type="button" class="button ghost small" data-hide-reveal>Done</button></div>';
  box.querySelector("[data-hide-reveal]").onclick = () => {
    box.hidden = true;
    box.innerHTML = "";
  };
  box.querySelector("[data-copy-link]")?.addEventListener("click", (event) =>
    copyText(event.currentTarget.dataset.copyLink, "Link copied"),
  );
}

function linkReveal(id, title, note, link) {
  reveal(
    id,
    `<strong>${safe(title)}</strong><span>${safe(note)}</span>` +
      (link
        ? `<div class="copy-field"><input class="input mono" readonly value="${safe(link)}" aria-label="Link"><button type="button" class="button secondary small" data-copy-link="${safe(link)}">Copy</button></div>`
        : "<span>Set GEOLOGS_PUBLIC_URL to make a link to share.</span>"),
  );
}

function renderUsers(users) {
  $("admin-user-count").textContent = users.length;
  const me = state.user?.id;
  $("admin-users").innerHTML = users
    .map((user) => {
      const badges = [
        user.id === me ? statusPill("neutral", "You") : "",
        user.role === "admin" ? statusPill("success", "Admin") : "",
        user.disabled_at ? statusPill("error", "Disabled") : "",
        user.two_factor ? statusPill("success", "2FA") : "",
        user.email && !user.email_verified_at ? statusPill("warning", "Unconfirmed") : "",
      ].join("");
      const recorded = user.fixes
        ? `${user.fixes.toLocaleString()} fixes from ${plural(user.sources, "tracker")} · last fix ${ago(user.last_fix)}`
        : user.counted_at
          ? "nothing recorded yet"
          : "not counted yet";
      const details = [
        user.email || "no email",
        recorded,
        user.last_login_at ? `signed in ${ago(user.last_login_at)}` : "never signed in with a password",
        `joined ${shortDate(user.created_at)}`,
      ];
      const owner = user.id === 1;
      const actions = [
        user.id !== me || user.role !== "admin"
          ? `<button type="button" data-admin-role="${user.id}:${user.role === "admin" ? "user" : "admin"}">${user.role === "admin" ? "Make a user" : "Make an admin"}</button>`
          : "",
        user.email ? `<button type="button" data-admin-reset="${user.id}">Password reset link</button>` : "",
        user.id !== me && !owner
          ? `<button type="button" data-admin-disable="${user.id}:${user.disabled_at ? "enable" : "disable"}">${user.disabled_at ? "Enable" : "Disable"}</button>`
          : "",
        user.id !== me && !owner ? `<button type="button" class="danger-text" data-admin-delete="${user.id}">Delete…</button>` : "",
      ].filter(Boolean);
      const initial = safe((user.name || user.email || "?").trim()[0].toUpperCase());
      return `<li class="list-item admin-user"><div class="place-main"><span class="admin-avatar" aria-hidden="true">${initial}</span><div><strong>${safe(user.name || user.email || `User ${user.id}`)} ${badges}</strong><small>${safe(details.join(" · "))}</small></div></div>${
        actions.length
          ? `<details class="row-menu"><summary class="button secondary small">Manage</summary><div class="row-menu-list">${actions.join("")}</div></details>`
          : ""
      }</li>`;
    })
    .join("");
  const find = (id) => users.find((user) => user.id === +id);
  onEach($("admin-users"), "admin-role", (value) => act(async () => {
    const [id, role] = value.split(":");
    await post("/api/admin/users/role", { id: +id, role });
    toast(`${find(id).email || "That user"} is ${role === "admin" ? "an admin" : "a user"} now`);
  }));
  onEach($("admin-users"), "admin-disable", (value) => act(async () => {
    const [id, change] = value.split(":");
    if (change === "disable" && !confirm(`Disable ${find(id).email}? They are signed out everywhere and cannot sign in until you enable them.`)) return;
    await post("/api/admin/users/disable", { id: +id, disabled: change === "disable" });
    toast(change === "disable" ? "Disabled" : "Enabled");
  }));
  onEach($("admin-users"), "admin-reset", (id) => act(async () => {
    const made = await post("/api/admin/users/reset", { id: +id });
    linkReveal("admin-reveal", `Password reset link for ${find(id).email}`,
      made.emailed ? "Emailed to them, and here to pass on. It works once, for an hour." : "Pass it on: this server can't email. It works once, for an hour.",
      made.link);
  }));
  onEach($("admin-users"), "admin-delete", (id) => act(async () => {
    const user = find(id);
    const typed = prompt(`Delete ${user.email || "this account"} and everything it recorded? This cannot be undone.\n\nType the email to confirm.`);
    if (typed == null) return;
    await post("/api/admin/users/delete", { id: +id, email: typed });
    toast("Deleted");
  }));
  document.querySelectorAll("#admin-users details.row-menu").forEach((menu) =>
    menu.addEventListener("toggle", () => {
      if (menu.open) document.querySelectorAll("#admin-users details.row-menu[open]").forEach((other) => other !== menu && (other.open = false));
    }),
  );
}

async function act(run) {
  try {
    await run();
    await refreshAdmin();
  } catch (error) {
    toast(error.message);
  }
}

function renderInvites(invites) {
  $("admin-invite-count").textContent = invites.length || "";
  $("admin-invites").innerHTML =
    invites
      .map(
        (invite) =>
          `<li class="list-item"><div class="place-main"><span class="health-dot neutral" aria-hidden="true"></span><div><strong>${safe(invite.email)}</strong><small>${safe(`as ${invite.role === "admin" ? "an admin" : "a user"} · sent ${ago(invite.created_at)} · expires ${shortDate(invite.expires_at)}`)}</small></div></div><button type="button" class="button secondary small" data-revoke-invite="${safe(invite.email)}">Withdraw</button></li>`,
      )
      .join("") || '<li class="empty-block"><strong>No invites waiting</strong><span>Invite someone above; the link works for 7 days.</span></li>';
  onEach($("admin-invites"), "revoke-invite", (email) => act(async () => {
    await post("/api/admin/invites/revoke", { email });
    toast("Invite withdrawn");
  }));
}

function renderAudit(entries) {
  $("admin-audit").innerHTML =
    entries
      .map((entry) => {
        const target = entry.target_email ? ` ${entry.target_email}` : "";
        const extra =
          entry.action === "role" ? ` (${entry.details?.from} → ${entry.details?.to})` : entry.action === "sign-up" ? ` to ${entry.details?.mode}` : "";
        return `<li class="list-item"><div><strong>${safe(entry.actor_email || "The owner")} ${safe(ACTIONS[entry.action] || entry.action)}${safe(target)}${safe(extra)}</strong><small>${safe([ago(entry.at), new Date(entry.at).toLocaleString(undefined, zoneOptions()), entry.client].filter(Boolean).join(" · "))}</small></div></li>`;
      })
      .join("") || '<li class="empty-block"><strong>Nothing yet</strong><span>Every change an admin makes is listed here.</span></li>';
}

function renderServer(health) {
  const email = health.email;
  const jobs = Object.entries(health.jobs);
  const outside = Object.entries(health.outside_services.today).sort((a, b) => b[1] - a[1]);
  const failures = health.failed_attempts;
  $("admin-server").innerHTML = `
    <section class="admin-section"><h3>Sign-up</h3>
      <form id="admin-signup-form" class="segmented-choice">
        <label><input type="radio" name="signup" value="invite" ${health.signup === "invite" ? "checked" : ""}><span><strong>Invite only</strong><small>New accounts come from an admin's invite.</small></span></label>
        <label><input type="radio" name="signup" value="open" ${health.signup === "open" ? "checked" : ""}><span><strong>Open</strong><small>Anyone who can reach this server can make an account.</small></span></label>
      </form></section>
    <section class="admin-section"><h3>Email ${email.can_send ? statusPill("success", "Sending") : statusPill("warning", "Not set up")}</h3>
      <p class="help">${email.can_send ? `Links point to ${safe(email.public_url)}.` : `Set ${email.mail_server ? "" : "GEOLOGS_SMTP_HOST and GEOLOGS_SMTP_FROM"}${!email.mail_server && !email.public_url ? " and " : ""}${email.public_url ? "" : "GEOLOGS_PUBLIC_URL"} to email invites and password resets. Until then, links are shown here to pass on.`}</p>
      <ul class="list rows compact-rows">${
        email.recent
          .slice(0, 8)
          .map(
            (item) =>
              `<li class="list-item"><div class="place-main"><span class="health-dot ${item.sent_at ? "success" : item.failed_at ? "error" : "warning"}" aria-hidden="true"></span><div><strong>${safe(item.subject)}</strong><small>${safe([item.to_address, item.sent_at ? `sent ${ago(item.sent_at)}` : item.failed_at ? `gave up after ${plural(item.attempts, "try", "tries")}` : `waiting · ${plural(item.attempts, "try", "tries")}`, item.last_error].filter(Boolean).join(" · "))}</small></div></div></li>`,
          )
          .join("") || '<li class="empty-block"><span>No email sent yet.</span></li>'
      }</ul></section>
    <section class="admin-section"><h3>Tracker requests with a wrong token ${failures.length ? `<button type="button" id="admin-clear-failures" class="button ghost small">Clear</button>` : ""}</h3>
      <p class="help">A phone sending with a wrong or old token belongs to no one, so it shows here rather than in anyone's devices.</p>
      <ul class="list rows compact-rows">${
        failures
          .map(
            (item) =>
              `<li class="list-item"><div class="place-main"><span class="health-dot error" aria-hidden="true"></span><div><strong>${safe(item.device || item.kind)}</strong><small>${safe([`${item.kind}`, `${plural(item.attempts, "try", "tries")}`, `last ${ago(item.last_at)}`, item.client].join(" · "))}</small></div></div></li>`,
          )
          .join("") || '<li class="empty-block"><span>None.</span></li>'
      }</ul></section>
    <section class="admin-section"><h3>Background work</h3>
      <div class="table-wrap"><table><thead><tr><th>Job</th><th>Last run</th><th>Runs</th><th>Slowest</th><th>Last problem</th></tr></thead><tbody>${
        jobs
          .map(
            ([name, job]) =>
              `<tr><td>${safe(name)}</td><td>${safe(job.last_at ? ago(job.last_at) : "—")}</td><td>${job.runs.toLocaleString()}</td><td>${job.slowest_seconds.toFixed(2)} s</td><td>${job.last_error ? `${safe(job.last_error)} <span class="muted">(${safe(ago(job.last_error_at))})</span>` : "—"}</td></tr>`,
          )
          .join("") || '<tr><td colspan="5" class="muted">Nothing has run since the server started.</td></tr>'
      }</tbody></table></div></section>
    <section class="admin-section admin-columns">
      <div><h3>Database</h3><p class="admin-figure">${bytes(health.database.total)}</p><ul class="admin-bars">${health.database.tables
        .map((table) => `<li><span>${safe(table.name)}</span><span>${bytes(table.size)}</span></li>`)
        .join("")}</ul></div>
      <div><h3>Outside services today</h3><ul class="admin-bars">${
        outside.map(([service, count]) => `<li><span>${safe(service)}</span><span>${count.toLocaleString()}</span></li>`).join("") ||
        '<li><span class="muted">None asked yet today.</span></li>'
      }</ul><p class="help">Counted since the server started; free plans (Geoapify, Unsplash) share one quota across every user.</p></div>
    </section>`;
  document.querySelectorAll('#admin-signup-form input[name="signup"]').forEach((input) => {
    input.onchange = () => act(async () => {
      await post("/api/admin/signup", { mode: input.value });
      toast(input.value === "open" ? "Anyone can make an account now" : "Sign-up is by invite only");
    });
  });
  $("admin-clear-failures")?.addEventListener("click", () => act(() => post("/api/admin/failures/clear", {})));
}

export async function refreshAdmin() {
  if (state.user?.role !== "admin") return;
  const [users, invites, audit, health] = await Promise.all([
    api("/api/admin/users"),
    api("/api/invites"),
    api("/api/admin/audit?limit=200"),
    api("/api/admin/health"),
  ]);
  renderUsers(users);
  renderInvites(invites);
  renderAudit(audit);
  renderServer(health);
  const me = users.find((user) => user.id === state.user.id);
  $("admin-2fa-hint").hidden = !me || me.two_factor || !me.has_password;
}

export function initAdmin() {
  document.querySelectorAll("[data-open-account]").forEach((button) => (button.onclick = () => showAccount()));
  $("admin-invite-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const made = await post("/api/invites", { email: $("admin-invite-email").value.trim(), role: $("admin-invite-role").value });
      $("admin-invite-form").reset();
      linkReveal(
        "admin-invite-reveal",
        `Invite for ${made.email}`,
        made.emailed ? "Emailed to them, and here to pass on. It works once, for 7 days." : "Pass it on: this server can't email. It works once, for 7 days.",
        made.link,
      );
      await refreshAdmin();
    } catch (error) {
      toast(error.message);
    }
  };
}
