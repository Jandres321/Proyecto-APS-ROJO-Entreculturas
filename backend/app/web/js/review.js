// Revisión: the list of expenses (left) and the detail of the selected one (right), with the two
// decisions. The verdict, the reasons and the decision come from the server; this module only
// shows them and sends the reviewer's decision.

import { api, fileUrl, jsonBody } from "./api.js";
import { launch } from "./jobs.js";
import { project, state } from "./state.js";
import { $, dateEs, esc, isTyping, listedAmount, num, say } from "./util.js";
import { mountViewer, stepViewer, unmountViewer } from "./viewer.js";

const TABS = [
  ["pending", "Por revisar"],
  ["reviewed", "Revisados"],
  ["ok", "Bien"],
  ["na", "No aplica"],
  ["all", "Todos"],
];

/** Which tab an expense belongs to (same split as the server's `counts`). */
export function bucket(e) {
  if (e.verdict.code === "ok") return "ok";
  if (e.verdict.code === "na") return "na";
  return e.decision.status === "pending" ? "pending" : "reviewed";
}

export function countsOf(expenses) {
  const out = { pending: 0, reviewed: 0, ok: 0, na: 0 };
  for (const e of expenses) out[bucket(e)]++;
  return out;
}

function statusOf(e) {
  if (e.decision.status === "correct") return ["green", "Revisado: correcto"];
  if (e.decision.status === "real_issue") return ["amber strong", "Incidencia real"];
  const b = bucket(e);
  if (b === "pending") return ["amber", e.decision.stale ? "Por revisar (decisión caducada)" : "Por revisar"];
  if (b === "ok") return ["green", "Bien"];
  return ["gray", "No aplica"];
}

const firstReason = (e) =>
  e.reasons.length ? e.reasons[0].label : e.verdict.code === "ok" ? "Comprobado en el PDF" : e.doc_status_label || "";

const byReportOrder = (a, b) => (a.source_row ?? 1e9) - (b.source_row ?? 1e9) || a.id - b.id;

function haystack(e) {
  return [
    e.order_no, e.issuer, e.concept, e.partida_code, e.invoice_no,
    ...e.reasons.map((r) => r.label),
    ...e.documents.map((d) => d.filename),
  ]
    .join(" ")
    .toLowerCase();
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.review.expenses
    .filter((e) => (state.tab === "all" || bucket(e) === state.tab) && (!q || haystack(e).includes(q)))
    .sort(byReportOrder);
}

const selectedExpense = () => state.review?.expenses.find((e) => e.id === state.selected) || null;

// --- loading -----------------------------------------------------------------------------------

const PENDING = { status: "pending", label: "Pendiente", note: null, decided_at: null, stale: false };

/** Empty values where an older API leaves out a field of the contract (it decides nothing). */
function normalize(e) {
  return {
    ...e,
    reasons: e.reasons || [],
    compare: e.compare || [],
    decision: e.decision || PENDING,
    documents: e.documents || [],
    segments: e.segments || [],
    findings: e.findings || [],
  };
}

export async function loadReview() {
  const p = project();
  state.review = null;
  state.counts = null;
  if (!p || !p.loaded) return;
  const review = await api(`/projects/${encodeURIComponent(p.key)}/review`);
  if (p.key !== state.key) return;
  review.expenses = review.expenses.map(normalize);
  state.review = review;
  state.counts = review.counts || countsOf(review.expenses);
}

// --- painting ----------------------------------------------------------------------------------

export function renderReview() {
  const p = project();
  const loaded = Boolean(p && p.loaded && state.review);
  $("revSplit").hidden = !loaded;
  $("revTools").hidden = !loaded;
  $("revEmpty").hidden = loaded;
  $("revTitle").textContent = p ? p.name : "Revisión";
  if (!loaded) {
    unmountViewer();
    $("revEmpty").innerHTML = !p
      ? '<p>No hay ningún proyecto. Crea uno en <a href="#/-/nuevo">Nuevo proyecto</a>.</p>'
      : p.loaded
        ? "<p>Cargando…</p>"
        : `<p>Este proyecto aún no se ha analizado.</p>
           <p><button type="button" class="btn primary" id="emptyRunBtn">Analizar ahora</button></p>
           <p class="status" id="runStatusEmpty" hidden></p>`;
    $("emptyRunBtn")?.addEventListener("click", () => launch(false));
    return;
  }
  const excel = fileUrl(`/projects/${encodeURIComponent(p.key)}/report.xlsx`);
  const link = $("excelBtn");
  link.hidden = !excel;
  if (excel) link.href = excel;
  const list = visible();
  if (!list.some((e) => e.id === state.selected)) state.selected = list[0]?.id ?? null;
  renderTabs();
  renderList(list);
  renderDetail();
}

function renderTabs() {
  const c = state.counts;
  const n = { ...c, all: state.review.expenses.length };
  $("tabs").innerHTML = TABS.map(
    ([key, label]) =>
      `<button type="button" role="tab" class="tab" data-tab="${key}" aria-selected="${state.tab === key}">${label} <span class="count">${n[key] ?? 0}</span></button>`,
  ).join("");
}

function rowHtml(e) {
  const [cls, text] = statusOf(e);
  const selected = e.id === state.selected;
  return `<div class="row${selected ? " selected" : ""}" role="option" aria-selected="${selected}" data-id="${e.id}">
    <span class="r-order">${esc(e.order_no || "s/n")}</span>
    <span class="r-issuer" title="${esc(e.issuer || "")}">${esc(e.issuer || "Sin proveedor")}</span>
    <span class="r-amount num">${esc(listedAmount(e))}</span>
    <span class="tag ${cls}">${esc(text)}</span>
    <span class="r-reason">${esc(firstReason(e))}</span>
  </div>`;
}

function renderList(list = visible()) {
  const box = $("list");
  box.innerHTML = list.length
    ? list.map(rowHtml).join("")
    : `<p class="muted pad">${state.query ? "Ningún gasto coincide con la búsqueda." : state.tab === "pending" ? "No queda nada por revisar." : "No hay gastos en esta pestaña."}</p>`;
  $("listCount").textContent = `${list.length} de ${state.review.expenses.length} gastos`;
  box.querySelector(".row.selected")?.scrollIntoView({ block: "nearest" });
}

function compareHtml(rows) {
  if (!rows?.length) return '<p class="muted">Sin datos que comparar.</p>';
  const match = (m) =>
    m === true ? '<span class="tag green">Sí</span>' : m === false ? '<span class="tag amber strong">No</span>' : '<span class="muted">—</span>';
  return `<table class="grid compare"><thead><tr><th>Campo</th><th>Listado</th><th>Leído en el PDF</th><th>Coincide</th></tr></thead><tbody>
    ${rows
      .map(
        (r) => `<tr>
          <th scope="row">${esc(r.label)}</th>
          <td>${r.listed === null || r.listed === undefined || r.listed === "" ? '<span class="muted">—</span>' : esc(r.listed)}</td>
          <td class="${r.read === null || r.read === undefined || r.read === "" ? "unread" : ""}">${r.read === null || r.read === undefined || r.read === "" ? "No leído" : esc(r.read)}</td>
          <td>${match(r.match)}</td></tr>`,
      )
      .join("")}</tbody></table>`;
}

function reasonsHtml(e) {
  if (!e.reasons.length) {
    return `<p class="muted">${e.verdict.code === "na" ? "No necesita revisión." : "Ninguno: el justificante se ha comprobado."}</p>`;
  }
  return `<ul class="reasons">${e.reasons
    .map((r) => `<li>${esc(r.label)}${r.blocking ? ' <span class="tag amber strong">Bloqueante</span>' : ""}</li>`)
    .join("")}</ul>`;
}

/** A finding as a sentence, with its raw data folded away. `extra` is trusted HTML. */
export function findingHtml(f, extra = "") {
  const where = [f.document_name ? esc(f.document_name) : "", f.page ? `página ${f.page}` : ""]
    .filter(Boolean)
    .join(", ");
  return `<li class="finding">
    <p><strong>${esc(f.label)}.</strong> ${esc(f.message)}</p>
    <p class="muted small">Gravedad ${esc(f.severity_label)} · ${esc(f.status_label)}${where ? " · " + where : ""}</p>
    <details><summary>Datos técnicos</summary><pre>${esc(JSON.stringify({ regla: f.rule_code, evidencia: f.evidence }, null, 2))}</pre></details>
    ${extra}
  </li>`;
}

function documentsHtml(e) {
  if (!e.documents.length) return "";
  return `<h3>Justificantes</h3><ul class="docs">${e.documents
    .map(
      (d) => `<li><span class="doc-name">${esc(d.filename)}</span>
        <span class="muted small">${esc(d.method_label || "")}${d.verified ? " · verificado por contenido" : " · sin verificar por contenido"}${d.reason ? " · " + esc(d.reason) : ""}</span></li>`,
    )
    .join("")}</ul>`;
}

function decidedLine(e) {
  const d = e.decision;
  if (d.stale) {
    return `<p class="notice">Ya se decidió este gasto, pero lo leído o encontrado ha cambiado desde entonces: vuelve a decidir.${d.note ? ` Nota anterior: «${esc(d.note)}».` : ""}</p>`;
  }
  if (d.status === "pending") return "";
  const when = d.decided_at ? ` el ${dateEs(d.decided_at)}` : "";
  return `<p class="decided">Decidido${when}: <strong>${esc(d.label || (d.status === "correct" ? "Correcto" : "Incidencia real"))}</strong>.
    <button type="button" class="link" data-act="pending">Volver a pendiente</button></p>`;
}

function startPage(e) {
  const doc = e.documents.find((d) => d.openable) || e.documents[0];
  if (!doc) return {};
  const f = e.findings.find((x) => x.document_id === doc.id && x.page);
  const s = e.segments.find((x) => x.document_id === doc.id);
  return { documentId: doc.id, page: f?.page || s?.pagina_inicio || 1 };
}

function renderDetail(message) {
  const box = $("detail");
  const e = selectedExpense();
  if (!e) {
    unmountViewer();
    box.innerHTML = '<p class="muted pad">Elige un gasto de la lista.</p>';
    return;
  }
  const blocking = e.reasons.some((r) => r.blocking);
  const [cls, text] = statusOf(e);
  const eur = e.amount_local !== null && e.amount_local !== undefined && e.amount_eur !== null ? `${num(e.amount_eur)} EUR` : "";
  const note = e.decision.note || "";
  box.innerHTML = `
    <div class="detail-scroll">
      <header class="d-head">
        <div>
          <h2>Gasto ${esc(e.order_no || "s/n")} · ${esc(e.issuer || "Sin proveedor")}</h2>
          <p class="muted">${esc(e.concept || "")}${e.partida_code ? ` · Partida ${esc(e.partida_code)}` : ""}${e.date ? ` · ${dateEs(e.date)}` : ""}${e.invoice_no ? ` · Factura ${esc(e.invoice_no)}` : ""}</p>
        </div>
        <div class="d-amount num">${esc(listedAmount(e))}${eur ? `<span class="muted small">${eur}</span>` : ""}</div>
        <span class="tag ${cls}">${esc(text)}</span>
      </header>
      ${decidedLine(e)}
      <div class="d-grid">
        <div class="d-info">
          <h3>Informe frente a PDF</h3>
          ${compareHtml(e.compare)}
          <h3>Motivos</h3>
          ${reasonsHtml(e)}
          <h3>Incidencias</h3>
          ${e.findings.length ? `<ul class="findings">${e.findings.map((f) => findingHtml(f)).join("")}</ul>` : '<p class="muted">Ninguna.</p>'}
          ${documentsHtml(e)}
        </div>
        <div class="d-viewer" id="viewer"></div>
      </div>
    </div>
    <footer class="d-actions">
      <label class="note-label" for="note">Nota${blocking ? " (obligatoria para «Correcto»: hay un motivo bloqueante)" : " (opcional)"}</label>
      <textarea id="note" rows="2" maxlength="2000">${esc(note)}</textarea>
      <div class="d-buttons">
        <button type="button" class="btn primary" data-act="correct">Correcto <kbd>C</kbd></button>
        <button type="button" class="btn" data-act="real_issue">Incidencia real <kbd>I</kbd></button>
        <span class="status" id="saveStatus" hidden></span>
      </div>
    </footer>`;
  if (message) say($("saveStatus"), message, "ok");
  mountViewer($("viewer"), e.documents, startPage(e));
}

// --- selecting and deciding --------------------------------------------------------------------

function select(id, { scroll = true } = {}) {
  if (id === state.selected) return;
  state.selected = id;
  for (const row of $("list").querySelectorAll(".row")) {
    const on = Number(row.dataset.id) === id;
    row.classList.toggle("selected", on);
    row.setAttribute("aria-selected", String(on));
    if (on && scroll) row.scrollIntoView({ block: "nearest" });
  }
  history.replaceState(null, "", `#/${encodeURIComponent(state.key)}/revision/${id}`);
  renderDetail();
}

function move(delta) {
  const list = visible();
  if (!list.length) return;
  const i = list.findIndex((e) => e.id === state.selected);
  const next = list[Math.min(Math.max(i + delta, 0), list.length - 1)] || list[0];
  select(next.id);
}

/** The next pending expense after position `index` of `list`, wrapping round. */
function nextPending(list, index, skipId) {
  const ordered = [...list.slice(index + 1), ...list.slice(0, Math.max(index, 0))];
  const fresh = (x) => state.review.expenses.find((e) => e.id === x.id);
  return ordered.map(fresh).find((e) => e && e.id !== skipId && bucket(e) === "pending") || null;
}

async function decide(status) {
  const e = selectedExpense();
  if (!e || state.saving) return;
  const noteEl = $("note");
  const statusEl = $("saveStatus");
  const note = noteEl.value.trim();
  if (status === "correct" && !note && e.reasons.some((r) => r.blocking)) {
    say(statusEl, "Hay un motivo bloqueante: escribe en la nota por qué es correcto.", "err");
    noteEl.focus();
    return;
  }
  const before = visible();
  const index = before.findIndex((x) => x.id === e.id);
  state.saving = true;
  const buttons = $("detail").querySelectorAll("[data-act]");
  for (const b of buttons) b.disabled = true;
  say(statusEl, "Guardando…");
  let updated;
  try {
    updated = await api(`/expenses/${e.id}/decision`, jsonBody("PATCH", { status, note: note || null }));
  } catch (error) {
    // Not saved: stay on this expense and say why.
    say(statusEl, `No se ha guardado: ${error.message}`, "err");
    for (const b of buttons) b.disabled = false;
    state.saving = false;
    return;
  }
  state.saving = false;
  const at = state.review.expenses.findIndex((x) => x.id === e.id);
  state.review.expenses[at] = normalize(updated);
  state.counts = countsOf(state.review.expenses);
  if (status === "pending") {
    renderTabs();
    renderList();
    renderDetail("Vuelve a estar pendiente.");
    return;
  }
  const done = `Guardado: gasto ${updated.order_no || ""} ${status === "correct" ? "correcto" : "con incidencia real"}.`;
  let next = nextPending(before, index, e.id);
  if (!next) {
    next = state.review.expenses.filter((x) => bucket(x) === "pending").sort(byReportOrder)[0] || null;
    if (next) {
      state.tab = "pending";
      state.query = "";
      $("q").value = "";
    }
  }
  if (next) {
    state.selected = next.id;
    history.replaceState(null, "", `#/${encodeURIComponent(state.key)}/revision/${next.id}`);
  }
  renderTabs();
  renderList();
  renderDetail(next ? done : `${done} No queda ningún gasto por revisar.`);
}

// --- events (attached once) --------------------------------------------------------------------

export function setupReview() {
  $("tabs").addEventListener("click", (ev) => {
    const tab = ev.target.closest("[data-tab]")?.dataset.tab;
    if (!tab || tab === state.tab) return;
    state.tab = tab;
    renderReview();
  });
  $("q").addEventListener("input", (ev) => {
    state.query = ev.target.value;
    renderReview();
  });
  $("list").addEventListener("click", (ev) => {
    const row = ev.target.closest(".row");
    if (row) select(Number(row.dataset.id), { scroll: false });
  });
  $("detail").addEventListener("click", (ev) => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (act) decide(act);
  });
  document.addEventListener("keydown", (ev) => {
    if (state.section !== "revision" || !state.review) return;
    if (ev.key === "Escape" && isTyping(ev)) {
      ev.target.blur();
      return;
    }
    if (isTyping(ev) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    const actions = {
      j: () => move(1),
      ArrowDown: () => move(1),
      k: () => move(-1),
      ArrowUp: () => move(-1),
      c: () => decide("correct"),
      i: () => decide("real_issue"),
      ArrowLeft: () => stepViewer(-1),
      ArrowRight: () => stepViewer(1),
    };
    if (!actions[key]) return;
    ev.preventDefault();
    actions[key]();
  });
}

/** Select an expense from the address (#/<project>/revision/<id>). */
export function selectFromAddress(id) {
  if (!state.review || !id) return;
  const e = state.review.expenses.find((x) => x.id === id);
  if (!e) return;
  state.selected = id;
  if (state.tab !== "all" && bucket(e) !== state.tab) state.tab = bucket(e);
}
