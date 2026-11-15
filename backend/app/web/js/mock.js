// Example data for ?mock=1, with the exact shape of the API contract (see docs/pendiente_web.md).
// Everything here is invented: generic businesses, no people, made-up amounts. It lives in memory:
// reloading the page starts over. With &fallo=1 every decision fails, to try the error path.

const params = new URLSearchParams(location.search);
const FAIL_DECISIONS = params.get("fallo") === "1";

const LABELS = {
  AMOUNT_MISMATCH: "El importe no coincide con el listado",
  DATE_MISMATCH: "La fecha no coincide con el listado",
  DATE_OUT_OF_PERIOD: "Fecha fuera del periodo de ejecución",
  DONOR_STAMP_MISSING: "Falta el sello del donante",
  PAYMENT_EVIDENCE_MISSING: "Falta la prueba de pago",
  DUPLICATE_INVOICE: "Factura imputada dos veces",
  NOT_AN_INVOICE: "El documento no es una factura válida",
  ILLEGIBLE: "Documento ilegible",
  BUDGET_DEVIATION: "Desviación respecto al presupuesto",
  LIMIT_EXCEEDED: "Tope del donante superado",
  ORPHAN_DOCUMENT: "Documento sin gasto en el listado",
};
const SEVERITY = { high: "alta", medium: "media", low: "baja" };
const STATUS = { open: "abierta", accepted: "aceptada", false_positive: "falso positivo" };
const DECISION = { pending: "Pendiente", correct: "Correcto", real_issue: "Incidencia real" };
const VERDICT = { ok: "Bien", review: "Necesita revisión", na: "No aplica" };

const SUPPLIERS = [
  "Ferretería El Puente",
  "Papelería Central",
  "Transportes del Valle",
  "Suministros Agrícolas Norte",
  "Imprenta Lago Azul",
  "Hotel Mirador",
  "Estación de Servicio Ruta 7",
  "Farmacia La Plaza",
  "Cooperativa Agraria Unión",
  "Taller Mecánico Sur",
  "Aguas del Distrito",
  "Telecomunicaciones Andina",
];
const CONCEPTS = [
  "Material de oficina",
  "Combustible para el vehículo del proyecto",
  "Semillas y aperos",
  "Impresión de materiales de formación",
  "Alojamiento del equipo en terreno",
  "Medicamentos para el botiquín comunitario",
  "Transporte de materiales",
  "Reparación del vehículo",
  "Suministro de agua",
  "Recarga de datos móviles",
];
const PARTIDAS = ["A.1", "A.2", "A.3", "B.1", "B.2"];

let nextId = 1000;
const id = () => nextId++;

// Deterministic pseudo-random numbers so the example looks the same on every load.
let seed = 7;
const rand = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
const pick = (list) => list[Math.floor(rand() * list.length)];

const fmtAmount = (value, currency) =>
  value === null
    ? null
    : `${value.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
const fmtDate = (iso) => (iso ? iso.split("-").reverse().join("/") : null);

function finding(expense, doc, code, severity, message, page, evidence) {
  return {
    id: id(),
    rule_code: code,
    label: LABELS[code],
    severity,
    severity_label: SEVERITY[severity],
    status: "open",
    status_label: STATUS.open,
    message,
    evidence,
    reviewer_note: null,
    expense_id: expense ? expense.id : null,
    document_id: doc ? doc.id : null,
    document_name: doc ? doc.filename : null,
    document_pages: doc ? doc.pages : null,
    document_openable: Boolean(doc && doc.openable),
    page,
  };
}

const reason = (code, label, blocking = false) => ({ code, label, blocking });

/** One expense of the listing in one of the situations the page has to show. */
function makeExpense(n, kind) {
  const currency = n % 3 === 0 ? "EUR" : "UGX";
  const local = currency === "EUR" ? null : Math.round((50000 + rand() * 4950000) / 100) * 100;
  const eur = local === null ? Math.round(20 + rand() * 1800) : Math.round(local / 4100);
  const day = String(1 + (n % 27)).padStart(2, "0");
  const month = String(3 + (n % 9)).padStart(2, "0");
  const date = `2024-${month}-${day}`;
  const invoice = `F-${2400 + n}`;
  const issuer = pick(SUPPLIERS);
  const listedCurrency = local === null ? "EUR" : currency;
  const listedValue = local === null ? eur : local;
  const e = {
    id: id(),
    order_no: String(n),
    source_row: 9 + n,
    date,
    issuer,
    concept: pick(CONCEPTS),
    partida_code: pick(PARTIDAS),
    partida_raw: null,
    amount_local: local,
    currency: local === null ? null : currency,
    amount_eur: eur,
    amount_donor: eur,
    invoice_no: invoice,
    payment: n % 4 === 0 ? "Efectivo" : `Transferencia ${fmtDate(date)}`,
    suggested_partida: null,
    suggested_reason: null,
    doc_status: "verified",
    doc_status_label: "Justificante verificado por contenido",
    confidence_label: "alta",
    documents: [],
    segments: [],
    findings: [],
    reasons: [],
    compare: [],
  };
  const pages = 1 + (n % 4);
  const doc = {
    id: id(),
    filename: `${n}. ${e.concept} ${invoice}.pdf`,
    pages,
    method_label: "encontrado por el contenido",
    confidence_label: "alta",
    verified: true,
    reason: "El importe, la fecha y el nº de factura leídos coinciden con el gasto.",
    openable: true,
  };
  const page = 1 + (n % pages);
  const read = {
    importe: fmtAmount(listedValue, listedCurrency),
    fecha: fmtDate(date),
    emisor: issuer,
    factura: invoice,
    sello: "sí",
    pago: "sí",
  };
  const match = { importe: true, fecha: true, emisor: true, factura: true, sello: true, pago: true };

  if (kind === "missing") {
    e.doc_status = "missing";
    e.doc_status_label = "Sin justificante";
    e.confidence_label = null;
    e.reasons.push(reason("DOC_MISSING", "Sin justificante", true));
    for (const k of Object.keys(read)) {
      read[k] = null;
      match[k] = null;
    }
  } else if (kind === "na") {
    e.doc_status = "not_required_indirect";
    e.doc_status_label = "No requiere justificante (coste indirecto)";
    e.confidence_label = null;
    e.concept = "Costes indirectos de gestión";
    e.partida_code = "C.1";
    for (const k of Object.keys(read)) {
      read[k] = null;
      match[k] = null;
    }
  } else {
    e.documents.push(doc);
    if (kind === "amount") {
      const wrong = Math.round(listedValue * 0.96);
      read.importe = fmtAmount(wrong, listedCurrency);
      match.importe = false;
      e.findings.push(
        finding(e, doc, "AMOUNT_MISMATCH", "medium",
          `El justificante dice ${read.importe} y el listado ${fmtAmount(listedValue, listedCurrency)}.`,
          page, { listado: listedValue, leido: wrong, moneda: listedCurrency }),
      );
      e.reasons.push(reason("AMOUNT_MISMATCH", LABELS.AMOUNT_MISMATCH));
    } else if (kind === "not_read") {
      read.importe = null;
      match.importe = null;
      read.fecha = null;
      match.fecha = null;
      doc.verified = false;
      doc.method_label = "propuesto por el nombre del archivo";
      doc.confidence_label = "media";
      doc.reason = "El nombre del archivo lleva el nº de orden y el nº de factura del gasto.";
      e.doc_status = "by_name";
      e.doc_status_label = "Emparejado solo por el nombre del archivo";
      e.reasons.push(reason("ONLY_BY_NAME", "Solo emparejado por el nombre del archivo"));
      e.reasons.push(reason("NOT_READ_AMOUNT", "No leído: importe"));
      e.reasons.push(reason("NOT_READ_DATE", "No leído: fecha"));
    } else if (kind === "stamp") {
      read.sello = "no";
      match.sello = false;
      e.findings.push(
        finding(e, doc, "DONOR_STAMP_MISSING", "medium",
          "No se ha encontrado el sello del donante en ninguna página del justificante.",
          page, { paginas_revisadas: pages }),
      );
      e.reasons.push(reason("DONOR_STAMP_MISSING", LABELS.DONOR_STAMP_MISSING));
    } else if (kind === "duplicate") {
      e.findings.push(
        finding(e, doc, "DUPLICATE_INVOICE", "high",
          `La factura ${invoice} del mismo proveedor aparece también en el gasto ${n + 1}.`,
          page, { factura: invoice, otros_gastos: [String(n + 1)] }),
      );
      e.reasons.push(reason("DUPLICATE_INVOICE", LABELS.DUPLICATE_INVOICE, true));
    } else if (kind === "doubtful") {
      read.emisor = null;
      match.emisor = null;
      read.factura = null;
      match.factura = null;
      e.reasons.push(reason("NOT_READ_ISSUER", "No leído: emisor ni nº de factura"));
      e.reasons.push(reason("DOUBTFUL_READING", "Lectura dudosa (confianza de lectura 41 %)"));
    } else if (kind === "payment") {
      read.pago = "no";
      match.pago = false;
      e.findings.push(
        finding(e, doc, "PAYMENT_EVIDENCE_MISSING", "low",
          "Revisar la lectura: no se ha encontrado la prueba de pago, pero la lectura del OCR es poco fiable.",
          pages, { confianza_ocr: 55 }),
      );
      e.reasons.push(reason("PAYMENT_EVIDENCE_MISSING", LABELS.PAYMENT_EVIDENCE_MISSING));
    } else if (kind === "period") {
      e.findings.push(
        finding(e, doc, "DATE_OUT_OF_PERIOD", "high",
          `La factura es del ${fmtDate(date)}, fuera del periodo de ejecución del proyecto.`,
          page, { fecha: date, periodo: ["2024-03-01", "2024-11-30"] }),
      );
      e.reasons.push(reason("DATE_OUT_OF_PERIOD", LABELS.DATE_OUT_OF_PERIOD, true));
    }
    e.segments.push({
      id: id(),
      document_id: doc.id,
      archivo: doc.filename,
      pagina_inicio: page,
      pagina_fin: page,
      tipo: "factura",
      confianza_ocr: kind === "doubtful" ? 41 : 88,
      legible: kind !== "doubtful",
      backend: "tesseract",
    });
  }

  const rows = [
    ["importe", "Importe", fmtAmount(listedValue, listedCurrency)],
    ["fecha", "Fecha", fmtDate(date)],
    ["emisor", "Emisor", issuer],
    ["factura", "Nº de factura", invoice],
    ["sello", "Sello del donante", null],
    ["pago", "Prueba de pago", e.payment],
  ];
  e.compare = rows.map(([field, label, listed]) => ({
    field,
    label,
    listed,
    read: read[field],
    match: match[field],
  }));
  const code = kind === "na" ? "na" : e.reasons.length ? "review" : "ok";
  e.verdict = { code, label: VERDICT[code] };
  e.decision = { status: "pending", label: DECISION.pending, note: null, decided_at: null, stale: false };
  return e;
}

const KINDS = [
  "ok", "amount", "ok", "not_read", "missing", "ok", "stamp", "na", "ok", "duplicate",
  "doubtful", "ok", "payment", "ok", "period", "ok", "amount", "na", "ok", "stamp",
];

function makeProject(count) {
  const expenses = [];
  for (let n = 1; n <= count; n++) expenses.push(makeExpense(n, KINDS[(n - 1) % KINDS.length]));
  // A few already decided, one of them on something that has changed since (stale).
  const review = expenses.filter((e) => e.verdict.code === "review");
  const decide = (e, status, note, stale = false) => {
    e.decision = {
      status: stale ? "pending" : status,
      label: DECISION[stale ? "pending" : status],
      note,
      decided_at: "2024-12-02T10:15:00+00:00",
      stale,
    };
  };
  if (review[0]) decide(review[0], "real_issue", "Pedir al socio la factura corregida.");
  if (review[2]) decide(review[2], "correct", "El sello está en el reverso, página 2.");
  if (review[4]) decide(review[4], "correct", "Revisado en el original.", true);
  return expenses;
}

function counts(expenses) {
  const out = { pending: 0, reviewed: 0, ok: 0, na: 0 };
  for (const e of expenses) {
    if (e.verdict.code === "ok") out.ok++;
    else if (e.verdict.code === "na") out.na++;
    else if (e.decision.status === "pending") out.pending++;
    else out.reviewed++;
  }
  return out;
}

const RULES = [
  ["AMOUNT_MISMATCH", "media", "El importe leído en el justificante debe coincidir con el del listado."],
  ["DATE_MISMATCH", "media", "La fecha leída debe coincidir con la del listado."],
  ["DATE_OUT_OF_PERIOD", "alta", "La fecha del gasto debe estar dentro del periodo de ejecución."],
  ["DONOR_STAMP_MISSING", "media", "Cada justificante lleva el sello del donante."],
  ["PAYMENT_EVIDENCE_MISSING", "media", "Cada gasto tiene su prueba de pago."],
  ["DUPLICATE_INVOICE", "alta", "Una factura no puede imputarse dos veces."],
].map(([code, severity_label, description]) => ({
  code,
  label: LABELS[code],
  description,
  severity_label,
  reference: "Normativa del donante (ejemplo)",
}));

function generalFindings() {
  return [
    finding(null, null, "BUDGET_DEVIATION", "medium",
      "La partida A.2 supera lo aprobado en un 14 %.", null, { partida: "A.2", desviacion_pct: 14 }),
    finding(null, null, "LIMIT_EXCEEDED", "high",
      "Los costes indirectos suponen el 6,1 % de la subvención (máximo 5 %).", null,
      { tope: "INDIRECTOS", porcentaje: 6.1, maximo: 5 }),
    finding(null, { id: 9999, filename: "Recibo sin número.pdf", pages: 1, openable: true },
      "ORPHAN_DOCUMENT", "low", "Este documento no se ha podido asociar a ningún gasto del listado.",
      1, { archivo: "Recibo sin número.pdf" }),
  ];
}

const BUDGET = {
  partidas: [
    ["A.1", "Personal local", 42000, 40310.5, 40310.5, "verde"],
    ["A.2", "Suministros", 18000, 20520.0, 20480.0, "rojo"],
    ["A.3", "Transporte", 9500, 9890.2, 9890.2, "amarillo"],
    ["B.1", "Formación", 12000, 11020.0, 11020.0, "verde"],
    ["B.2", "Equipamiento", 7000, 6400.0, 6400.0, "verde"],
  ].map(([code, name, approved, executed, declared, traffic]) => ({
    code,
    name,
    approved,
    executed,
    declared,
    deviation: executed - approved,
    deviation_pct: (executed - approved) / approved,
    traffic,
  })),
  limits: [
    { code: "PERSONAL", description: "Personal sobre el total", executed: 40310.5, pct: 0.46, max_pct: 0.7, max_amount_eur: null, exceeded: false },
    { code: "INDIRECTOS", description: "Costes indirectos", executed: 5300, pct: 0.061, max_pct: 0.05, max_amount_eur: null, exceeded: true },
    { code: "AUDITORIA", description: "Auditoría", executed: 2100, pct: null, max_pct: null, max_amount_eur: 2500, exceeded: false },
  ],
};

const db = {
  settings: {
    donors: [
      { value: "xunta", label: "Xunta de Galicia" },
      { value: "aecid", label: "AECID" },
    ],
    llm_modes: [
      { value: "off", label: "No (solo OCR local)" },
      { value: "dudosas", label: "Solo las páginas que el OCR lee mal" },
      { value: "todas", label: "Todas las páginas" },
    ],
    data_dir_available: true,
    drive: { available: false, message: "Google Drive no está configurado en este equipo (ejemplo)." },
    external_llm: true,
    llm_provider: "anthropic",
    llm_model: "claude-haiku-5-5",
  },
  projects: [
    project("ejemplo-xunta", "Ejemplo Xunta · agua y saneamiento", "xunta", "dudosas", "2024-03-01", "2024-11-30", makeProject(60)),
    project("ejemplo-aecid", "Ejemplo AECID · formación agrícola", "aecid", "off", "2023-01-01", "2024-06-30", makeProject(24)),
    project("ejemplo-vacio", "Ejemplo sin analizar", "xunta", "off", "2024-01-01", "2024-12-31", []),
  ],
};

function project(key, name, donor, llm_mode, start_date, end_date, expenses) {
  return {
    key,
    name,
    donor,
    code: key.toUpperCase(),
    currency: "EUR",
    source_type: "upload",
    report_file: "Informe economico.xlsx",
    start_date,
    end_date,
    llm_mode,
    llm_max_pages: 50,
    is_sample: key !== "ejemplo-vacio",
    expenses: expenses.length,
    loaded: expenses.length > 0,
    job: { state: "idle" },
    _expenses: expenses,
    _general: expenses.length ? generalFindings() : [],
  };
}

const publicProject = (p) => {
  const { _expenses, _general, ...rest } = p;
  return { ...rest, expenses: _expenses.length, loaded: _expenses.length > 0 };
};

// --- tiny router ---------------------------------------------------------------------------------

const clone = (value) => JSON.parse(JSON.stringify(value));

function reply(body, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const notFound = (detail = "No encontrado") => reply({ detail }, 404);
const bad = (detail, status = 400) => reply({ detail }, status);

function readBody(options) {
  if (!options.body) return {};
  if (options.body instanceof FormData) return Object.fromEntries(options.body.entries());
  return JSON.parse(options.body);
}

function findExpense(expenseId) {
  for (const p of db.projects) {
    const e = p._expenses.find((x) => x.id === expenseId);
    if (e) return e;
  }
  return null;
}

function allFindings() {
  return db.projects.flatMap((p) => [...p._expenses.flatMap((e) => e.findings), ...p._general]);
}

function documentById(documentId) {
  for (const p of db.projects) {
    for (const e of p._expenses) {
      const d = e.documents.find((x) => x.id === documentId);
      if (d) return d;
    }
  }
  return documentId === 9999 ? { filename: "Recibo sin número.pdf", pages: 1 } : null;
}

const slug = (name) =>
  name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "proyecto";

export async function respond(fullPath, options = {}) {
  await new Promise((r) => setTimeout(r, 120)); // feel like a network
  const method = (options.method || "GET").toUpperCase();
  const [path, query = ""] = fullPath.split("?");
  const q = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean);

  if (path === "/settings") return reply(db.settings);
  if (parts[0] === "projects") return projects(method, parts, options);
  if (parts[0] === "expenses" && parts[2] === "decision" && method === "PATCH") {
    return decide(Number(parts[1]), readBody(options));
  }
  if (parts[0] === "findings" && method === "PATCH") {
    const f = allFindings().find((x) => x.id === Number(parts[1]));
    if (!f) return notFound("Incidencia no encontrada");
    const body = readBody(options);
    if (body.status) {
      f.status = body.status;
      f.status_label = STATUS[body.status];
    }
    if (body.reviewer_note !== undefined) f.reviewer_note = body.reviewer_note || null;
    return reply(clone(f));
  }
  if (parts[0] === "documents" && parts[2] === "page") {
    const doc = documentById(Number(parts[1]));
    if (!doc) return notFound("Documento no encontrado");
    return pagePicture(doc, parseInt(parts[3], 10), Number(q.get("rotation")) || 0, Number(q.get("dpi")) || 110);
  }
  return notFound();
}

function projects(method, parts, options) {
  if (parts.length === 1 && method === "GET") return reply(db.projects.map(publicProject));
  if (parts.length === 1 && method === "POST") return create(readBody(options));
  if (parts[1] === "upload" && method === "POST") return create(readBody(options));
  const p = db.projects.find((x) => x.key === parts[1]);
  if (!p) return notFound("Proyecto no encontrado");
  const sub = parts[2];
  if (!sub && method === "GET") return reply(publicProject(p));
  if (!sub && method === "PATCH") {
    const body = readBody(options);
    const start = "start_date" in body ? body.start_date : p.start_date;
    const end = "end_date" in body ? body.end_date : p.end_date;
    if (start && end && end < start) return bad("La fecha de fin es anterior a la de inicio.");
    Object.assign(p, body);
    return reply(publicProject(p));
  }
  if (!sub && method === "DELETE") {
    if (p.is_sample) return bad("Los proyectos de ejemplo no se pueden borrar.");
    db.projects = db.projects.filter((x) => x !== p);
    return reply(null, 204);
  }
  if (sub === "run" && method === "POST") {
    if (!p._expenses.length) {
      p._expenses = makeProject(12);
      p._general = generalFindings();
    }
    p.job = { state: "done", fraction: 1, message: "Análisis simulado con datos de ejemplo." };
    return reply(p.job, 202);
  }
  if (sub === "run") return reply(p.job);
  if (!p._expenses.length) return notFound("El proyecto aún no se ha analizado");
  if (sub === "review") {
    return reply(
      clone({
        project: { name: p.name, code: p.code, donor: p.donor, expenses: p._expenses.length },
        expenses: p._expenses,
        counts: counts(p._expenses),
        general_findings: p._general,
        rule_labels: LABELS,
        rules: RULES,
      }),
    );
  }
  if (sub === "budget") return reply(clone(BUDGET));
  return notFound();
}

function create(body) {
  if (!body.name) return bad("Falta el nombre del proyecto.");
  if (!body.start_date || !body.end_date) return bad("Falta el periodo de ejecución.");
  let key = slug(body.name);
  while (db.projects.some((p) => p.key === key)) key += "-2";
  const p = project(key, body.name, body.donor || "xunta", body.llm_mode || "off", body.start_date, body.end_date, []);
  p.is_sample = false;
  p.source_type = body.source_type === "drive" ? "drive" : "upload";
  db.projects.push(p);
  return reply({ ...publicProject(p), saved: { documentos: 1, informe: 1 } }, 201);
}

function decide(expenseId, body) {
  if (FAIL_DECISIONS) return bad("Fallo simulado al guardar (&fallo=1).", 503);
  const e = findExpense(expenseId);
  if (!e) return notFound("Gasto no encontrado");
  if (!["pending", "correct", "real_issue"].includes(body.status)) return bad("Estado no válido.", 422);
  const note = (body.note || "").trim() || null;
  if (body.status === "correct" && !note && e.reasons.some((r) => r.blocking)) {
    return bad("Este gasto tiene un motivo bloqueante: explica en la nota por qué es correcto.", 422);
  }
  e.decision = {
    status: body.status,
    label: DECISION[body.status],
    note,
    decided_at: body.status === "pending" ? null : new Date().toISOString(),
    stale: false,
  };
  return reply(clone(e));
}

/** A drawn stand-in for a scanned page (SVG), turned and sized like the real PNG would be. */
function pagePicture(doc, page, rotation, dpi) {
  const pages = doc.pages || 1;
  const n = Math.min(Math.max(page || 1, 1), pages);
  const scale = Math.min(Math.max(dpi, 72), 300) / 110;
  const w = Math.round(620 * scale);
  const h = Math.round(877 * scale);
  const turned = rotation % 180 !== 0;
  const W = turned ? h : w;
  const H = turned ? w : h;
  const lines = [];
  for (let i = 0; i < 14; i++) {
    const len = 260 + ((i * 37 + n * 11) % 230);
    lines.push(`<rect x="60" y="${250 + i * 34}" width="${len}" height="9" fill="#c9ced6"/>`);
  }
  const name = doc.filename.replace(/[&<>"']/g, "");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
<rect width="${W}" height="${H}" fill="#fff"/>
<g transform="translate(${W / 2} ${H / 2}) rotate(${rotation}) scale(${scale}) translate(-310 -438.5)">
<rect x="0.5" y="0.5" width="619" height="876" fill="#fbfaf7" stroke="#bbb"/>
<text x="60" y="90" font-family="sans-serif" font-size="26" fill="#333">FACTURA</text>
<text x="60" y="125" font-family="sans-serif" font-size="14" fill="#555">${name}</text>
<text x="60" y="190" font-family="sans-serif" font-size="13" fill="#777">Documento de ejemplo: no es un justificante real</text>
${lines.join("")}
<rect x="400" y="740" width="160" height="80" fill="none" stroke="#7a8fb8" stroke-width="3" transform="rotate(-8 480 780)"/>
<text x="420" y="785" font-family="sans-serif" font-size="14" fill="#7a8fb8" transform="rotate(-8 480 780)">SELLO DONANTE</text>
<text x="310" y="850" text-anchor="middle" font-family="sans-serif" font-size="13" fill="#888">Página ${n} de ${pages}</text>
</g></svg>`;
  return new Response(new Blob([svg], { type: "image/svg+xml" }), {
    status: 200,
    headers: { "Content-Type": "image/svg+xml", "X-Pages": String(pages) },
  });
}
