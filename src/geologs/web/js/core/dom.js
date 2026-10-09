// DOM helpers: element lookup, HTML escaping, toasts, and the clipboard.
export const $ = (id) => document.getElementById(id);

export function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 3800);
}

export function safe(text) {
  return String(text ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

export async function copyText(value, label = "Copied") {
  try {
    await navigator.clipboard.writeText(value);
    toast(label);
  } catch (_error) {
    toast(value);
  }
}
