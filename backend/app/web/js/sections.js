// The other sections: Nuevo proyecto, Presupuesto y topes, Incidencias generales and Ajustes.

import { api, isMock, jsonBody } from "./api.js";
import { launch, showJob } from "./jobs.js";
import { findingHtml } from "./review.js";
import { project, state } from "./state.js";
import { $, esc, num, say } from "./util.js";

let hooks = { refresh: async () => {}, go: () => {} };
let source = "upload";

const options = (items) => items.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");

/** Fill the selects that depend on the installation (donors, LLM modes). */
export function fillSettings() {
  const s = state.settings;
  $("newDonor").innerHTML = s.donors.map((d) => `<option value="${esc(d.value)}">${esc(d.label)}</option>`).join("");
  for (const id of ["newLlm", "setLlm"]) {
    $(id).innerHTML = options(s.llm_modes);
    // Without permission for an external AI the option exists but the analysis would refuse it.
    for (const opt of $(id).options) opt.disabled = opt.value !== "off" && !s.external_llm;
  }
  setSource(source);
}

// --- Nuevo proyecto ---------------------------------------------------------------------------

function setSource(next) {
  source = next;
  for (const b of document.querySelectorAll("[data-source]")) {
    b.setAttribute("aria-pressed", String(b.dataset.source === source));
  }
  $("uploadFields").hidden = source !== "upload";
  $("driveFields").hidden = source !== "drive";
  const s = state.settings;
  if (!s) return;
  let problem = s.data_dir_available ? "" : "La carpeta de datos (DATA_DIR) no está definida en el servidor.";
  if (!problem && source === "drive" && !s.drive.available) problem = s.drive.message;
  $("createBtn").disabled = Boolean(problem);
  say($("createStatus"), problem, problem ? "err" : "");
}

async function createProject(ev) {
  ev.preventDefault();
  const status = $("createStatus");
  const name = $("newName").value.trim();
  const start = $("newStart").value;
  const end = $("newEnd").value;
  if (!name) return say(status, "Falta el nombre del proyecto.", "err");
  if (!start || !end) return say(status, "Indica el inicio y el fin del periodo de ejecución.", "err");
  if (end < start) return say(status, "El fin del periodo es anterior al inicio.", "err");
  const common = { name, donor: $("newDonor").value, start_date: start, end_date: end, llm_mode: $("newLlm").value };
  $("createBtn").disabled = true;
  try {
    let created;
    if (source === "drive") {
      const url = $("driveUrl").value.trim();
      if (!url) throw new Error("Pega el enlace de la carpeta de Drive.");
      say(status, "Comprobando el acceso a la carpeta…");
      created = await api("/projects", jsonBody("POST", { ...common, source_type: "drive", drive_url: url, report_file: $("driveReport").value.trim() || null }));
    } else {
      const report = $("newReport").files[0];
      const docs = Array.from($("newDocs").files);
      if (!report) throw new Error("Elige el Excel del informe económico.");
      if (!docs.length) throw new Error("Elige los justificantes (PDF o un .zip).");
      const form = new FormData();
      for (const [k, v] of Object.entries(common)) form.append(k, v);
      form.append("report", report);
      for (const d of docs) form.append("documents", d);
      say(status, `Subiendo ${docs.length} archivo(s)…`);
      created = await api("/projects/upload", { method: "POST", body: form });
    }
    $("newForm").reset();
    say(status, "");
    await hooks.refresh();
    hooks.go(created.key, "revision");
  } catch (error) {
    say(status, error.message, "err");
  } finally {
    setSource(source);
  }
}

// --- Presupuesto y topes -----------------------------------------------------------------------

const TRAFFIC = { verde: ["green", "Bien"], amarillo: ["amber", "Revisar"], rojo: ["amber strong", "Desviación alta"] };

export async function renderBudget() {
  const p = project();
  const box = $("budgetBody");
  if (!p || !p.loaded || !state.review) {
    box.innerHTML = '<p class="muted">Analiza el proyecto para ver su presupuesto.</p>';
    return;
  }
  if (!state.budget) {
    box.innerHTML = '<p class="muted">Cargando…</p>';
    try {
      const budget = await api(`/projects/${encodeURIComponent(p.key)}/budget`);
      if (p.key !== state.key) return;
      state.budget = budget;
    } catch (error) {
      box.innerHTML = `<p class="error">No se ha podido cargar el presupuesto: ${esc(error.message)}</p>`;
      return;
    }
  }
  const b = state.budget;
  const pct = (v) => (v === null || v === undefined ? "—" : `${num(v * 100, 1)} %`);
  const rules = state.review.rules || [];
  box.innerHTML = `
    <h3>Aprobado frente a ejecutado por partida</h3>
    ${b.partidas.length ? `<table class="grid"><thead><tr><th>Partida</th><th>Nombre</th><th class="num">Aprobado</th><th class="num">Ejecutado (listado)</th><th class="num">Ejecutado (informe)</th><th class="num">Desviación</th><th class="num">%</th><th>Estado</th></tr></thead><tbody>
      ${b.partidas.map((r) => {
        const [cls, text] = TRAFFIC[r.traffic] || ["gray", r.traffic];
        return `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td class="num">${num(r.approved)}</td><td class="num">${num(r.executed)}</td><td class="num">${num(r.declared)}</td><td class="num">${num(r.deviation)}</td><td class="num">${pct(r.deviation_pct)}</td><td><span class="tag ${cls}">${esc(text)}</span></td></tr>`;
      }).join("")}</tbody></table>
      <p class="muted small">Importes en euros. Lo ejecutado se recalcula sumando el listado de gastos.</p>` : '<p class="muted">Este proyecto no tiene presupuesto cargado.</p>'}
    <h3>Topes del donante</h3>
    ${b.limits.length ? `<table class="grid"><thead><tr><th>Tope</th><th>Descripción</th><th class="num">Ejecutado</th><th class="num">Porcentaje</th><th class="num">Máximo %</th><th class="num">Máximo EUR</th><th>Estado</th></tr></thead><tbody>
      ${b.limits.map((c) => `<tr><td>${esc(c.code)}</td><td>${esc(c.description)}</td><td class="num">${num(c.executed)}</td><td class="num">${pct(c.pct)}</td><td class="num">${pct(c.max_pct)}</td><td class="num">${num(c.max_amount_eur)}</td><td>${c.exceeded ? '<span class="tag amber strong">Superado</span>' : '<span class="tag green">Dentro</span>'}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">Este donante no tiene topes definidos.</p>'}
    <h3>Reglas que se comprueban</h3>
    ${rules.length ? `<table class="grid"><thead><tr><th>Regla</th><th>Gravedad</th><th>Descripción</th><th>Referencia</th></tr></thead><tbody>
      ${rules.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.severity_label)}</td><td>${esc(r.description)}</td><td>${esc(r.reference || "")}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">Las reglas se cargan al analizar.</p>'}`;
}

// --- Incidencias generales ---------------------------------------------------------------------

export function renderGeneral() {
  const box = $("generalBody");
  const list = state.review?.general_findings || [];
  if (!state.review) {
    box.innerHTML = '<p class="muted">Analiza el proyecto para ver sus incidencias generales.</p>';
    return;
  }
  box.innerHTML = list.length
    ? `<ul class="findings general">${list
        .map(
          (f) =>
            findingHtml(
              f,
              `<div class="finding-actions" data-finding="${f.id}">
                <input type="text" aria-label="Nota" placeholder="Nota (opcional)" value="${esc(f.reviewer_note || "")}" maxlength="2000" />
                <button type="button" class="btn small" data-status="accepted" aria-pressed="${f.status === "accepted"}">Es real</button>
                <button type="button" class="btn small" data-status="false_positive" aria-pressed="${f.status === "false_positive"}">Falso positivo</button>
                ${f.status !== "open" ? '<button type="button" class="btn small" data-status="open">Reabrir</button>' : ""}
                <span class="status" hidden></span>
              </div>`,
            ),
        )
        .join("")}</ul>`
    : '<p class="muted">No hay incidencias de presupuesto, topes ni documentos sin gasto.</p>';
}

async function decideGeneral(ev) {
  const button = ev.target.closest("[data-status]");
  if (!button) return;
  const row = button.closest("[data-finding]");
  const f = state.review.general_findings.find((x) => x.id === Number(row.dataset.finding));
  const note = row.querySelector("input").value.trim() || null;
  const status = row.querySelector(".status");
  try {
    const updated = await api(`/findings/${f.id}`, jsonBody("PATCH", { status: button.dataset.status, reviewer_note: note }));
    const labels = { open: "abierta", accepted: "aceptada", false_positive: "falso positivo" };
    f.status = updated.status;
    f.status_label = updated.status_label || labels[updated.status];
    f.reviewer_note = updated.reviewer_note;
    renderGeneral();
  } catch (error) {
    say(status, `No se ha guardado: ${error.message}`, "err");
  }
}

// --- Ajustes -----------------------------------------------------------------------------------

export function renderSettings() {
  const p = project();
  $("settingsForm").hidden = !p;
  $("settingsNone").hidden = Boolean(p);
  if (!p) return;
  $("setStart").value = p.start_date || "";
  $("setEnd").value = p.end_date || "";
  $("setLlm").value = p.llm_mode;
  $("setReport").value = p.report_file || "";
  $("reread").checked = false;
  $("reread").disabled = !p.loaded;
  $("deleteBtn").hidden = p.is_sample;
  say($("settingsStatus"), "");
  showJob(p.job);
}

async function saveSettings(ev) {
  ev.preventDefault();
  const p = project();
  const start = $("setStart").value || null;
  const end = $("setEnd").value || null;
  if (start && end && end < start) return say($("settingsStatus"), "El fin del periodo es anterior al inicio.", "err");
  try {
    await api(`/projects/${encodeURIComponent(p.key)}`, jsonBody("PATCH", { start_date: start, end_date: end, llm_mode: $("setLlm").value, report_file: $("setReport").value.trim() || null }));
    await hooks.refresh();
    say($("settingsStatus"), "Guardado. Se aplica en el próximo análisis.", "ok");
  } catch (error) {
    say($("settingsStatus"), error.message, "err");
  }
}

async function deleteProject() {
  const p = project();
  if (!p || p.is_sample) return;
  if (!confirm(`¿Borrar «${p.name}» y todo lo revisado en él?`)) return;
  const files = p.source_type !== "local" && confirm("¿Borrar también sus archivos subidos o descargados? (Cancelar = conservarlos)");
  try {
    await api(`/projects/${encodeURIComponent(p.key)}?delete_files=${files}`, { method: "DELETE" });
    state.key = null;
    await hooks.refresh();
    hooks.go(state.key, "revision");
  } catch (error) {
    say($("settingsStatus"), `No se ha podido borrar: ${error.message}`, "err");
  }
}

// --- events (attached once) --------------------------------------------------------------------

export function setupSections(callbacks) {
  hooks = callbacks;
  for (const b of document.querySelectorAll("[data-source]")) {
    b.addEventListener("click", () => setSource(b.dataset.source));
  }
  $("newForm").addEventListener("submit", createProject);
  $("settingsForm").addEventListener("submit", saveSettings);
  $("runBtn").addEventListener("click", () => launch($("reread").checked));
  $("deleteBtn").addEventListener("click", deleteProject);
  $("generalBody").addEventListener("click", decideGeneral);
  if (isMock) $("mockUploadHint").hidden = false;
}
