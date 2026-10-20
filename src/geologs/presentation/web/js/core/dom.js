// DOM helpers: element lookup, HTML escaping, toasts, and the clipboard.
export const $ = (id) => document.getElementById(id);

// A short message; with an action ({ label, run }) it offers a button such as Undo and stays a little longer.
export function toast(message, action) {
  const box = $("toast");
  box.textContent = message;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.onclick = () => {
      box.classList.remove("show");
      action.run();
    };
    box.append(button);
  }
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), action ? 8000 : 3800);
}

// Close a modal dialog when a press both starts and ends on the backdrop around it. Looking at the click
// alone closes it when a press that began inside (selecting text, dragging) is let go outside.
export function closeOnBackdrop(dialog, close = () => dialog.close()) {
  const outside = (event) => {
    const box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  };
  let pressedOutside = false;
  dialog.addEventListener("pointerdown", (event) => {
    pressedOutside = event.target === dialog && outside(event);
  });
  dialog.addEventListener("click", (event) => {
    if (pressedOutside && event.target === dialog && outside(event)) close();
    pressedOutside = false;
  });
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
