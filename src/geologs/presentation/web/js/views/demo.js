// Demo mode (GEOLOGS_DEMO): the strip saying this archive is a fictional, read-only demo, and leaving
// it. "Try the demo" on the sign-in screen is in auth.js.
import { $ } from "../core/dom.js";
import { signOut } from "./account.js";

// `demo` is /api/me's: true while looking at the demo, false for a real account.
export function renderDemo(demo) {
  document.body.classList.toggle("demo", !!demo);
  $("demo-bar").hidden = !demo;
}

export function initDemo() {
  $("demo-leave").onclick = () => signOut("");
}
