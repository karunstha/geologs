// Signing in with the access token.
import { refreshAll } from "../app/refresh.js";
import { openAuth } from "../core/api.js";
import { $ } from "../core/dom.js";
import { state } from "../core/state.js";

export function initAuth() {
  $("auth-cancel").onclick = () => {
    state.authDismissed = true;
    $("auth-panel").hidden = true;
    $("auth-token").value = "";
  };
  $("auth-form").onsubmit = async (event) => {
    event.preventDefault();
    const token = $("auth-token").value.trim();
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || "Access token rejected");
      sessionStorage.removeItem("geologs-token");
      $("auth-token").value = "";
      $("auth-panel").hidden = true;
      state.authDismissed = false;
      await refreshAll();
    } catch (error) {
      $("auth-error").textContent = error.message;
    }
  };
  $("auth-button").onclick = () => {
    state.authDismissed = false;
    openAuth();
  };
}
