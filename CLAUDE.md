# CLAUDE.md — Revisor de justificación

## Proyecto

"Revisor de justificación" para la **Fundación Entreculturas** (hackatón IA4GOOD).
Herramienta que ayuda a revisar la justificación económica de proyectos de cooperación
financiados por donantes (AECID, Xunta de Galicia...).

- La IA **SEÑALA** incidencias con explicación; un técnico humano las **acepta o descarta**.
- No se exige 100 % de precisión (mejor señalar de más que de menos, pero con explicación clara).
- Salida principal: **Excel**.

## Alcance MVP

- **Proceso 1 — Justificantes.** Para cada justificante (PDF escaneado) comprobar que:
  - existe;
  - cuadra con su línea del listado de gastos (importe, fecha, nº orden, emisor);
  - es una factura válida;
  - lleva sello del donante;
  - tiene prueba de pago;
  - es legible;
  - su expediente está completo según el tipo de gasto.
- **Proceso 2a — Presupuesto.** Ejecutado vs presupuesto aprobado por partida + topes del donante.
- **Proceso 2b — Imputación.** Sugerir la partida correcta de cada gasto y marcar imputaciones dudosas.
- **Duplicados / doble imputación.**

## Datos

**NUNCA se suben al repo.** Ruta base en la variable `DATA_DIR` del `.env`.

### Piloto — Xunta: `$DATA_DIR/DocJustificativa/24-UG-226 Adjumani - Xunta 2024/`
- Informe: `Informe_economico_final_Adjuamni_def 14.10.25 v1.xls` (formato .xls → `xlrd`).
  - Hojas: `Relación de facturas` (~203 gastos), `Comparativa gastos`, `Tipo de cambio`.
- ~100 PDFs nombrados `Nº orden. Concepto Nº factura.pdf`. Casos raros a contemplar:
  - rangos: `33-39 ...`;
  - sufijos: `69.A`, `49A`;
  - duplicados;
  - pares Excel + PDF del mismo justificante;
  - nº orden `N/A`.
- Normativa: `2024_guia_ah-_galego-castellano.pdf`, `AnuncioG0759-260424-0004_es.pdf`.

### Segundo donante — AECID: `$DATA_DIR/DocJustificativa/22-PY-136 - AECID/`
- Informe: `Informe seguimiento y final_31_10.xlsx`.
  - Hoja `"X.B Listado de comprobantes "` (**OJO: espacio final** en el nombre).
    - La cabecera está en la fila cuya columna A es `Nº de Orden` (no es la primera fila).
    - 1.321 gastos.
    - Nº orden con prefijo: `CG` = CEPAG, `FA` = Fe y Alegría, `EC` = Entreculturas.
    - Subnúmeros tipo `FA236.3`.
    - Columnas: Nº de Orden, Fecha, Fuente, Emisor, Concepto, Partida, Moneda Local,
      TC Divisa/M.L., Divisa, TC €/Divisa, Euros, AECID, Fondos propios, Otros.
  - Hoja `"X.A.1 Balance presup "` (también con espacio final): presupuesto aprobado vs
    ejecutado por partida AECID.
- 17 PDFs escaneados sin texto. Mapeo de nombre de fichero → nº orden:
  - `ORDEN xx` / `Orden N°xx` → `CGxx`;
  - número solo → `FAxx`, **salvo** `163`/`164` → `CG163`/`CG164`.
- Normativa AECID: `$DATA_DIR/DocJustificativa/Guía Normas AECID ONGD y ACCIONES versión revisada diciembre 2019 (1).pdf` (texto extraíble).

### Evaluación: `$DATA_DIR/DocJustificativa/22_ET_217 Crisis Tigray Addis - Xunta de Galicia/`
- `Documentos e informe con incidencias/Resumen incidencias_ Respuestas EC.xlsx`:
  incidencias reales del revisor externo + respuesta de EC (muchas son falsos positivos).
  Úsalo como ground truth para medir precisión/recall.

### Prototipo anterior
- `$DATA_DIR/Prototipo/Rules.xlsx`: reglas del prototipo anterior (Xunta), hojas `test` y `hardcoded`.

### Características de los PDF
Casi todos son **escaneados (sin capa de texto)**, algunos **girados 180º**, con **manuscrito**.
Hay que tratarlos como imágenes (render con PyMuPDF → LLM multimodal) y detectar/corregir rotación.

## Topes conocidos

**AECID**
- Costes indirectos ≤ 12 % de la subvención.
- Cambios entre partidas = modificación accidental (se comunica, no requiere autorización).

**Xunta de Galicia**
- Personal ≤ 70 %.
- Costes indirectos ≤ 5 %.
- Funcionamiento en terreno ≤ 5 %.
- Auditoría ≤ 2.500 €.
- Evaluación ≤ 5.000 €.
- 3 ofertas en obras > 40.000 € y en suministros/servicios > 15.000 €.
- Pagos en efectivo ≤ 1.000 €.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 + PostgreSQL, Pydantic v2.
- **Datos/PDF:** pandas, openpyxl, xlrd, PyMuPDF, rapidfuzz.
- **LLM:** multimodal con salida JSON estructurada, detrás de una interfaz propia
  (proveedor configurable por `.env`).
- **Frontend:** React + Vite + TypeScript + TanStack Table + react-pdf.
- **Infra:** docker-compose con servicios `db`, `api`, `web`.

## Reglas de trabajo

- Comprobaciones numéricas **SIEMPRE en código**; el LLM solo extrae datos y juzga reglas cualitativas.
- Todo resultado del LLM se **cachea por hash del PDF + versión del prompt**.
- Cada incidencia (*finding*) lleva:
  - regla;
  - severidad (`alta` / `media` / `baja`);
  - explicación en español;
  - evidencia (página / campo / valores);
  - estado (`abierta` / `aceptada` / `falso_positivo`).
- **Nunca commitear** datos de `DATA_DIR` ni `.env`.
- Tests con **pytest**; los tests que usan datos reales se **saltan si `DATA_DIR` no existe**.
- Código y comentarios en **inglés**; textos de cara al usuario en **español**.
