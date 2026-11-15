// The PDF page embedded in the expense detail: previous / next page, rotate, zoom (dpi), and a
// link to the original. Only one viewer exists at a time; mount() replaces the previous one.

import { fileUrl, pageImage } from "./api.js";
import { esc } from "./util.js";

const ZOOM_DPI = [80, 110, 150, 200, 250, 300];
const DEFAULT_ZOOM = 1; // 110 dpi, what the API renders by default

let current = null;

class Viewer {
  constructor(root, docs, start) {
    this.root = root;
    this.docs = docs;
    this.doc = Math.max(0, docs.findIndex((d) => d.id === start.documentId));
    this.page = start.page || 1;
    this.pages = null;
    this.rotation = 0;
    this.zoom = DEFAULT_ZOOM;
    this.url = null;
    this.token = 0;
    root.addEventListener("click", (ev) => this.onClick(ev));
    root.addEventListener("change", (ev) => {
      if (ev.target.matches("[data-v=doc]")) this.showDocument(Number(ev.target.value));
    });
    this.paint();
    this.load();
  }

  get document() {
    return this.docs[this.doc];
  }

  onClick(ev) {
    const action = ev.target.closest("[data-v]")?.dataset.v;
    if (action === "prev") this.step(-1);
    else if (action === "next") this.step(1);
    else if (action === "rotate") this.rotate();
    else if (action === "zoom-in") this.setZoom(this.zoom + 1);
    else if (action === "zoom-out") this.setZoom(this.zoom - 1);
  }

  showDocument(index) {
    this.doc = index;
    this.page = 1;
    this.pages = this.document.pages || null;
    this.rotation = 0;
    this.paint();
    this.load();
  }

  step(delta) {
    const next = this.page + delta;
    if (next < 1 || (this.pages && next > this.pages)) return;
    this.page = next;
    this.load();
  }

  rotate() {
    this.rotation = (this.rotation + 90) % 360;
    this.load();
  }

  setZoom(level) {
    const clamped = Math.min(Math.max(level, 0), ZOOM_DPI.length - 1);
    if (clamped === this.zoom) return;
    this.zoom = clamped;
    this.load();
  }

  paint() {
    const d = this.document;
    const original = fileUrl(`/documents/${d.id}/file`);
    const choose =
      this.docs.length > 1
        ? `<select data-v="doc" aria-label="Documento">${this.docs
            .map((x, i) => `<option value="${i}" ${i === this.doc ? "selected" : ""}>${esc(x.filename)}</option>`)
            .join("")}</select>`
        : `<span class="viewer-name" title="${esc(d.filename)}">${esc(d.filename)}</span>`;
    this.root.innerHTML = `
      <div class="viewer-bar">
        ${choose}
        <span class="viewer-tools">
          <button type="button" class="btn small" data-v="prev" title="Página anterior (←)">Anterior</button>
          <span class="viewer-page" data-v-page></span>
          <button type="button" class="btn small" data-v="next" title="Página siguiente (→)">Siguiente</button>
          <button type="button" class="btn small" data-v="rotate">Girar</button>
          <button type="button" class="btn small" data-v="zoom-out" aria-label="Reducir">−</button>
          <span class="viewer-zoom" data-v-zoom></span>
          <button type="button" class="btn small" data-v="zoom-in" aria-label="Ampliar">+</button>
          ${original ? `<a class="btn small" href="${esc(original)}" target="_blank" rel="noopener">Abrir original</a>` : ""}
        </span>
      </div>
      <div class="viewer-canvas" data-v-canvas></div>`;
    if (!d.openable) {
      this.root.querySelector("[data-v-canvas]").innerHTML =
        '<p class="muted pad">No se puede previsualizar: no es un PDF, está dentro de un .zip o no está en la carpeta de datos.</p>';
    }
  }

  async load() {
    const d = this.document;
    if (!d.openable) return;
    const token = ++this.token;
    const pageEl = this.root.querySelector("[data-v-page]");
    const zoomEl = this.root.querySelector("[data-v-zoom]");
    const canvas = this.root.querySelector("[data-v-canvas]");
    pageEl.textContent = `Página ${this.page}…`;
    zoomEl.textContent = `${Math.round((ZOOM_DPI[this.zoom] / 110) * 100)} %`;
    canvas.setAttribute("aria-busy", "true");
    try {
      const { blob, pages } = await pageImage(d.id, this.page, this.rotation, ZOOM_DPI[this.zoom]);
      if (token !== this.token || current !== this) return; // a newer request or another expense
      this.pages = pages || this.pages;
      this.page = Math.min(this.page, this.pages || this.page);
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = URL.createObjectURL(blob);
      canvas.innerHTML = `<img alt="Página ${this.page} de ${esc(d.filename)}" src="${esc(this.url)}" />`;
      pageEl.textContent = `Página ${this.page} de ${this.pages || "?"}`;
    } catch (error) {
      if (token !== this.token) return;
      canvas.innerHTML = `<p class="error pad">No se ha podido mostrar la página: ${esc(error.message)}</p>`;
      pageEl.textContent = "";
    } finally {
      if (token === this.token) canvas.removeAttribute("aria-busy");
    }
  }

  destroy() {
    this.token++;
    if (this.url) URL.revokeObjectURL(this.url);
  }
}

/** Show `docs` in `root`, starting at { documentId, page }. */
export function mountViewer(root, docs, start = {}) {
  unmountViewer();
  if (!docs.length) {
    root.innerHTML = '<p class="muted pad">Sin justificante: no hay documento que mostrar.</p>';
    return null;
  }
  current = new Viewer(root, docs, start);
  return current;
}

export function unmountViewer() {
  if (current) current.destroy();
  current = null;
}

/** For the ←/→ shortcuts. */
export function stepViewer(delta) {
  if (current) current.step(delta);
}
