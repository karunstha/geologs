// Signing in (an email and password, or the server's access token), and the pages links in emails
// open: choosing a new password, accepting an invite, and confirming an address.
import { refreshAll } from "../app/refresh.js";
import { $, toast } from "../core/dom.js";
import { state } from "../core/state.js";
import { clearAccount, refreshAccount, showAccount } from "./account.js";

let mode = "signin";
let linkToken = null; // a reset or invite token, kept only in memory once read from the address bar
let options = { email: false, token: true, signup: false };

async function send(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let value = {};
  try {
    value = await response.json();
  } catch (_error) {}
  if (!response.ok) {
    const failure = Error(value.error || `HTTP ${response.status}`);
    failure.needsCode = !!value.needs_code;
    throw failure;
  }
  return value;
}

function form(name) {
  return document.querySelector(`.auth-mode[data-auth-mode="${name}"]`);
}

function show(name, message = "") {
  mode = name;
  $("auth-panel").hidden = false;
  document.querySelectorAll(".auth-mode").forEach((el) => {
    el.hidden = el.dataset.authMode !== name;
    const error = el.querySelector(".auth-error");
    if (error) error.textContent = el.dataset.authMode === name ? message : "";
  });
  document.querySelectorAll("[data-needs-email]").forEach((el) => (el.hidden = !options.email));
  document.querySelectorAll("[data-needs-token]").forEach((el) => (el.hidden = !options.token));
  document.querySelectorAll("[data-needs-signup]").forEach((el) => (el.hidden = !options.signup));
  // Only a signed-out browser that may still look around (none of the link pages) can be dismissed.
  document.querySelectorAll("[data-auth-cancel]").forEach((el) => (el.hidden = !state.user));
  form(name)?.querySelector("input:not([type=hidden])")?.focus();
}

function hide() {
  $("auth-panel").hidden = true;
  linkToken = null;
  mode = "signin";
  document.querySelectorAll(".auth-mode input").forEach((input) => (input.value = ""));
  $("auth-code-field").hidden = true;
}

async function signedIn(user) {
  sessionStorage.removeItem("geologs-token");
  state.user = user || null;
  state.authDismissed = false;
  hide();
  await refreshAll();
  // Signed out from the account page, a new sign-in lands back on it: show this person's account.
  if ($("account-view").classList.contains("active")) await refreshAccount().catch((failure) => toast(failure.message));
}

function error(name, message) {
  form(name).querySelector(".auth-error").textContent = message;
}

function bothMatch(first, second, name) {
  if ($(first).value !== $(second).value) {
    error(name, "The two passwords differ.");
    return false;
  }
  return true;
}

// A link from an email: #reset=…, #invite=… or #verify=…. The token leaves the address bar at once.
async function openLink() {
  const match = location.hash.match(/^#(reset|invite|verify)=([\w-]+)/);
  if (!match) return false;
  const [, kind, token] = match;
  history.replaceState(null, "", location.pathname + location.search);
  if (kind === "verify") {
    try {
      await send("/api/email/verify", { token });
      toast("Email confirmed");
    } catch (failure) {
      toast(failure.message);
    }
    return false;
  }
  linkToken = token;
  if (kind === "reset") {
    show("reset");
    return true;
  }
  const response = await fetch("/api/invite?token=" + encodeURIComponent(token), { credentials: "same-origin" });
  if (!response.ok) {
    linkToken = null;
    show("signin", "This invite has expired or was already used. Ask for a new one.");
    return true;
  }
  $("auth-invite-email").textContent = (await response.json()).email;
  show("invite");
  return true;
}

export async function initAuth() {
  document.addEventListener("geologs:sign-in", (event) => {
    // Background polls ask again every few seconds while signed out; a sign-in screen already open is
    // left as it is, so the cursor stays where the person is typing (and a reset or invite form, which
    // is what this visit is for, stays open).
    if (!$("auth-panel").hidden) return;
    state.user = null;
    clearAccount();
    let preferred = "signin";
    try {
      preferred = localStorage.getItem("geologs-sign-in") === "token" && options.token ? "token" : "signin";
    } catch (_error) {}
    show(preferred, event.detail?.message || "");
  });
  document.querySelectorAll("[data-auth-go]").forEach((button) => {
    button.onclick = () => {
      const target = button.dataset.authGo;
      if (target === "forgot") {
        $("auth-forgot-email").value = $("auth-email").value;
        form("forgot").querySelector(".auth-sent").hidden = true;
      }
      show(target);
    };
  });
  document.querySelectorAll("[data-auth-cancel]").forEach((button) => {
    button.onclick = () => {
      state.authDismissed = true;
      hide();
    };
  });
  form("signin").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const body = { email: $("auth-email").value.trim(), password: $("auth-password").value };
      if (!$("auth-code-field").hidden) body.code = $("auth-code").value.trim();
      const result = await send("/api/session", body);
      try {
        localStorage.setItem("geologs-sign-in", "email");
      } catch (_error) {}
      await signedIn(result.user);
    } catch (failure) {
      error("signin", failure.message);
      if (failure.needsCode) {
        // The password was right: the account also asks for its authenticator code.
        $("auth-code-field").hidden = false;
        $("auth-code").value = "";
        $("auth-code").focus();
      } else $("auth-password").select();
    }
  };
  form("token").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const result = await send("/api/session", { token: $("auth-token").value.trim() });
      try {
        localStorage.setItem("geologs-sign-in", "token");
      } catch (_error) {}
      await signedIn(result.user);
    } catch (failure) {
      error("token", failure.message);
    }
  };
  form("forgot").onsubmit = async (event) => {
    event.preventDefault();
    try {
      await send("/api/password/forgot", { email: $("auth-forgot-email").value.trim() });
      error("forgot", "");
      form("forgot").querySelector(".auth-sent").hidden = false;
    } catch (failure) {
      error("forgot", failure.message);
    }
  };
  form("reset").onsubmit = async (event) => {
    event.preventDefault();
    if (!bothMatch("auth-reset-password", "auth-reset-again", "reset")) return;
    try {
      const result = await send("/api/password/reset", { token: linkToken, password: $("auth-reset-password").value });
      linkToken = null;
      $("auth-email").value = result.user.email;
      show("signin");
      form("signin").querySelector(".auth-error").textContent = "";
      toast("Password changed. Sign in with it.");
      $("auth-password").focus();
    } catch (failure) {
      error("reset", failure.message);
    }
  };
  form("signup").onsubmit = async (event) => {
    event.preventDefault();
    if (!bothMatch("auth-signup-password", "auth-signup-again", "signup")) return;
    try {
      const result = await send("/api/signup", {
        email: $("auth-signup-email").value.trim(),
        password: $("auth-signup-password").value,
        name: $("auth-signup-name").value.trim(),
      });
      toast("Welcome to GeoLogs. Check your email to confirm your address.");
      await signedIn(result.user);
    } catch (failure) {
      error("signup", failure.message);
    }
  };
  form("invite").onsubmit = async (event) => {
    event.preventDefault();
    if (!bothMatch("auth-invite-password", "auth-invite-again", "invite")) return;
    try {
      const result = await send("/api/invites/accept", {
        token: linkToken,
        password: $("auth-invite-password").value,
        name: $("auth-invite-name").value.trim(),
      });
      toast("Welcome to GeoLogs");
      await signedIn(result.user);
    } catch (failure) {
      error("invite", failure.message);
    }
  };
  $("auth-button").onclick = () => {
    if (state.user) showAccount();
    else {
      state.authDismissed = false;
      document.dispatchEvent(new CustomEvent("geologs:sign-in", { detail: { message: "" } }));
    }
  };
  try {
    options = await (await fetch("/api/sign-in-options", { credentials: "same-origin" })).json();
  } catch (_error) {}
  // A link opened in a tab that already shows GeoLogs only changes the address's #part.
  window.addEventListener("hashchange", () => openLink().catch((failure) => toast(failure.message)));
  return openLink();
}
