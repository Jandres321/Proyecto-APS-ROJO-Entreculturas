// Launching the analysis of a project and following its progress (sidebar and Ajustes).

import { api } from "./api.js";
import { project, state } from "./state.js";
import { $, say } from "./util.js";

let poll = null;
let onFinished = async () => {};

/** `callback` reloads projects and review when a run ends. */
export function onJobFinished(callback) {
  onFinished = callback;
}

export async function launch(reread = false) {
  const p = project();
  if (!p) return;
  try {
    showJob(await api(`/projects/${encodeURIComponent(p.key)}/run?reread=${reread}`, { method: "POST" }));
    if (!poll) await check(); // finished at once (example data)
  } catch (error) {
    showJob({ state: "error", message: error.message });
  }
}

async function check() {
  const key = state.key;
  let job;
  try {
    job = await api(`/projects/${encodeURIComponent(key)}/run`);
  } catch {
    return;
  }
  if (key !== state.key) return;
  showJob(job);
  if (job.state !== "running") {
    stopPolling();
    await onFinished(job);
  }
}

function stopPolling() {
  clearInterval(poll);
  poll = null;
}

/** Paint a job (or the lack of one) wherever it is shown. */
export function showJob(job) {
  const p = project();
  const running = Boolean(job && job.state === "running");
  const pct = running ? Math.round((job.fraction || 0) * 100) : 0;

  const side = $("jobLine");
  if (running) say(side, `Analizando: ${pct} %`, "");
  else if (job && job.state === "error") say(side, "El último análisis falló (ver Ajustes).", "err");
  else say(side, "");

  for (const status of [$("runStatus"), $("runStatusEmpty")]) {
    if (!status) continue;
    if (!p) say(status, "");
    else if (running) say(status, (job.step ? `Paso ${job.step} de ${job.steps}: ` : "") + (job.message || "Analizando…"));
    else if (job && job.state === "done") say(status, job.message || "Análisis terminado.", "ok");
    else if (job && job.state === "error") say(status, job.message || "El análisis ha fallado.", "err");
    else say(status, p.loaded ? "Proyecto analizado. Relanzar conserva las decisiones." : "Aún no se ha analizado.");
  }
  const bar = $("progress");
  if (bar) {
    bar.hidden = !running;
    bar.value = pct;
  }
  for (const id of ["runBtn", "emptyRunBtn"]) {
    const button = $(id);
    if (button) button.disabled = running || !p;
  }
  if (running && !poll) poll = setInterval(check, 1000);
  if (!running && poll) stopPolling();
}
