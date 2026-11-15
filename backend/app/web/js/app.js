// Entry point: sidebar, address (#/<project>/<section>[/<expense id>]) and start-up.

import { api, isMock } from "./api.js";
import { onJobFinished, showJob } from "./jobs.js";
import { loadReview, renderReview, selectFromAddress, setupReview } from "./review.js";
import { fillSettings, renderBudget, renderGeneral, renderSettings, setupSections } from "./sections.js";
import { SECTIONS, project, state } from "./state.js";
import { $, esc, say } from "./util.js";

function fatal(message) {
  say($("fatal"), message, "err");
}

// --- address -----------------------------------------------------------------------------------

function parseHash() {
  const [key, section, id] = location.hash
    .replace(/^#\/?/, "")
    .split("/")
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch {
        return "";
      }
    });
  return {
    key: key && key !== "-" ? key : null,
    section: Object.hasOwn(SECTIONS, section) ? section : "revision",
    id: Number(id) || null,
  };
}

const hashFor = (key, section, id) =>
  `#/${encodeURIComponent(key || "-")}/${section}${id ? `/${id}` : ""}`;

/** Go to a project and section (the address changes, so the browser's back button works). */
function go(key, section, id = null) {
  const target = hashFor(key, section, id);
  if (location.hash === target) route();
  else location.hash = target;
}

async function route() {
  const target = parseHash();
  const known = (key) => state.projects.some((p) => p.key === key);
  const key = known(target.key) ? target.key : state.key;
  if (key !== state.key || (key && !state.review && project()?.loaded)) await selectProject(key);
  state.section = target.section;
  if (target.id && target.section === "revision") selectFromAddress(target.id);
  const canonical = hashFor(state.key, state.section, target.section === "revision" ? target.id : null);
  if (location.hash !== canonical) history.replaceState(null, "", canonical);
  render();
}

async function selectProject(key) {
  state.key = key;
  state.review = null;
  state.counts = null;
  state.budget = null;
  state.selected = null;
  state.tab = "pending";
  state.query = "";
  $("q").value = "";
  try {
    await loadReview();
    fatal("");
  } catch (error) {
    fatal(`No se han podido cargar los gastos del proyecto: ${error.message}`);
  }
}

// --- painting ----------------------------------------------------------------------------------

function aiLine(p) {
  const s = state.settings;
  if (!p || !s || !s.external_llm || p.llm_mode === "off") return "Todo en local";
  const mode = s.llm_modes.find((m) => m.value === p.llm_mode)?.label || p.llm_mode;
  const who = [s.llm_provider, s.llm_model].filter(Boolean).join(" · ");
  return `Lectura con IA externa (${who}): ${mode.charAt(0).toLowerCase()}${mode.slice(1)}`;
}

function renderSidebar() {
  const p = project();
  $("projectSelect").innerHTML = state.projects.length
    ? state.projects
        .map((x) => `<option value="${esc(x.key)}" ${x.key === state.key ? "selected" : ""}>${esc(x.name)}${x.loaded ? "" : " (sin analizar)"}</option>`)
        .join("")
    : '<option value="">Sin proyectos</option>';
  $("projectSelect").disabled = !state.projects.length;
  for (const link of document.querySelectorAll("#nav a[data-section]")) {
    const section = link.dataset.section;
    link.href = hashFor(state.key, section);
    if (section === state.section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("aiLine").textContent = aiLine(p);
}

function render() {
  renderSidebar();
  for (const section of Object.keys(SECTIONS)) $(`sec-${section}`).hidden = section !== state.section;
  const p = project();
  document.title = `${SECTIONS[state.section]}${p ? " · " + p.name : ""} · Revisor de justificación`;
  if (state.section === "revision") renderReview();
  else if (state.section === "presupuesto") renderBudget();
  else if (state.section === "generales") renderGeneral();
  else if (state.section === "ajustes") renderSettings();
  showJob(p ? p.job : null);
}

/** Reload the projects (and the selected project's expenses). */
async function refresh() {
  state.projects = await api("/projects");
  if (!state.projects.some((p) => p.key === state.key)) {
    state.key = (state.projects.find((p) => p.loaded) || state.projects[0] || {}).key || null;
  }
  const key = state.key;
  state.key = null; // force reloading its expenses
  await selectProject(key);
}

// --- start --------------------------------------------------------------------------------------

(async function init() {
  $("mockBanner").hidden = !isMock;
  setupReview();
  setupSections({ refresh, go });
  onJobFinished(async () => {
    await refresh();
    render();
  });
  $("projectSelect").addEventListener("change", (ev) => {
    go(ev.target.value, state.section === "nuevo" ? "revision" : state.section);
  });
  window.addEventListener("hashchange", route);
  try {
    state.settings = await api("/settings");
    fillSettings();
    state.projects = await api("/projects");
  } catch (error) {
    fatal(`No hay conexión con el servidor o con la base de datos (${error.message}). Arranca la base de datos con «docker compose up -d db».`);
    return;
  }
  if (!state.settings.data_dir_available) {
    say($("dataNote"), "La carpeta de datos (DATA_DIR) no está definida: se puede revisar lo ya cargado, pero no crear proyectos ni analizar.", "err");
  }
  const wanted = parseHash().key;
  state.key = state.projects.some((p) => p.key === wanted)
    ? wanted
    : (state.projects.find((p) => p.loaded) || state.projects[0] || {}).key || null;
  await selectProject(state.key);
  await route();
})();
