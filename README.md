# Revisor de justificación

Herramienta de apoyo a la revisión de la justificación económica de proyectos de cooperación
(Fundación Entreculturas · hackatón IA4GOOD). La IA **señala** incidencias con explicación; un
técnico las **acepta o descarta**. Salida principal: Excel. Contexto completo en [CLAUDE.md](CLAUDE.md).

## Este repositorio integra dos piezas

| Pieza | Dónde | Para qué |
|---|---|---|
| **Revisor de justificación** | `backend/`, `frontend/` | La aplicación descrita en este README: lectura de PDF, cruce híbrido, reglas del donante, presupuesto y app de revisión (Streamlit) |
| **Conciliador** (línea de comandos) | `src/conciliador/`, `config/`, `tests/` | El conciliador original, con un YAML por proyecto (Uganda, ECHO, La Rioja), lectura del contenido con Claude y Google Drive. Ver [docs/conciliador.md](docs/conciliador.md) |

El cruce por nombre del backend (`pipeline/conciliador.py`) es un port del conciliador. El estado de la
integración está en [Integración con el conciliador](#integración-con-el-conciliador).

## Arranque rápido

Requisitos: Docker Desktop (con Compose v2).

```bash
cp .env.example .env      # y edita DATA_DIR con la ruta local de los datos confidenciales
docker compose up --build # o: make up
```

| Servicio | URL |
|---|---|
| Web (placeholder) | http://localhost:5173 |
| API | http://localhost:8000 (docs en `/docs`, salud en `/health`) |
| **App de revisión (web)** | http://localhost:8000 (la sirve la propia API) |
| App de revisión (Streamlit, alternativa) | http://localhost:8501 (`make ui`) |
| PostgreSQL | localhost:5432 (usuario/clave/base: `revisor`) |

El contenedor `api` aplica las migraciones al arrancar y recarga el código en caliente.
`DATA_DIR` se monta **solo lectura** en `/data`. Los datos nunca se suben al repo.

## Comandos (Makefile)

| Comando | Acción |
|---|---|
| `make up` / `make down` | Levantar / parar el stack |
| `make migrate` | `alembic upgrade head` en el contenedor api |
| `make test` | `pytest` (los tests con datos reales se saltan si no hay `DATA_DIR`) |
| `make lint` | ruff (backend) y `tsc` (frontend) |
| `make inspect` | Resumen por partida de los informes piloto (Uganda/Xunta y Paraguay/AECID) |
| `make read-pdfs` | Lee los PDFs de Paraguay (OCR + segmentación) y guarda los segmentos |
| `make match-report` | Cruce híbrido de los dos pilotos (`--solo-nombre` para el cruce por nombre de archivo): con documento, sin documento y huérfanos |
| `make evaluar-hibrido` | Mide conciliador solo / contenido solo / híbrido contra la numeración manual e imprime la tabla |
| `make compare-cruce` | Compara a nivel de pares nombre, contenido y ambos (y con PDFs renombrados al azar) |
| `make checks` | Ejecuta las comprobaciones de justificantes (`rules/<donante>.yaml`) sobre los dos pilotos |
| `make ui` | Levanta la app de revisión (Streamlit) en http://localhost:8501 |

## Cruce de justificantes: híbrido (`pipeline/conciliador.py`, `match_hybrid.py`, `match_content.py`)

El cruce es **híbrido: el conciliador (por nombre) propone y el contenido verifica**. Cada mitad falla donde
la otra acierta: el nombre describe el gasto pero no dice nada de lo que hay dentro del PDF; el contenido es
exacto cuando el OCR lee bien y mudo cuando no. Con los datos de Uganda, solo por nombre se acierta el 89 % de
los gastos; solo por contenido, el 9 %.

- `register_documents`: inventario recursivo (incluye PDFs dentro de `.zip`) con sha256, páginas y
  si hay capa de texto. Un duplicado exacto no se vuelve a registrar: avisa (log + incidencia
  `DOC_DUPLICATE`, baja) y su nombre sigue sirviendo para el cruce.
- `read_project_documents` (`app/lectura/project.py`): lee los PDF registrados y guarda sus
  segmentos. La convención de importes y la moneda por defecto salen de `match_config.py`.
- `match_by_segments` (`match_content.py`, delega en `match_hybrid.match_project`): lanza el cruce híbrido
  y guarda vínculos e incidencias.

### 1. El conciliador propone (`conciliador.py`)

Port del conciliador por nombre del repo `Proyecto-APS-ROJO-Entreculturas` (sin modificarlo). **Oculta el nº
de orden manual** del nombre («12. Fondo pensiones.pdf») y compara solo:

- **concepto**: palabras del nombre contra concepto + emisor + nº de factura del gasto (singulares,
  sinónimos del dominio unificados);
- **referencia de factura/comprobante** escrita en el nombre (`EB UGA04 602`): si identifica **un solo**
  documento basta para asignar; si la comparten varios, decide el concepto;
- **mes** del nombre contra el del gasto (o el anterior, en cotizaciones que se pagan al mes siguiente).

Umbral 0,60. **Confianza:** alta ≥ 0,80; media entre 0,60 y 0,80; **baja** si otro documento *distinto*
(no una copia: el `.pdf` y el `.xlsx` de lo mismo no compiten) queda a menos de 0,05. Es exactamente la lógica
del conciliador original: reproduce su **161/181** en Uganda (alta 78/78, media 54/54, baja 29/41).

Se configura por proyecto en `match_config.py → name_reconciler`: `reference_pattern`, `long_numbers`,
`hide_order`, `synonyms` y `params` (umbrales y conceptos que se pagan el mes siguiente). **Sin esa sección el
proyecto se cruza solo por contenido** (Paraguay: sus nombres son solo números de orden).

### 2. El contenido verifica (`match_hybrid.hybrid_match`)

1. Propuesta **alta/media** → **se acepta**. Se marca **«verificado por contenido»** si el total del documento
   coincide con el importe del gasto **y** coincide el emisor o la fecha (o coincide el nº de factura junto con
   cualquiera de los otros tres). Si el documento muestra importes y ninguno es el del gasto, el vínculo se marca
   `contradicho` pero **no se descarta ni se baja su confianza** (ver más abajo por qué).
2. El contenido **solo reasigna con evidencia fuerte**: importe + emisor, o nº de factura, hacia **otro** gasto, y
   solo si ese documento no respalda en absoluto al gasto propuesto. Entonces se registra un conflicto y el
   documento pasa al otro gasto.
3. Propuesta **baja** (empate entre candidatos): el contenido desempata si **exactamente uno** de los candidatos
   queda verificado; si no, se mantiene la mejor opción del conciliador con confianza baja y una incidencia
   `MATCH_AMBIGUOUS`.
4. Un gasto que nadie propuso solo recibe documento con evidencia sólida del contenido (evidencia fuerte o un
   vínculo del motor de contenido de confianza media o alta sin ambigüedad). Con dos documentos igual de buenos no
   se adivina.
5. **`NAME_CONTENT_CONFLICT` solo cuando el contenido apunta con evidencia fuerte a OTRO gasto.** Nunca cuando el
   OCR no lee nada: un documento ilegible no es un conflicto. También se comprueban los nº de orden del nombre
   (`match_by_filename`), pero solo para *señalar* conflictos, nunca para asignar.

Cada vínculo (`expense_document`) guarda `method` (`filename` si lo propuso el nombre, `content` si lo halló el
contenido), `confidence`, `verified` y un **motivo en español**; los segmentos que lo respaldan se guardan en
`expense_segment` para que `pipeline/checks/` pueda comprobar el importe, la fecha, el sello y el pago sobre ellos. Si
ningún segmento respalda al gasto se enlazan los de tipo factura/recibo/liquidación del documento.

### 3. `carpeta_completa` (por proyecto, en `match_config.py`)

| Valor | Proyecto | Un gasto sin documento es… |
|---|---|---|
| `True` (por defecto) | Uganda | `DOC_MISSING` (alta) |
| `False` | Paraguay (17 PDF para 1.321 gastos) | **«No incluido en la muestra»**: informativo, **sin incidencia** |

El estado de justificante de cada gasto (`review.document_statuses`) sale en el Excel (hoja *Gastos*) y es uno de:
verificado por contenido · propuesto por el nombre (sin verificar) · encontrado por contenido · vinculado a mano ·
sin justificante · no incluido en la muestra · no requiere justificante (coste indirecto / cofinanciación).

> Este repo no tiene un YAML por proyecto: la configuración por proyecto vive en `match_config.py` (el
> equivalente del YAML del conciliador original), y `carpeta_completa` está ahí.

### 4. Medición contra la numeración manual (`scripts/evaluar_hibrido.py`)

```bash
make evaluar-hibrido        # o: cd backend && python scripts/evaluar_hibrido.py [--only xunta|aecid]
```

Misma métrica que el `--validar` del conciliador: por cada gasto con nº de orden manual (181 en Uganda), ¿el
documento asignado es el que lleva ese número en el nombre? No escribe en la BD; lee los PDF de la caché.

**Uganda, 14/11/2026** (181 gastos evaluables, 119 archivos, 475 segmentos leídos):

| Variante | Exactitud | Correctos | Incorrectos | No encontrados | Alta | Media | Baja |
|---|---|---|---|---|---|---|---|
| Conciliador solo (por nombre) | **161/181** (89,0 %) | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |
| Solo contenido | 16/181 (8,8 %) | 16 | 0 | 165 | 1/1 | 15/15 | — |
| Híbrido: solo verificar | 161/181 | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |
| Híbrido + desempate de dudosos | 161/181 | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |
| Híbrido + reasignar con evidencia fuerte | 161/181 | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |
| Híbrido + asignar por contenido | 161/181 | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |
| **Híbrido completo** | **161/181** (89,0 %) | 161 | 12 | 8 | 78/78 | 54/54 | 29/41 |

**El híbrido cumple el mínimo (≥ 161/181) pero no lo mejora**, y conviene entender por qué:
- El contenido casi no aporta evidencia fuerte: solo **17 de 179** propuestas se verifican por contenido, y el
  desempate, la reasignación y la asignación por contenido no llegan a disparar en este dato (conflictos: 0).
  El límite es la lectura (manuscritos, sellos, OCR pobre), no la lógica: sale de las mismas causas que el cruce
  solo por contenido (ver la comparación al final de esta sección).
- Los 12 incorrectos y los 8 no encontrados son los mismos que los del conciliador solo: gastos con nombres
  casi idénticos (órdenes 22, 31, 32...) entre los que el contenido no tiene cómo elegir.
- **37 propuestas aparecen como `contradicho`** (el documento muestra importes y ninguno es el del gasto) y
  **las 37 son asignaciones correctas** según la numeración manual: el OCR lee mal los totales. Por eso la
  contradicción es solo un aviso y **no veta ni baja la confianza**; vetar habría bajado la exactitud de
  161 a 124.

**Paraguay** (muestra: 17 PDF, 24 pares verdaderos; sin conciliador por nombre, solo contenido): 3 gastos con
documento bien asignado, 0 incorrectos, 21 no encontrados. La «exactitud» (1.300/1.321) **no es comparable**:
cuenta como acierto no asignar nada a un gasto sin documento esperado, y casi todos lo son.

**Efecto en las incidencias del pipeline completo** (antes → ahora, con el mismo OCR):

| | Uganda | Paraguay |
|---|---|---|
| Gastos con documento | 37 → 179 | 6 → 3 |
| `DOC_MISSING` | 157 → 15 | 1.140 → 0 (1.143 no incluidos en la muestra) |
| `NAME_CONTENT_CONFLICT` | 148 → 0 | 23 → 2 |
| `SEGMENT_ORPHAN` | 209 → 21 | 21 → 23 |
| `MATCH_AMBIGUOUS` | 2 → 42 (empates por nombre sin resolver) | 0 |

**Paraguay pierde cobertura:** el motor de contenido solo daba 6 vínculos (5 correctos, 3 de ellos de confianza baja); el híbrido solo asigna por contenido con evidencia sólida y se queda en 3 (los 3 correctos, precisión del 100 % en vez del 83 %), a cambio de dejar sin asignar dos aciertos de confianza baja.

**Contrapartida:** como ahora hay muchos más gastos con documento, `pipeline/checks/` se ejecuta sobre 179 en vez de 37
(Uganda: 260 incidencias de comprobación frente a 78): `AMOUNT_MISMATCH` pasa de 0 a 36 y `DATE_MISMATCH` de 10 a
45. Dada la tabla anterior, **buena parte de los `AMOUNT_MISMATCH` son ruido de OCR** sobre asignaciones
correctas; el mensaje enseña el importe leído y el del gasto para que el revisor lo descarte rápido.
`SEGMENT_ORPHAN` ya no marca las páginas sueltas de un PDF que sí justifica un gasto.

### Motor de contenido (`match_content.match_segments`)

Es el verificador del híbrido: puntúa cada pareja gasto–segmento y asigna **N:M**.

| Señal | Peso | Regla |
|---|---|---|
| importe | 0,45 | el total del segmento = importe local / EUR / imputado al donante (±0,05 %: completo; ±1 %: 70 %); +0,08 si ese importe es único en el listado |
| nº factura | 0,35 | igual (completo), contenido (80 %) o mismos dígitos (70 %) |
| emisor / NIF | 0,15 | emisor parecido (≥85: completo; ≥70: 60 %) o NIF dentro del gasto |
| fecha | 0,12 | ±3 días completo, ±15 días 60 %, ±45 días 30 % |
| nº de orden | 0,12 | el escrito/sellado en el documento = el del gasto |
| débil | 0,06 | palabras del concepto en el emisor |

Una pareja necesita un ancla (importe, nº de factura o nº de orden) y puntuación ≥ 0,50. **Confianza:**
alta ≥ 0,80, media ≥ 0,62, baja el resto o si hay empate. Un importe que solo *aparece entre las cifras* del
documento (no como total) cuenta al 80 % y no basta por sí solo. Un total que es la **suma de varios gastos**
(nóminas) se puede asignar a todos con `group_matching=True`, pero está **desactivado**: dio 1 acierto de 22.
Umbral 0,50: bajarlo a 0,36 en Uganda gana 15 aciertos pero la precisión cae del 90 % al 56 %.

### Incidencias del cruce (se regeneran en cada ejecución; las decisiones del revisor se respetan)

| Regla | Severidad | Cuándo |
|---|---|---|
| `DOC_MISSING` | alta | gasto sin documento **y `carpeta_completa` = true**. **No** para costes indirectos (`no_document_partidas`) ni cofinanciación (importe donante 0 o % imputado 0; ver `match_config.expense_kind`) |
| `NAME_CONTENT_CONFLICT` | media | el contenido apunta con evidencia fuerte a **otro** gasto (si se reasignó, el mensaje lo dice). Nunca por un documento que el OCR no lee |
| `SEGMENT_ORPHAN` | media/baja | segmento (salvo «otro») de un documento que no respalda **ningún** gasto |
| `MATCH_AMBIGUOUS` | media | empate por nombre que el contenido no resuelve, o un documento que encaja igual por contenido con varios gastos |
| `ORDER_NUMBER_MISMATCH` | media | el nº de orden escrito en el documento no es el del gasto al que se vincula |

- `match_by_filename` + `match_report`: el cruce por nombre anterior (nº de orden del nombre ↔ listado)
  sigue disponible (`scripts/match_report.py --solo-nombre`).

### Comparación de variantes a nivel de pares (`scripts/comparar_cruce.py`)

Complementa a `evaluar_hibrido.py`: mide pares gasto ↔ PDF contra la numeración manual para **(a)** el conciliador
por nombre, **(b)** solo contenido, **(c)** contenido con el nombre como señal débil, y **(b)/(c) con los PDF
renombrados al azar**. Resultados (14/11/2026):

| Proyecto | Variante | Pares | Bien | Precisión | Cobertura |
|---|---|---|---|---|---|
| Uganda (203 gastos, 91 PDF, 187 pares verdaderos) | (a) conciliador por nombre | 178 | 160 | 89,9 % | 85,6 % |
| | (b) solo contenido | 39 | 35 | 89,7 % | 18,7 % |
| | (c) contenido + nombre débil | 45 | 40 | 88,9 % | 21,4 % |
| | (b) renombrados al azar | 39 | 35 | 89,7 % | 18,7 % |
| | (c) renombrados al azar | 39 | 35 | 89,7 % | 18,7 % |
| Paraguay (1.321 gastos, 17 PDF, 24 pares verdaderos) | (a) conciliador por nombre | 0 | 0 | — | 0 % |
| | (b) solo contenido | 6 | 5 | 83,3 % | 20,8 % |
| | (c)/(b) renombrados | 6 | 5 | 83,3 % | 20,8 % |

- (b) con PDFs renombrados da **exactamente** los mismos pares porque el motor de contenido no recibe nombres:
  comprueba que no hay filtración. (a) funciona porque en Uganda el nombre describe el gasto; con nombres al azar dejaría
  de hacerlo, y es lo que no se puede garantizar en proyectos nuevos: por eso el contenido verifica.
- En Uganda, 101 de los 187 pares verdaderos no tienen en el PDF ninguna cifra, nº de factura ni nº de orden que
  coincida con el gasto; en Paraguay, 15 de 19 (manuscrito, sellos, OCR pobre). Es donde debería aportar el backend
  LLM con visión.
- La validación usa la numeración de los nombres como verdad: si una persona se equivocó al numerar, el error cuenta
  contra el cruce.

## Comprobaciones de justificantes (`pipeline/checks/` + `backend/rules/<donante>.yaml`)

Sobre cada gasto y los segmentos que lo respaldan se ejecutan diez comprobaciones. **Los umbrales,
tolerancias, requisitos de documentos, la referencia normativa y la severidad están en el YAML del
donante**, no en el código (`app/rules.py` lo carga y valida: faltar una regla o un código mal escrito
es un error; una regla se apaga con `active: false`). `sync_rules` las vuelca a la tabla `rule`.

- `rules/xunta.yaml`: parte de `Prototipo/Rules.xlsx` (cada regla indica en `params.sources` de cuál sale) y de
  la Orden de convocatoria AH 2024, art. 19.5 (DOG 91, págs. 28377-28378) y Anexo IV.
- `rules/aecid.yaml`: parte de la Guía AECID (nov. 2019): 10.6 (R11-VII.1.6, pág. 88), 10.7 (R11-VII.5, pág. 89),
  11.1 (R11-VII.2.1, pág. 90), 11.2 (R11-VII.3.1, pág. 93) y 11.4 (R11-VII.4.2.6, pág. 117; 4.2.9, pág. 122).
  `Rules.xlsx` es solo de Xunta.

`pipeline/checks/` es un paquete (la API pública sigue siendo `from app.pipeline.checks import run_checks,
build_context, check_documents, check_file_completeness, check_duplicates…`):

| Módulo | Contenido |
|---|---|
| `context.py` | `Seg`, `ExpenseFacts`, `Case`, `CheckContext`, helpers comunes (`_finding`, `unreliable_reading`, `soften`…), `project_period` y `build_context` (lee la BD) |
| `documents.py` | importe, fecha, sello, factura válida, prueba de pago, efectivo e ilegible (`check_documents`) |
| `period.py` | `DATE_OUT_OF_PERIOD` |
| `completeness.py` | `DOC_INCOMPLETE` (`check_file_completeness`) |
| `duplicates.py` | `DUPLICATE_INVOICE` (`check_duplicates`) |
| `__init__.py` | `run_checks` (ejecuta todo y persiste) y los reexports |

| Regla | Qué comprueba | Xunta | AECID |
|---|---|---|---|
| `AMOUNT_MISMATCH` (alta) | el importe del documento ≠ el del listado (local, euros o imputado; ±1 %; admite la suma si un documento respalda varios gastos) | sí | sí |
| `DATE_MISMATCH` (media) | fecha del documento ≠ fecha del listado (±3 días); fecha de pago del listado ≠ fecha del justificante bancario | + fecha de pago | solo documento |
| `DATE_OUT_OF_PERIOD` (media) | fecha del documento o de pago (listado o justificante bancario) fuera del periodo del proyecto (`start_date`/`end_date`, con días de margen en el YAML); sin periodo no se aplica; si la fecha sale de un OCR dudoso, severidad baja | sí | sí |
| `DONOR_STAMP_MISSING` (media) | falta el sello, el nº de expediente o el % de imputación (o el % no coincide con el listado) | % siempre | % solo si la imputación es parcial |
| `NOT_AN_INVOICE` (media) | recibo o proforma presentados como factura; factura sin nº, emisor o fecha (baja: puede ser fallo de lectura) | partidas de bienes y servicios | idem |
| `PAYMENT_EVIDENCE_MISSING` (alta) | sin justificante bancario (transferencia) o, en efectivo, sin sello «pagado» | sello exigido | no exigido (la Guía lo exime en Paraguay, pág. 167) |
| `CASH_OVER_LIMIT` (alta) | pago en efectivo de 1.000 € o más | ≥ 1.000 € | > 2.500 € |
| `ILLEGIBLE` (media) | documento ilegible (confianza de lectura < 45 %) | sí | sí |
| `DUPLICATE_INVOICE` (alta) | misma factura en dos PDF; un documento imputado a varios gastos por más de su importe; línea repetida en el listado (mismo nº, emisor, concepto e importe) | + listado | no (el listado no trae el nº de factura) |
| `DOC_INCOMPLETE` (media) | nómina → contrato + pago + cotización; refrigerios → lista de asistencia | sí | sí |

- **Menos falsos avisos (lectura dudosa):** `AMOUNT_MISMATCH`, `DATE_MISMATCH`, `DONOR_STAMP_MISSING`,
  `PAYMENT_EVIDENCE_MISSING` y `DATE_OUT_OF_PERIOD` bajan a severidad **baja** y el mensaje empieza por «Revisar la
  lectura: …» cuando el documento está vinculado con confianza alta/media o la confianza OCR es baja. Se controla en
  el YAML de cada regla con `soft_link_confidences` (lista; vacía = criterio apagado) y `soft_below_ocr_confidence`
  (0 = apagado); la evidencia lleva `lectura_dudosa`. Para la prueba de pago basta con que *algún* documento del
  expediente se lea mal (la prueba puede estar en esa página).
- Las comprobaciones sobre documentos solo se ejecutan en gastos **con segmentos vinculados**; los gastos sin
  documento ya los señala `DOC_MISSING`. `CASH_OVER_LIMIT` es una propiedad del listado y se evalúa siempre.
- El **expediente** de un gasto son los segmentos enlazados (importe, fecha, sello...) más todos los del mismo PDF
  (la prueba de pago, el contrato o la lista de asistencia suelen ir en el mismo PDF que la factura).
- Cada incidencia lleva el mensaje en español, la evidencia (segmento, páginas, valores) y la referencia normativa
  (`evidence.referencia`). Se regeneran en cada ejecución y se respetan las decisiones del revisor.
- Ejecutar: `python scripts/checks_report.py` (`make checks`): lee los PDF, cruza, carga las reglas y cuenta las
  incidencias por regla; hace rollback salvo `--persist`.

**Resultados con los pilotos** (14/11/2026, lectura con Tesseract): Uganda, 78 incidencias en los 37 gastos con
documento (36 de sello, 14 de factura, 10 de fecha, 9 de pago, 6 de expediente incompleto, 3 de duplicados);
Paraguay, 9 en los 6 gastos con documento.

**Limitaciones que hay que conocer:**
- Con el cruce híbrido el 88 % de los gastos de Uganda tiene documento (por nombre, verificado por contenido en 17),
  pero en Paraguay solo 3 de 1.321 (es una muestra). Las comprobaciones sobre documentos propuestos por nombre
  dependen del OCR: ver el apartado *Contrapartida* del cruce (`AMOUNT_MISMATCH` por totales mal leídos).
- Las comprobaciones de **sello, fecha y emisor son tan buenas como el OCR**: en Uganda el sello de Xunta
  («Funded by: Xunta de Galicia / Code / File number») se reconoce cuando se lee, pero en varios PDF el sello no sale en
  el texto y la incidencia es un falso positivo (el mensaje lo avisa). Misma limitación para las fechas manuscritas.
- Un control que `Rules.xlsx` incluye **no está implementado**: la traducción de idiomas. El plazo de ejecución
  (`DATE_OUT_OF_PERIOD`) se lee con `getattr(project, "start_date" / "end_date", None)`: **hasta que el modelo de
  proyectos tenga esas columnas la regla no se aplica** (ver `docs/integracion_pendiente.md`). Los topes por partida no son por gasto: viven en
  [Presupuesto y topes](#presupuesto-y-topes-pipelinebudgetpy).
- `DONOR_STAMP_MISSING` y `NOT_AN_INVOICE` son heurísticos; `ILLEGIBLE` no tiene norma detrás (criterio de
  Entreculturas) y así consta en su `reference`.

## Presupuesto y topes (`pipeline/budget.py`)

Proceso 2a: presupuesto aprobado frente a ejecutado por partida y topes del donante. Todo el cálculo
es numérico y en código. `analyze_budget` es una función pura (la que prueban los tests);
`compute_execution` la alimenta desde la BD y `run_budget` guarda las incidencias en `finding`.

**Ejecutado = siempre recalculado desde el listado de gastos**, nunca el que declara el informe:

- *Importe del donante* = suma de `expense.amount_donor`.
- *Importe del proyecto* = importe del donante + los otros financiadores imputados al proyecto
  (`expense.extra`: `other_funders` en Xunta; `own_funds` y `other_funders` en AECID). **No** es
  `amount_eur`, que es la factura entera aunque casi todo se pague fuera del proyecto: con él, el
  personal de Uganda saldría al 81 % del presupuesto en vez de al 27 %. La suma donante + otros cuadra
  con el «ejecutado total» del informe en todas las partidas de Uganda.

| Regla | Cuándo |
|---|---|
| `LIMIT_EXCEEDED` | Se supera un tope del donante (`DonorAdapter.limits()`, ámbito `budget`). El porcentaje es sobre la base **aprobada** (subvención del donante o presupuesto total del proyecto, según el `Limit`). Un valor igual al tope es válido |
| `BUDGET_DEVIATION` | La desviación ejecutado − aprobado sale de la zona verde del semáforo. En AECID se explica como *modificación accidental*: no requiere autorización, pero hay que comunicarla en el informe |
| `EXECUTED_REPORT_MISMATCH` | El ejecutado declarado en el informe difiere del recalculado en más de 1 € |

**Semáforo por partida** (`POLICIES`, por donante): verde si |desviación| ≤ 5 % del aprobado (o ≤ 1 €),
amarillo hasta el 10 %, rojo por encima. Una partida sin importe aprobado que tiene gasto es roja; un
gasto en una partida que no está en el presupuesto crea la partida con aprobado 0. Se pone también en rojo
cualquier partida afectada por un tope superado o por un descuadre con el informe. Severidad de
`BUDGET_DEVIATION`: Xunta media/alta; AECID baja/media (no es un error, es algo que comunicar).

**Topes** (los define cada adaptador, ámbito `budget`): AECID, indirectos ≤ 12 % de la subvención; Xunta,
personal ≤ 70 %, indirectos ≤ 5 % y funcionamiento en terreno ≤ 5 % del presupuesto total, auditoría ≤ 2.500 €
y evaluación ≤ 5.000 €. Los topes por gasto (3 ofertas, efectivo ≤ 1.000 €) no son de presupuesto.

**Con los informes reales** (14/11/2026) no salta ningún tope ni descuadre: Paraguay, indirectos al
**11,84 %** de la subvención (límite 12 %); Uganda, indirectos al 4,96 % (límite 5 %) y personal al 27 %.
Las desviaciones por partida sí aparecen (varias en rojo en Uganda, 3 en AECID).

**Supuestos pendientes de confirmar con Entreculturas:** los umbrales del semáforo (5 % / 10 %) y la
tolerancia de 1 € son criterio nuestro, no norma del donante; y la nota de Xunta para las desviaciones
es genérica porque no tenemos la regla de modificaciones de la convocatoria.

**Identidad de una incidencia entre ejecuciones:** `_sync_findings` identifica una incidencia por
(regla, gasto, documento) y, para las que no cuelgan de ninguno (presupuesto), por `evidence["subject"]`
(partida o código del tope). Así una decisión sobre un tope no esconde otro de la misma regla.

## App de revisión web (`backend/app/web/index.html`)

Interfaz principal para el técnico. Es una sola página estática que sirve la API en `/` y que habla con
`/api`: no necesita Node ni internet. Hace lo mismo que la app de Streamlit, con otra presentación.

**De dónde sale el diseño.** El aspecto (barra superior, tarjetas por pasos, tarjetas semáforo que filtran y
**una fila desplegable por gasto**) viene del prototipo `app_voluntariado` del equipo. De ese prototipo se ha
tomado **solo la estética**: sus datos venían escritos dentro de la página y los campos «leídos del PDF» eran
copia del listado, así que sus resultados no medían nada. Aquí **todo lo que se ve sale de la base de datos**,
es decir, de lo que el pipeline ha leído y cruzado de verdad:

- Un dato que no se ha leído dentro del PDF se muestra como *no leído*; nunca se rellena con el del listado.
- No hay porcentaje de confianza inventado: el veredicto de cada gasto se calcula con su estado de justificante
  y sus incidencias (`app/review_view.py → expense_verdict`), y la confianza es la del cruce (alta/media/baja).
- No hay incidencias escritas a mano para casos concretos.

| Paso | Qué hace |
|---|---|
| **1. Proyecto y análisis** | Pestañas con los proyectos (`pipeline/run.py: PILOTS`) y botón **Lanzar análisis**: ejecuta el pipeline completo en segundo plano con barra de progreso. *Releer el informe* se comporta igual que en Streamlit |
| **2. Reglas, presupuesto y topes** | Reglas activas del donante (de `rules/<donante>.yaml`), aprobado frente a ejecutado por partida con su semáforo y topes del donante |
| **3. Listado de gastos** | Una fila por gasto con su veredicto. Tarjetas semáforo (todos / hay que revisar / sin justificante / sin incidencias), buscador, filtros por incidencia, gravedad y estado, y orden. Al desplegar: **justificantes vinculados** (método, confianza, si está verificado por contenido y por qué), **qué se ha leído dentro del PDF** (emisor, nº de factura, fecha, importe, sello, prueba de pago, legibilidad, por segmento) e **incidencias del gasto** con su evidencia |
| **4. Incidencias generales** | Las que no cuelgan de un gasto: presupuesto, topes, documentos y segmentos sin gasto |

- **Decisión por incidencia:** *Aceptar*, *Falso positivo* o *Reabrir*, con nota opcional (`PATCH /api/findings/{id}`).
  Sobrevive a relanzar el análisis. En la fila del gasto hay dos atajos sobre todas sus incidencias abiertas:
  *Dar por bueno* (las marca como falso positivo) y *Confirmar* (las acepta como reales).
- **PDF al lado:** *Ver en el PDF* abre la página del problema, con anterior/siguiente y giro de 90° (muchos
  escaneos vienen del revés), y un enlace para abrir el PDF original. Solo se sirven archivos registrados
  que estén dentro de `DATA_DIR`.
- **Informe Excel:** *Descargar informe Excel* (`GET /api/ui/pilots/{key}/report.xlsx`, `pipeline/report.py`) genera un
  informe del análisis con las decisiones tomadas hasta ese momento: **Resumen** (totales por veredicto),
  **Análisis por gasto** (una fila por gasto: veredicto y semáforo, justificante, lo leído dentro del PDF e
  incidencias), **Incidencias**, **Presupuesto** y **Topes**. Sale de la misma vista que la página, así que
  coinciden. El libro técnico de siempre sigue en `.../export.xlsx`.
- **Google Drive:** se puede conectar una carpeta de Drive en vez de copiar los archivos (ver «Google Drive»).
- **IA externa:** la página solo **muestra** si está activada (`LLM_PROVIDER` / `ALLOW_EXTERNAL_LLM` del
  `.env`). No pide ni guarda ninguna clave en el navegador.

**Lo que no se ha traído del prototipo, y por qué:** subir un Excel y PDFs sueltos desde el navegador (aquí los
proyectos se definen en `PILOTS` y se leen de `DATA_DIR`), las reglas editables desde la interfaz (las reglas
viven en `rules/<donante>.yaml`), la clave de Claude guardada en el navegador y el OCR en el navegador.

Rutas que usa (`app/api/ui.py`): `GET /api/ui/pilots`, `POST /api/ui/drive/projects`,
`DELETE /api/ui/drive/projects/{key}`, `POST` y `GET /api/ui/pilots/{key}/run`, `GET /api/ui/pilots/{key}/review`,
`.../budget`, `.../report.xlsx`, `.../export.xlsx`, `GET /api/ui/documents/{id}/page/{n}.png` y `.../file`. Probada con `tests/test_web_app.py` (datos inventados) y a mano con Uganda.

## Google Drive (`app/drive.py`, `app/projects.py`)

En la app web, el bloque **☁️ Conectar una carpeta de Google Drive** añade un proyecto a partir de una carpeta de
Drive con el informe económico (Excel o Google Sheets) y los justificantes. Al lanzar el análisis, la carpeta
se **copia** a `DATA_DIR/_drive/<código>/` (solo lo nuevo o cambiado; lo borrado en Drive se borra de la copia)
y se analiza como cualquier otro proyecto. Las conexiones se guardan en `DATA_DIR/_drive/proyectos.json`, dentro
de la carpeta de datos confidenciales, nunca en el repositorio. Permiso de solo lectura (`drive.readonly`).

**Credenciales** (en `.secrets/`, ignorada por git; rutas en `GOOGLE_CREDENTIALS` / `GOOGLE_TOKEN`):

| Opción | Cómo | Cuándo |
|---|---|---|
| **Cuenta de servicio** (recomendada con Docker) | Google Cloud Console → proyecto → activar *Google Drive API* → *Credenciales* → *Cuenta de servicio* → *Claves* → JSON. Guárdalo como `.secrets/credentials.json` y **comparte la carpeta de Drive** (lector) con el correo de la cuenta de servicio | Sin navegador; funciona dentro de Docker |
| **OAuth (app de escritorio)** | Mismo sitio → *ID de cliente OAuth* tipo *Aplicación de escritorio* → JSON como `.secrets/credentials.json`. Después, **una vez y fuera de Docker**: `cd backend && python -m app.drive` (abre el navegador y guarda `.secrets/token.json`) | Si no se puede compartir con una cuenta de servicio |

Sin credenciales o sin el extra `drive`, el resto de la app funciona y la página explica qué falta.
Con Docker: `.secrets/` se monta en `/secrets` y `DATA_DIR/_drive` se monta con escritura (el resto de
`DATA_DIR` sigue en solo lectura). Probado con `tests/test_drive_source.py` (Drive simulado, sin red).

## Cómo ejecutar y probar la app de revisión

```bash
cp .env.example .env            # DATA_DIR = ruta local de TUM_SociAI (con DocJustificativa/)
mkdir -p .secrets               # opcional: credentials.json para Google Drive
docker compose up --build api   # levanta PostgreSQL + API (migraciones automáticas)
```

Abre **http://localhost:8000**.

1. **Proyecto de ejemplo:** elige Uganda o Paraguay → *Lanzar análisis* (la primera vez el OCR tarda; luego
   usa la caché) → revisa gastos, abre *Ver en el PDF*, marca alguna incidencia → *Descargar informe Excel*.
2. **Drive:** pega el enlace de una carpeta de prueba (con un informe y algunos PDF), nombre y donante →
   *Conectar carpeta* → aparece como pestaña ☁️ → *Lanzar análisis* (descarga y analiza) → informe Excel.
   Para quitarla: *Quitar esta conexión de Drive* (los archivos descargados se conservan).
3. **Tests:** `docker compose run --rm api pytest` (los de base de datos necesitan una base vacía; sin Docker,
   `cd backend && pytest` salta los que necesitan PostgreSQL o `DATA_DIR`).

## App de revisión (Streamlit, `backend/ui/streamlit_app.py`)

Alternativa a la app web, con las mismas funciones y los mismos datos.

Interfaz para el técnico: lanza el pipeline completo de un proyecto y revisa las incidencias con el PDF
al lado.

```bash
make ui                                   # Docker: http://localhost:8501 (Tesseract incluido)
# o, sin Docker, desde backend/ (necesita PostgreSQL migrado y DATA_DIR):
uv pip install -e ".[dev,ui]" && streamlit run ui/streamlit_app.py --server.address localhost
```

1. **Proyecto y análisis.** Se elige un piloto (`pipeline/run.py: PILOTS`: Uganda/Xunta y Paraguay/AECID)
   y **Lanzar análisis** ejecuta, con barra de progreso: lectura del informe → inventario de documentos →
   lectura de los PDF (OCR con caché) → cruce por contenido → comprobaciones de justificantes → presupuesto
   y topes. Con la caché de OCR tarda unos segundos (Uganda ~8 s, Paraguay ~3 s).
2. **Pestaña Incidencias.** Todas las incidencias (cruce, comprobaciones y presupuesto) salen de la misma tabla
   `finding`. Se filtran por tipo, gravedad, estado (por defecto, solo las abiertas) y texto.
3. **Detalle.** Al seleccionar una fila: la explicación, la evidencia y, a la derecha, el **PDF en la página del
   problema** (`evidence.pagina_inicio`; con selector de página y giro de 90/180/270° porque muchos escaneos vienen
   del revés). En `DOC_MISSING` se enseña el candidato más cercano. Las incidencias de presupuesto no tienen PDF.
4. **Decisión.** **Aceptar** (la incidencia es real), **Falso positivo** o **Reabrir**, con una nota opcional. Se
   guarda en `finding.status` / `finding.reviewer_note` y **sobrevive a relanzar el análisis** (`_sync_findings`
   solo reemplaza las abiertas).
5. **Pestaña Presupuesto y topes.** Aprobado frente a ejecutado por partida con el semáforo, y los topes con su estado.
6. **Excel.** *Preparar Excel* y *Descargar*: hojas `Incidencias` (con estado y nota), `Gastos` (documentos
   vinculados y partida sugerida), `Presupuesto` y `Topes` (`pipeline/export.py`).

Las mismas decisiones están en la API: `GET /api/projects/{id}/findings?status=&severity=` y
`PATCH /api/findings/{id}` (`{"status": "accepted" | "false_positive" | "open", "reviewer_note": "..."}`).

**Detalles que conviene saber:**
- *Releer el informe* (casilla, desactivada hasta que el proyecto está cargado) recrea los gastos y **descarta las
  decisiones sobre incidencias de gastos concretos**, porque cuelgan de los gastos borrados. Por eso no se hace por
  defecto al relanzar. Las decisiones sobre incidencias de presupuesto se conservan.
- A diferencia de los scripts (`make checks`, `make match-report`, que hacen rollback), la app **guarda** en la BD.
- Lógica compartida con la API y los tests: `app/review.py` (listar, decidir, localizar documento y página) y
  `app/pipeline/run.py` (orquestador).
- Un PDF dentro de un `.zip` o que no está en `DATA_DIR` no se puede mostrar; la app lo avisa.
- Probada con `AppTest` (`tests/test_streamlit_app.py`) sobre datos inventados y sobre Paraguay.

## Adaptadores de donante

`backend/app/adapters/` convierte el informe de cada donante al modelo común (`ExpenseIn`,
`BudgetLineIn`, `Partida`, `Limit`):

- **Xunta** (`.xls`, xlrd): hojas "Relación de facturas" y "comparativa gastos".
- **AECID** (`.xlsx`, openpyxl): hojas "X.B Listado de comprobantes " y "X.A.1 Balance presup "
  (con espacio final; la búsqueda de hoja tolera mayúsculas y espacios).
- El catálogo de partidas AECID sale de la Guía (`aecid_partidas.json`, generado con
  `python scripts/build_aecid_catalog.py`; las definiciones débiles se corrigen a mano en el propio
  script). El de Xunta está en `xunta.py`, redactado desde la orden de convocatoria (art. 8).
- `ingest_project(session, project_id, expenses_file)` guarda gastos y líneas de presupuesto en BD
  (sustituye lo anterior del proyecto; si el fichero falla, no borra nada).

Resumen de los datos leídos, sin base de datos: `python scripts/inspect_ingest.py`.

Los tests con datos reales se saltan si no hay `DATA_DIR`; los de ingesta necesitan además
PostgreSQL migrado (`docker compose up -d db` + `alembic upgrade head`) y hacen rollback al
terminar.

## Lectura de PDFs (`app/lectura/`)

Lee **todas las páginas** de cada PDF, lo parte en **documentos lógicos** (segmentos) y extrae
campos por segmento. Se guarda en la tabla `segmento` (migración `0003`).

```bash
cd backend
python -m app.lectura "$DATA_DIR/DocJustificativa/22-PY-136 - AECID" --convencion dot_thousands --moneda PYG
python -m app.lectura "$DATA_DIR/DocJustificativa/24-UG-226 Adjumani - Xunta 2024" \
    --max 20 --excluir "guia|anuncio|formulario" --convencion comma_thousands --moneda UGX
```

| Opción | Efecto |
|---|---|
| `--convencion` | Importes: `dot_thousands` (Paraguay: `1.250.000`), `comma_thousands` (Uganda: `1,250,000.50`) o `auto` |
| `--moneda` | Moneda por defecto si el documento no la indica (una mención suelta de «euros» no la pisa) |
| `--max N` / `--excluir REGEX` | Muestra de N PDFs repartidos por la lista / ignora nombres (normativa) |
| `--llm` / `--llm-modo` | Backend LLM con visión (`--llm` = todas las páginas; `--llm-modo dudosas` solo las dudosas). **Solo con `ALLOW_EXTERNAL_LLM=true`** |
| `--no-guardar` | No escribe en `segmento` |

- **Páginas:** texto nativo si lo hay; si no, OCR con Tesseract (`spa+eng+fra`). Si la lectura es
  pobre se detecta la orientación (OSD) y se prueban los giros, quedándose con el mejor.
- **Caché:** `CACHE_DIR/lectura/<sha256>/<página>.<backend>.json`. Procesado en paralelo con un
  único pool de páginas (`OCR_WORKERS`).
- **Segmentos:** tipo (`factura`, `recibo`, `nomina`, `transferencia`, `lista_asistencia`,
  `liquidacion`, `contrato`, `otro`) por palabras clave; páginas consecutivas del mismo tipo se
  agrupan (una factura con otro nº abre segmento nuevo; los anexos flojos se pegan al contrato).
- **Campos:** emisor, NIF/RUC, nº factura, fecha ISO, importe (decimal, según convención), moneda,
  nº de orden escrito/sellado, sello del donante (sí/no, expediente, %), prueba de pago, legible y
  página (`pagina_inicio`/`pagina_fin`). Todo con reglas en código; el LLM, si se activa, solo
  completa y sus valores pisan a las reglas.
- **Lectura selectiva con el LLM (`LlmReadOptions`):** `modo` `off` (por defecto), `dudosas` o
  `todas`, `max_pages` y `min_ocr_confidence`. En `dudosas` solo se envían las páginas con confianza
  OCR por debajo del umbral o cuyo segmento no tiene importe o fecha (las listas de asistencia y los
  segmentos «otro» no los necesitan); nunca más de `max_pages` por ejecución, las más dudosas
  primero. Las páginas ya leídas por el LLM salen de la caché sin coste y no cuentan para el tope.
  `process_pdfs(..., llm=...)` devuelve la lista de PDFs con `.llm` (`LlmReadSummary`: páginas
  enviadas, tokens y coste estimado con `LLM_PRICE_INPUT_PER_MTOK`/`LLM_PRICE_OUTPUT_PER_MTOK`).
  `read_project_documents(..., llm=..., on_llm_summary=...)` hace lo mismo para un proyecto.
  CLI: `--llm-modo off|dudosas|todas --llm-max-paginas N --llm-min-confianza X` (`--llm` = `todas`).
- **Qué campo gana:** el valor del LLM sustituye al del OCR solo si el OCR no lo tenía o la
  confianza del segmento es baja (< umbral) o es ilegible; si no, se queda el del OCR.
  `segmento.extra["campo_fuente"]` guarda el origen de cada campo (`ocr` o `llm`).
- **Backend LLM con visión:** desactivado por defecto porque envía páginas a un servicio externo.
  Con proveedor distinto de `fake` exige `ALLOW_EXTERNAL_LLM=true`. Proveedor implementado: `anthropic`
  (`app/llm/anthropic_client.py`, salida estructurada; modelo en `LLM_MODEL`, por defecto `claude-haiku-5-5`;
  clave en `LLM_API_KEY` o, si está vacía, `ANTHROPIC_API_KEY`). `openai` y `gemini` siguen sin implementar.

Requisitos: Tesseract con `spa`, `eng`, `fra` y `osd` (la imagen Docker ya los instala; en Windows
`winget install UB-Mannheim.TesseractOCR` y define `TESSDATA_DIR` si falta algún idioma).

**Limitaciones conocidas:** el OCR no lee bien el manuscrito (importes y fechas escritos a mano, nº de
orden), los sellos en diagonal salen fragmentados y el emisor es heurístico (a veces sale una línea
de cabecera). Esos campos son los que debería mejorar el backend con visión.

## Desarrollo sin Docker

```bash
# Backend (Python 3.12)
cd backend
uv venv --python 3.12 && uv pip install -e ".[dev]"
alembic upgrade head          # requiere un PostgreSQL accesible en DATABASE_URL
uvicorn app.main:app --reload
pytest

# Frontend
cd frontend && npm install && npm run dev
```

Nueva migración tras cambiar `app/models.py`: `alembic revision --autogenerate -m "descripción"`.

## Estructura

```
backend/app/
  main.py, config.py, db.py, models.py, schemas.py, enums.py
  review.py   decisiones del revisor y localización de documento/página de una incidencia
  adapters/   un adaptador por donante (xunta, aecid) tras la interfaz DonorAdapter
  pipeline/   ingest -> match -> extract -> checks / classify / budget -> export (run.py los encadena)
              match: conciliador.py (por nombre) + match_content.py (motor de contenido) -> match_hybrid.py
              match_eval.py: validación contra la numeración manual
  lectura/    OCR y segmentación de PDF
  llm/        interfaz LLMClient + factory (proveedor por LLM_PROVIDER; "fake" para tests)
  api/        rutas (projects, findings, ui: las de la app web)
  review_view.py  vista por gasto (documentos, lo leído, incidencias y veredicto) para la app web
  web/        app de revisión web: una página estática (index.html) que sirve la API en /
backend/ui/        app de revisión alternativa (Streamlit)
backend/alembic/   migraciones
frontend/          Vite + React + TS (TanStack Query/Table, react-pdf, react-router)
```

## Estado

| Pieza | Estado |
|---|---|
| Adaptadores Xunta y AECID, ingesta, lectura de PDF, cruce híbrido (nombre propone, contenido verifica), comprobaciones | Hechos |
| Presupuesto y topes (`budget.py`), revisión de incidencias (`review.py`), Excel (`export.py`) | Hechos |
| App de revisión web (diseño de `app_voluntariado`, datos reales), app de Streamlit y rutas de findings de la API | Hechas |
| Extracción con LLM (`extract.py`), sugerencia de partida (`classify.py`, proceso 2b) | Pendientes (stubs) |
| Rutas `projects` de la API, frontend React | Pendientes (501 / placeholder) |

## Proceso 2b: partida sugerida por gasto (`app/pipeline/classify.py`)

- **Capa 1, reglas (siempre, sin red):** palabras clave del concepto y el proveedor → categoría (viajes, personal, materiales, comidas, servicios…) → código de partida de cada donante. Personal de sede (EUR o fuente "Entreculturas") va a A.I.6.3 / A.V.3. Si no hay regla clara, no sugiere nada. Nunca contradice los costes indirectos (B).
- **Capa 2, IA (opcional):** solo para lo que la capa 1 no resuelve, solo con `ALLOW_EXTERNAL_LLM=true` y como máximo `max_llm_expenses` gastos (por defecto 1). Solo envía concepto y proveedor del listado, nunca PDFs. Proveedor: `LLM_PROVIDER=anthropic` (`app/llm/anthropic_client.py`, modelo por defecto `claude-haiku-5-5`). Prueba con un gasto inventado: `python scripts/probar_llm.py`.
- **Incidencia `PARTIDA_MISMATCH`:** media si la confianza es ≥ 0,75; baja entre 0,5 y 0,75 (categorías discutibles como materiales de construcción o alquiler de sillas).
- **Acuerdo con la partida imputada** (informes ya aprobados, capa 1): Uganda 171/175 (97,7 %, sugiere en el 86 % de gastos); Paraguay 1.114/1.161 (96,0 %, sugiere en el 88 %). Las reglas se ajustaron mirando estos dos proyectos: cifras optimistas.

## `AMOUNT_MISMATCH` suavizado

Si el documento está asignado con confianza alta/media, o la lectura OCR tiene confianza < 70 %, la diferencia de importe sale como severidad **baja** con el texto «Revisar la lectura del importe…» (casi siempre es un error de OCR). Parámetros en `rules/<donante>.yaml`: `soft_below_ocr_confidence`, `soft_link_confidences`.

## Integración con el conciliador

La rama `integracion-revisor` junta este repositorio con el conciliador por línea de comandos
(`src/conciliador/`, documentado en [docs/conciliador.md](docs/conciliador.md)). Las dos ramas no tenían
historia común: se han fusionado tal cual y las dos piezas **conviven sin tocarse**. Esta aplicación es la
interfaz que se mantiene; lo que el conciliador tiene de más se va portando aquí.

| Función del conciliador | Estado en el backend (14/11/2026) |
|---|---|
| Cruce por nombre de archivo | **Portado** desde antes (`pipeline/conciliador.py`): reproduce el 161/181 de Uganda |
| Documentos sin información en el nombre no se toman por copias (`AC-07.pdf`) | **Portado**: sin esto un empate exacto entre dos documentos no salía como ambiguo |
| Lectura del documento con Claude (visión) | El backend ya la tenía por página (`lectura/vision.py`). **Arreglado el cliente**: usaba una tool forzada (`tool_choice`), que los modelos actuales (`claude-sonnet-5-5`, `claude-opus-5-5`) rechazan con un error 400; ahora usa salida estructurada. La imagen Docker instala ya el extra `llm`. **Sin probar contra la API real desde el backend** |
| Clave de la API | `LLM_API_KEY` o, si está vacía, `ANTHROPIC_API_KEY`: sirve el mismo `.env` del conciliador |
| Fecha de pago como segunda fecha del cruce por contenido | **Pendiente.** En La Rioja la fecha del documento era la de pago en 8 de 15 casos. Aquí cambia los resultados de `match_content.py` y hay que medirlo antes con `make evaluar-hibrido` (hoy la fecha de pago solo se usa en `DATE_MISMATCH`) |
| Tope de llamadas por ejecución, exclusión de archivos (nóminas) y leer solo los documentos dudosos | **Pendiente.** El backend lee todas las páginas de todos los PDF; con el LLM activado eso es coste y datos personales enviados fuera |
| Proyectos ECHO (Colombia) y La Rioja (Colombia) | Solo en el conciliador (`config/*.yaml`). Aquí hace falta un adaptador de donante por cada uno (`app/adapters/`) |
| Un YAML por proyecto | Solo en el conciliador |
| Google Drive | En la app web (ver «Google Drive») y en el conciliador |

**Lo que enseñaron las pruebas reales del conciliador con Claude** (detalle en `docs/conciliador.md`, sección 9), útil para este backend:

- En **La Rioja** (archivos nombrados solo por su nº de orden) el importe leído coincidió con el del listado en
  los 15 documentos: se asignaron 12 gastos, 11 correctos, sin falsos positivos. Es el caso que aquí queda
  «solo por contenido», donde el OCR no llega.
- En **Uganda** el importe leído **no** coincide con el del listado aunque la lectura sea correcta (se comprobó
  a mano un documento): no es la moneda ni la imputación parcial. Es coherente con los 37 `contradicho`
  de la tabla de arriba, que por tanto pueden no ser errores de OCR sino del dato.

**Cómo se ejecuta cada pieza:**

```bash
# Aplicación (esta): Docker
docker compose up --build            # o make up / make ui

# Conciliador (línea de comandos), desde la raíz, con su propio entorno
pip install -e ".[dev]"
python -m conciliador config/uganda_adjumani.yaml --validar
python -m pytest                     # tests del conciliador (los del backend: make test)
```

`DATA_DIR` de esta aplicación puede apuntar a la carpeta `TUM_SociAI/` que usa el conciliador: es el mismo
conjunto de datos.

**Verificación de la integración (15/11/2026):**

- Tests del conciliador: 57 de 57.
- Tests del backend **dentro de Docker** (imagen `api`, con Tesseract 5.5, PostgreSQL y los datos reales
  montados en `/data`): **332 de 332**, sin saltos, sobre una base de datos vacía.
- **Los tests con base de datos suponen que está vacía.** Después de lanzar un análisis desde la app (web o
  Streamlit), que sí guarda en la base, fallan 17 tests que comprueban cosas como «no hay ningún vínculo».
  Para no borrar lo analizado, ejecútalos contra una base aparte:
  ```bash
  docker compose exec db createdb -U revisor revisor_test      # una vez
  docker compose run --rm -e DATABASE_URL=postgresql+psycopg://revisor:revisor@db:5432/revisor_test       api sh -c "alembic upgrade head && pytest"
  ```
- Fuera de Docker (Python 3.12, sin Tesseract) pasan 322, se salta 1 y falla
  `test_streamlit_app.py::test_launching_the_analysis_runs_the_whole_pipeline`: espera incidencias que salen de
  leer los PDF con OCR. No es un fallo del código; para la batería completa hay que usar Docker o instalar
  Tesseract.
- App web probada a mano con Uganda (análisis completo, visor de PDF, decisiones, filtros, presupuesto y Excel).
- **Sin probar:** el cliente de Anthropic contra la API real (solo con una API simulada), la app web con
  Paraguay y la app Streamlit a mano.
