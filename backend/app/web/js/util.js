// Small helpers shared by every module. Everything painted with innerHTML goes through esc().

export const $ = (id) => document.getElementById(id);

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ENTITIES[c]);

export function num(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  return Number(value).toLocaleString("es-ES", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Amount of the listing: local currency when there is one, else euros. */
export function listedAmount(e) {
  if (e.amount_local !== null && e.amount_local !== undefined) {
    return `${num(e.amount_local)} ${e.currency || ""}`.trim();
  }
  return e.amount_eur === null || e.amount_eur === undefined ? "—" : `${num(e.amount_eur)} EUR`;
}

export const dateEs = (iso) => (iso ? iso.slice(0, 10).split("-").reverse().join("/") : "");

/** True while the user is typing: shortcuts must not fire then. */
export function isTyping(event) {
  const el = event.target;
  if (!el || !(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  return !["checkbox", "radio", "button", "submit", "reset", "file"].includes(el.type);
}

/** One line of text in a status element; kind is "", "ok" or "err". */
export function say(el, message, kind = "") {
  el.textContent = message || "";
  el.className = "status" + (kind ? " " + kind : "");
  el.hidden = !message;
}
