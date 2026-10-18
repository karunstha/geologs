// The account page: your name and email, your password, the browsers you're signed in on, and
// personal API tokens. Opened from the account button at the top right.
import { api, openAuth, post, setConnection } from "../core/api.js";
import { $, copyText, safe, toast } from "../core/dom.js";
import { ago, zoneOptions } from "../core/format.js";
import { state } from "../core/state.js";
import { onEach } from "../ui/actions.js";
import { statusPill } from "../ui/icons.js";
import { showView } from "./shell.js";

// "Chrome on macOS" from a browser's user-agent string; what its owner would call it.
export function browserName(agent) {
  if (!agent) return "A browser";
  const browser = /Edg\//.test(agent)
    ? "Edge"
    : /Firefox\//.test(agent)
      ? "Firefox"
      : /Chrome\//.test(agent)
        ? "Chrome"
        : /Safari\//.test(agent)
          ? "Safari"
          : /curl\//.test(agent)
            ? "curl"
            : "A browser";
  const system = /iPhone/.test(agent)
    ? "iPhone"
    : /iPad/.test(agent)
      ? "iPad"
      : /Android/.test(agent)
        ? "Android"
        : /Mac OS X|Macintosh/.test(agent)
          ? "macOS"
          : /Windows/.test(agent)
            ? "Windows"
            : /Linux/.test(agent)
              ? "Linux"
              : "";
  return system ? `${browser} on ${system}` : browser;
}

function shortDate(value) {
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zoneOptions() });
}

function renderAccount() {
  const data = state.account;
  if (!data) return;
  const { user } = data;
  $("account-name").value = user.name || "";
  $("account-email").textContent = user.email || "No email yet: add one below to sign in with a password.";
  $("account-email-state").innerHTML = user.email
    ? user.email_verified
      ? statusPill("success", "Confirmed")
      : statusPill("warning", "Not confirmed")
    : "";
  $("account-verify").hidden = !(user.email && !user.email_verified && data.can_email);
  document.querySelectorAll("#account-view [data-needs-password]").forEach((el) => (el.hidden = !user.has_password));
  $("account-current-password").required = user.has_password;
  $("account-email-password").required = user.has_password;
  $("account-password-note").textContent = user.has_password
    ? "At least 10 characters. Changing it signs out your other browsers."
    : "Your account has no password yet: you sign in with the server's access token. Choose one to sign in with your email.";
  $("account-password-save").textContent = user.has_password ? "Change password" : "Set password";
  $("account-email-summary").textContent = user.email ? "Change your email" : "Add your email";
  $("account-email-save").textContent = user.email ? "Change email" : "Add email";
  if (!user.email) $("account-email-change").open = true;

  const two = data.two_factor;
  $("account-2fa-state").innerHTML = two.enabled ? statusPill("success", "On") : statusPill("neutral", "Off");
  $("account-2fa-off").hidden = two.enabled || !$("account-2fa-setup").hidden;
  $("account-2fa-on").hidden = !two.enabled;
  $("account-2fa-note").textContent = two.enabled
    ? `Signing in asks for a code from your authenticator app. ${two.recovery_codes_left} recovery code${two.recovery_codes_left === 1 ? "" : "s"} left.`
    : "After your password, ask for a code from an authenticator app on your phone.";
  if (!user.has_password) {
    $("account-2fa-off").hidden = true;
    $("account-2fa-note").textContent = "Set a password first: two-factor sign-in adds a code to it.";
  }

  const others = data.sessions.filter((item) => !item.current);
  $("account-end-others").hidden = !others.length;
  $("account-sessions").innerHTML =
    data.sessions
      .map(
        (item) =>
          `<li class="list-item"><div class="place-main"><span class="health-dot ${item.current ? "success" : "neutral"}" aria-hidden="true"></span><div><strong>${safe(browserName(item.user_agent))}${item.current ? ' <span class="muted">· this browser</span>' : ""}</strong><small>${safe(`last used ${ago(item.last_seen_at)} · signed in ${shortDate(item.created_at)}`)}</small></div></div>${item.current ? "" : `<button type="button" class="button secondary small" data-end-session="${safe(item.id)}">Sign out</button>`}</li>`,
      )
      .join("") ||
    '<li class="empty-block"><strong>Signed in with a token</strong><span>This browser uses the server token or an API token, not a sign-in of its own.</span></li>';
  onEach($("account-sessions"), "end-session", async (id) => {
    try {
      await post("/api/account/sessions/end", { id });
      await refreshAccount();
      toast("That browser is signed out");
    } catch (error) {
      toast(error.message);
    }
  });

  $("account-tokens").innerHTML =
    data.api_tokens
      .map(
        (token) =>
          `<li class="list-item"><div class="place-main"><span class="health-dot ${token.last_used_at && Date.now() - Date.parse(token.last_used_at) < 86400e3 ? "success" : "neutral"}" aria-hidden="true"></span><div><strong>${safe(token.name)}</strong><small>${safe([token.hint, `created ${shortDate(token.created_at)}`, token.last_used_at ? `last used ${ago(token.last_used_at)}` : "never used"].join(" · "))}</small></div></div><button type="button" class="button secondary small danger-text" data-revoke-api-token="${token.id}">Revoke</button></li>`,
      )
      .join("") ||
    '<li class="empty-block"><strong>No API tokens</strong><span>Create one for an MCP client or a script that reads your history.</span></li>';
  onEach($("account-tokens"), "revoke-api-token", async (id) => {
    const token = data.api_tokens.find((item) => item.id === +id);
    if (!confirm(`Revoke “${token?.name}”? Anything using it stops working at once.`)) return;
    try {
      await post("/api/account/tokens/delete", { id: +id });
      await refreshAccount();
      toast("Token revoked");
    } catch (error) {
      toast(error.message);
    }
  });
}

export async function refreshAccount() {
  state.account = await api("/api/account");
  renderAccount();
}

export function showAccount() {
  showView("account");
  refreshAccount().catch((error) => toast(error.message));
}

function submit(id, run) {
  $(id).onsubmit = async (event) => {
    event.preventDefault();
    try {
      await run();
    } catch (error) {
      toast(error.message);
    }
  };
}

export async function signOut(message = "You're signed out.") {
  try {
    await fetch("/api/session", { method: "DELETE", credentials: "same-origin" });
  } catch (_error) {}
  sessionStorage.removeItem("geologs-token");
  state.user = null;
  state.authDismissed = false;
  setConnection("warning", "Signed out");
  openAuth(message);
}

function showRecoveryCodes(codes) {
  const box = $("account-2fa-codes");
  box.hidden = false;
  box.innerHTML = `<strong>Two-factor sign-in is on. Keep these recovery codes somewhere safe</strong><span>Each signs you in once if you lose your phone. They are shown only now.</span><pre class="recovery-codes">${codes.map(safe).join("\n")}</pre><div class="copy-field"><button type="button" class="button secondary small" data-copy-codes>Copy codes</button><button type="button" class="button ghost small" data-hide-codes>Done</button></div>`;
  box.querySelector("[data-copy-codes]").onclick = () => copyText(codes.join("\n"), "Recovery codes copied");
  box.querySelector("[data-hide-codes]").onclick = () => {
    box.hidden = true;
    box.innerHTML = "";
  };
}

export function initAccount() {
  $("account-2fa-begin").onclick = async () => {
    try {
      const setup = await post("/api/account/2fa/begin", {});
      $("account-2fa-secret").value = setup.secret.replace(/(.{4})/g, "$1 ").trim();
      $("account-2fa-link").href = setup.uri;
      $("account-2fa-setup").hidden = false;
      $("account-2fa-off").hidden = true;
      $("account-2fa-code").focus();
    } catch (error) {
      toast(error.message);
    }
  };
  $("account-2fa-copy").onclick = () => copyText($("account-2fa-secret").value.replace(/\s/g, ""), "Key copied");
  submit("account-2fa-setup", async () => {
    const result = await post("/api/account/2fa/confirm", { code: $("account-2fa-code").value.trim() });
    $("account-2fa-setup").reset();
    $("account-2fa-setup").hidden = true;
    showRecoveryCodes(result.recovery_codes);
    await refreshAccount();
  });
  submit("account-2fa-on", async () => {
    if (!confirm("Turn off two-factor sign-in? Signing in will need only your password.")) return;
    await post("/api/account/2fa/stop", { current: $("account-2fa-password").value });
    $("account-2fa-on").reset();
    await refreshAccount();
    toast("Two-factor sign-in is off");
  });
  $("account-sign-out").onclick = () => signOut();
  submit("account-name-form", async () => {
    state.user = await post("/api/account", { name: $("account-name").value.trim() });
    await refreshAccount();
    toast("Name saved");
  });
  submit("account-email-form", async () => {
    const result = await post("/api/account/email", {
      email: $("account-new-email").value.trim(),
      current: $("account-email-password").value,
    });
    $("account-email-form").reset();
    $("account-email-change").open = false;
    await refreshAccount();
    toast(result.verification_sent ? "Email changed. Check it for a confirmation link." : "Email changed");
  });
  submit("account-password-form", async () => {
    if ($("account-new-password").value !== $("account-new-again").value) throw Error("The two new passwords differ");
    await post("/api/account/password", {
      current: $("account-current-password").value,
      password: $("account-new-password").value,
    });
    $("account-password-form").reset();
    await refreshAccount();
    toast("Password saved. Your other browsers are signed out.");
  });
  $("account-verify").onclick = async () => {
    try {
      const result = await post("/api/email/verify/send", {});
      toast(result.sent ? "Check your email for the confirmation link" : "This server can't send email");
    } catch (error) {
      toast(error.message);
    }
  };
  $("account-end-others").onclick = async () => {
    if (!confirm("Sign out every other browser? This one stays signed in.")) return;
    try {
      await post("/api/account/sessions/end", {});
      await refreshAccount();
      toast("Signed out everywhere else");
    } catch (error) {
      toast(error.message);
    }
  };
  submit("account-token-form", async () => {
    const created = await post("/api/account/tokens", { name: $("account-token-name").value.trim() });
    $("account-token-form").reset();
    const reveal = $("account-token-reveal");
    reveal.hidden = false;
    reveal.innerHTML = `<strong>Copy the token for ${safe(created.name)} now</strong><span>It is shown only once. Send it as a bearer token: <code>Authorization: Bearer …</code></span><div class="copy-field"><input class="input mono" readonly value="${safe(created.token)}" aria-label="New token"><button type="button" class="button secondary small" data-copy-new-token>Copy</button><button type="button" class="button ghost small" data-hide-token>Done</button></div>`;
    reveal.querySelector("[data-copy-new-token]").onclick = () => copyText(created.token, "Token copied");
    reveal.querySelector("[data-hide-token]").onclick = () => {
      reveal.hidden = true;
      reveal.innerHTML = "";
    };
    await refreshAccount();
  });
}
