// Server requests, the connection indicator, and the sign-in prompt.
import { $ } from "./dom.js";
import { state } from "./state.js";

// Ask for sign-in; views/auth.js shows the sign-in screen (unless a reset or invite form is open).
export function openAuth(message = "") {
  if (state.authDismissed) return;
  document.dispatchEvent(new CustomEvent("geologs:sign-in", { detail: { message } }));
}

export function setConnection(tone, text, detail = "") {
  const el = $("connection");
  el.dataset.state = tone;
  el.textContent = text;
  el.title = detail || text;
}

export async function api(path, options = {}) {
  const previousToken = sessionStorage.getItem("geologs-token");
  const headers = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(previousToken ? { Authorization: "Bearer " + previousToken } : {}),
    ...(options.headers || {}),
  };
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  } catch (_error) {
    setConnection("error", "Offline", "The GeoLogs server cannot be reached.");
    throw Error("Cannot reach the GeoLogs server. Check your connection.");
  }
  if (response.status === 401) {
    sessionStorage.removeItem("geologs-token");
    setConnection("warning", "Sign in required");
    openAuth(previousToken ? "The previously saved token was rejected. Sign in again." : "");
    throw Error(previousToken ? "Previously saved access token was rejected." : "Sign in required.");
  }
  let value = {};
  try {
    value = await response.json();
  } catch (_error) {}
  if (!response.ok) {
    if (response.status === 503) setConnection("error", "Database unavailable", value.error);
    throw Error(value.error || `HTTP ${response.status}`);
  }
  // sw.js answered from what this browser kept, because the server cannot be reached.
  if (response.headers.get("X-GeoLogs-Offline")) setConnection("warning", "Offline", "Showing what this browser kept from your last visit.");
  else setConnection("ok", "Connected");
  return value;
}

export async function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}
