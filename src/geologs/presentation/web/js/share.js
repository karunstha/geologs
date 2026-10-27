// A shared replay's page (share.html, at /s/<token>): it plays the one day its link was made for, frozen
// as it was then, to anyone holding the link. Nothing here is signed in, and nothing is kept.
import { $, safe } from "./core/dom.js";
import { state } from "./core/state.js";
import { openReplay } from "./replay/player.js";

function say(title, text) {
  $("share-message").innerHTML = `<h1>${safe(title)}</h1><p>${safe(text)}</p>`;
}

async function play() {
  const token = location.pathname.split("/")[2] || "";
  let response;
  try {
    // No cookies: a link plays the same for its owner as for anyone else.
    response = await fetch(`/api/shared/${encodeURIComponent(token)}`, { credentials: "omit", cache: "no-store" });
  } catch (_error) {
    say("Can't reach the server", "Check your connection, then reload the page.");
    return;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    say("This replay isn't available", data.error || "The link may be mistyped.");
    return;
  }
  // Distances in the units its owner reads them in.
  state.settings = { units: data.units };
  document.title = `${data.title} · Replay`;
  const problem = openReplay(data, { title: data.title, standalone: true });
  if (problem) say("This replay can't play here", problem);
}

play();
