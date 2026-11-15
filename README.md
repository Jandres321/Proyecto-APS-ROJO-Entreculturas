# Revisor de justificación

Herramienta de apoyo a la revisión de la justificación económica de proyectos de cooperación
(Fundación Entreculturas · hackatón IA4GOOD). La IA **señala** incidencias con explicación; un
técnico las **acepta o descarta**. Salida principal: Excel. Contexto completo en [CLAUDE.md](CLAUDE.md).

## Este repositorio integra dos piezas

| Pieza | Dónde | Para qué |
|---|---|---|
| **Revisor de justificación** | `backend/` | La aplicación descrita en este README: proyectos (ejemplo, subida, Drive), lectura de PDF, cruce híbrido, reglas del donante, presupuesto y app de revisión web |
| **Conciliador** (línea de comandos, **legado**) | `src/conciliador/`, `config/`, `tests/` | El conciliador original por nombre de archivo, con un YAML por proyecto. Se conserva como herramienta de referencia y **ya no se mantiene**: su cruce está portado al backend (`pipeline/conciliador.py`), que es la única implementación que evoluciona. Ver [docs/conciliador.md](docs/conciliador.md) |

El estado de la integración está en [Integración con el conciliador](#integración-con-el-conciliador).

## Arranque rápido

Requisitos: Docker Desktop (con Compose v2).

```bash
cp .env.example .env      # y edita DATA_DIR con la ruta local de los datos confidenciales
docker compose up --build api   # o: make up
```

| Servicio | URL |
|---|---|
| **App de revisión (web)** | http://localhost:8000 (la sirve la propia API) |
| API | http://localhost:8000/api (docs en `/docs`, salud en `/health`) |
| PostgreSQL | localhost:5432 (usuario/clave/base: `revisor`) |

El contenedor `api` aplica las migraciones al arrancar y recarga el código en caliente.
`DATA_DIR` se monta **solo lectura** en `/data`, salvo `DATA_DIR/_proyectos` (archivos subidos y copias de
Drive). Los datos nunca se suben al repo.

## Comandos (Makefile)

| Comando | Acción |
|---|---|
| `make up` / `make down` | Levantar / parar el stack |
| `make migrate` | `alembic upgrade head` en el contenedor api |
| `make test` | `pytest` (los tests con datos reales se saltan si no hay `DATA_DIR`) |
| `make lint` | ruff (backend) |
| `make inspect` | Resumen por partida de los informes piloto (Uganda/Xunta y Paraguay/AECID) |
| `make read-pdfs` | Lee los PDFs de Paraguay (OCR + segmentación) y guarda los segmentos |
| `make match-report` | Cruce híbrido de los dos pilotos (`--solo-nombre` para el cruce por nombre de archivo): con documento, sin documento y huérfanos |
| `make evaluar-hibrido` | Mide conciliador solo / contenido solo / híbrido contra la numeración manual e imprime la tabla |
| `make compare-cruce` | Compara a nivel de pares nombre, contenido y ambos (y con PDFs renombrados al azar) |
| `make checks` | Ejecuta las comprobaciones de justificantes (`rules/<donante>.yaml`) sobre los dos pilotos |

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

### 3. `carpeta_completa` (por proyecto, en `project.match_config`)

| Valor | Proyecto | Un gasto sin documento es… |
|---|---|---|
| `True` (por defecto) | Uganda | `DOC_MISSING` (alta) |
| `False` | Paraguay (17 PDF para 1.321 gastos) | **«No incluido en la muestra»**: informativo, **sin incidencia** |

El estado de justificante de cada gasto (`review.document_statuses`) sale en el Excel (hoja *Gastos*) y es uno de:
verificado por contenido · propuesto por el nombre (sin verificar) · encontrado por contenido · vinculado a mano ·
sin justificante · no incluido en la muestra · no requiere justificante (coste indirecto / cofinanciación).

> La configuración de cruce de un proyecto es la de su donante (`match_config.py → DONOR_DEFAULTS`) con lo que
> el proyecto cambie en `project.match_config` (el equivalente del YAML del conciliador original). La de los
> proyectos de ejemplo está en `app/samples.py`.

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
  (`DATE_OUT_OF_PERIOD`) sale del periodo del proyecto (`start_date` / `end_date`, editable en los ajustes del
  proyecto): **si el proyecto no tiene periodo la regla no se aplica**. Los topes por partida no son por gasto: viven en
  [Presupuesto y topes](#presupuesto-y-topes-pipelinebudgetpy).
- `DONOR_STAMP_MISSING` y `NOT_AN_INVOICE` son heurísticos; `ILLEGIBLE` no tiene norma detrás (criterio de
  Entreculturas) y así consta en su `reference`.

## Evaluación con Tigray (`backend/eval/`)

El Excel `Resumen incidencias_ Respuestas EC.xlsx` (proyecto 22_ET_217 Tigray, Xunta) recoge las incidencias del
revisor externo y la respuesta de Entreculturas (EC); muchas son falsos positivos del revisor. Sirve de verdad de
referencia para medir las comprobaciones.

```bash
cd backend
python eval/convertir_tigray.py            # DATA_DIR/.../Resumen incidencias_ Respuestas EC.xlsx -> eval/tigray_ground_truth.csv
python eval/evaluar_tigray.py --proyecto ID            # incidencias nuestras desde la BD
python eval/evaluar_tigray.py --hallazgos nuestras.csv # o desde un CSV order_no,rule_code
```

- `tigray_ground_truth.csv` tiene una fila por incidencia: partida, nº de orden, factura, fecha, texto del revisor,
  comentario de EC, `categoria` (nuestros códigos de regla, `|` si son varios, `SIN_REGLA` si no tenemos regla) y
  `veredicto` (`real` si EC la acepta o la corrige, `falso_positivo` si EC demuestra que estaba bien,
  `indeterminado` si la respuesta no lo aclara). La clasificación es por palabras clave y prudente: revisa a mano las
  `indeterminado`. **Contiene datos reales: está en `.gitignore`, no se sube.**
- `evaluar_tigray.py` da, por categoría, cuántas incidencias reales detectamos (recall) y cuántos falsos positivos del
  revisor evitamos (no levantamos la misma regla sobre ese gasto). Las `SIN_REGLA` se listan pero no cuentan y las
  `indeterminado` quedan fuera de los porcentajes.
- Resultado de la conversión (14/11/2026 · 238 incidencias): 131 reales, 73 falsos positivos del revisor y 34
  indeterminadas; por categoría: 62 expediente incompleto, 60 fecha (13 también importe), 22 importe, 56 sin regla
  nuestra (reparto del importe entre trabajadores, descripción de la factura).
- **Limitación:** la carpeta de Tigray solo trae este Excel, sin el listado de gastos ni los PDF, así que **todavía no
  hay medición de nuestras detecciones**. Qué falta, en [docs/integracion_pendiente.md](docs/integracion_pendiente.md).

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

## Proyectos y fuentes de archivos (`app/services/`, `app/samples.py`)

Un **proyecto** es una fila de la tabla `project` y es la única fuente de verdad: no hay listas fijas en el
código ni registros en archivos JSON. Cada proyecto tiene:

| Campo | Para qué |
|---|---|
| `key` | Identificador en las URL (slug único: `uganda-adjumani`, `proyecto-subido`…) |
| `donor` | Decide el adaptador del informe y las reglas (`rules/<donante>.yaml`) |
| `source_type` | De dónde salen los archivos: `local` (carpeta ya en `DATA_DIR`), `upload` (subidos desde el navegador) o `drive` (carpeta de Google Drive) |
| `folder` / `report_file` | Carpeta de los archivos (relativa a `DATA_DIR`) y Excel del informe dentro de ella. Si no se indica el informe, se busca al lanzar el análisis |
| `start_date` / `end_date` | Periodo de ejecución (para las comprobaciones de fechas) |
| `match_config` | Lo que el proyecto cambia sobre la configuración de cruce por defecto de su donante (`pipeline/match_config.py → DONOR_DEFAULTS`) |
| `llm_mode` / `llm_max_pages` | Lectura de los PDF con IA externa: `off`, `dudosas` (solo lo que el OCR lee mal) o `todas`. Siempre sujeto a `ALLOW_EXTERNAL_LLM` en `.env` |
| `is_sample` | Proyectos de ejemplo: no se pueden borrar |

- **Proyectos de ejemplo** (`app/samples.py`): Uganda (Xunta) y Paraguay (AECID). Se crean al arrancar la API
  si su carpeta está en `DATA_DIR`. Su configuración de cruce específica (patrón de comprobantes de JRS Uganda,
  prefijos CG/FA de Paraguay, contrato de Yvy Pora…) es **dato** en ese archivo: el código de cruce no sabe
  nada de Uganda ni de Paraguay.
- **Archivos subidos y de Drive** se guardan en `DATA_DIR/_proyectos/<key>/` (el informe arriba y los
  justificantes en `justificantes/`; un `.zip` se descomprime, rechazando rutas que salgan de la carpeta y zips
  de más de 2 GB descomprimidos). Con Docker es el único punto con escritura de `DATA_DIR`.
- **Ejecuciones del análisis** (`app/services/jobs.py`): se guardan en la tabla `analysis_job` (estado, paso,
  progreso, mensaje). Sobreviven a un reinicio de la API (al arrancar, las que quedaron a medias se marcan como
  interrumpidas) y hay una sola a la vez por proyecto. Hoy se ejecutan en un hilo de la API (`launch_in_thread`);
  pasar a una cola de tareas solo cambia esa función.
- **Incidencias** (`app/services/findings.py → sync_findings`): cada paso del análisis sustituye sus incidencias
  abiertas y respeta las decisiones del revisor.

### API (`app/api/`)

| Ruta | Qué hace |
|---|---|
| `GET /api/settings` | Donantes, modos de IA, si hay `DATA_DIR`, estado de Drive y de la IA externa |
| `GET /api/projects` · `GET /api/projects/{key}` | Proyectos con nº de gastos y última ejecución |
| `POST /api/projects` | Crear desde una carpeta de `DATA_DIR` (`local`), una carpeta de Drive (`drive`, se comprueba el acceso al crearla) o vacío para subir archivos (`upload`) |
| `POST /api/projects/upload` | Crear subiendo de una vez el informe y los justificantes (formulario multipart) |
| `POST /api/projects/{key}/files` | Añadir archivos a un proyecto subido |
| `PATCH /api/projects/{key}` | Cambiar nombre, periodo, informe y lectura con IA |
| `DELETE /api/projects/{key}?delete_files=` | Borrar el proyecto y lo revisado (y sus archivos, si se pide). Los de ejemplo no |
| `POST /api/projects/{key}/run?reread=` · `GET .../run` | Lanzar el análisis y seguir su progreso |
| `GET /api/projects/{key}/review` · `.../budget` | Vista por gasto y presupuesto, para la app web |
| `GET /api/projects/{key}/report.xlsx` · `.../export.xlsx` | Informe del análisis y libro técnico |
| `GET /api/projects/{key}/findings` · `PATCH /api/findings/{id}` | Incidencias y decisión del revisor |
| `GET /api/documents/{id}/page/{n}.png?rotation=` · `.../file` | Página de un PDF (solo dentro de `DATA_DIR`) y el original |

Documentación interactiva en http://localhost:8000/docs.

## App de revisión web (`backend/app/web/`)

Interfaz única para el técnico. Es una página estática (`index.html`) con su lógica en módulos ES
(`web/js/`), que sirve la propia API en `/` y `/js/…` y que habla con `/api`. Sin framework, sin Node en
producción, sin recursos de internet (funciona sin conexión) y sin guardar datos en el navegador.

**Todo lo que se ve sale del servidor.** El veredicto de cada gasto (*Bien*, *Necesita revisión*, *No aplica*),
sus motivos y la comparación «Informe frente a PDF» los calcula el backend; la página no decide nada. Un dato que
no se ha leído dentro del PDF se muestra como *No leído* (en ámbar), nunca con el valor del listado.

Menú lateral fijo con el selector de proyecto y estos apartados (la dirección es `#/<proyecto>/<apartado>`,
y en la revisión `#/<proyecto>/revision/<id del gasto>`):

| Apartado | Qué hace |
|---|---|
| **Revisión** (por defecto) | A la izquierda, una línea por gasto: nº de orden, proveedor, importe del listado, estado y primer motivo; pestañas *Por revisar*, *Revisados*, *Bien*, *No aplica*, *Todos* y buscador. A la derecha, el gasto: «Informe frente a PDF» (campo, listado, leído, si coincide), motivos, incidencias en frases (con los *Datos técnicos* plegados), la página del PDF incrustada (pasar página, girar, zoom, abrir el original) y, fijos abajo, **Correcto** e **Incidencia real** con su nota |
| **Nuevo proyecto** | Subir el informe y los justificantes (PDF o .zip) o conectar una carpeta de Drive, con donante, lectura con IA y **periodo de ejecución obligatorio** |
| **Presupuesto y topes** | Aprobado frente a ejecutado por partida, topes del donante y reglas que se comprueban |
| **Incidencias generales** | Las que no cuelgan de un gasto (presupuesto, topes, documentos sin gasto), con *Es real* / *Falso positivo* / *Reabrir* y nota |
| **Ajustes** | Periodo, lectura con IA, informe, **Analizar** (con *Releer el informe*) y borrar el proyecto |

Al pie del menú, una línea dice qué hace el proyecto con la IA («Todo en local» o el proveedor y el modo).

- **Decidir es por gasto y de uno en uno.** No hay decisiones positivas en bloque. *Correcto* exige nota si algún
  motivo es bloqueante (lo comprueba también el servidor). Tras decidir se pasa al siguiente pendiente; si el
  guardado falla, no se avanza y se muestra el error.
- **Atajos:** `J`/`K` o `↓`/`↑` cambian de gasto, `C` es *Correcto*, `I` es *Incidencia real*, `←`/`→` pasan la
  página del PDF. No se disparan mientras se escribe; `Esc` sale de la nota.
- **Informe Excel:** *Descargar Excel* (`pipeline/report.py`), con las decisiones tomadas.
- **Datos de ejemplo:** con `?mock=1` (por ejemplo `http://localhost:8000/?mock=1`) la página funciona sin
  base de datos, con datos inventados de `web/js/mock.js`; `&fallo=1` hace fallar cada decisión para probar
  el error. Lo que solo funciona así está en [docs/pendiente_web.md](docs/pendiente_web.md).

*Releer el informe* recrea los gastos y descarta las decisiones sobre gastos concretos (las de presupuesto se
conservan); por eso no se hace por defecto al relanzar.

## Google Drive (`app/drive.py`)

Un proyecto `drive` guarda el id de la carpeta. Al lanzar el análisis, la carpeta se **copia** a
`DATA_DIR/_proyectos/<key>/` (solo lo nuevo o cambiado; lo borrado en Drive se borra de la copia; el manifiesto
de lo descargado queda fuera de la carpeta) y se analiza como cualquier otro proyecto. Permiso de solo lectura
(`drive.readonly`).

**Credenciales** (en `.secrets/`, ignorada por git; rutas en `GOOGLE_CREDENTIALS` / `GOOGLE_TOKEN`):

| Opción | Cómo | Cuándo |
|---|---|---|
| **Cuenta de servicio** (recomendada con Docker) | Google Cloud Console → proyecto → activar *Google Drive API* → *Credenciales* → *Cuenta de servicio* → *Claves* → JSON. Guárdalo como `.secrets/credentials.json` y **comparte la carpeta de Drive** (lector) con el correo de la cuenta de servicio (`client_email` del JSON) | Sin navegador; funciona dentro de Docker |
| **OAuth (app de escritorio)** | Mismo sitio → *ID de cliente OAuth* tipo *Aplicación de escritorio* → JSON como `.secrets/credentials.json`. Después, **una vez y fuera de Docker**: `cd backend && python -m app.drive` (abre el navegador y guarda `.secrets/token.json`) | Si no se puede compartir con una cuenta de servicio |

Sin credenciales o sin el extra `drive`, el resto de la app funciona y la página explica qué falta. Probado con
`tests/test_drive_source.py` (Drive simulado, sin red). **Sin probar con una cuenta real.**

## Cómo ejecutar y probar

```bash
cp .env.example .env            # DATA_DIR = ruta local de TUM_SociAI (con DocJustificativa/)
mkdir -p .secrets               # opcional: credentials.json para Google Drive
docker compose up --build api   # PostgreSQL + API (migraciones automáticas) · o: make up
```

Abre **http://localhost:8000**.

1. **Proyecto de ejemplo:** Uganda o Paraguay → *Lanzar análisis* (la primera vez el OCR tarda; luego usa la
   caché) → revisa gastos, *Ver en el PDF*, decide alguna incidencia → *Descargar informe Excel*.
2. **Subida:** *Nuevo proyecto* → *Subir archivos* → nombre, donante, el Excel del informe y los justificantes
   (varios PDF o un .zip) → *Crear proyecto* → *Lanzar análisis*.
3. **Drive:** *Nuevo proyecto* → *Google Drive* → enlace de la carpeta (con el informe y los PDF) → *Crear
   proyecto* → *Lanzar análisis* (descarga y analiza).
4. **Tests:** `make test`. Los de base de datos suponen una base **vacía** (ver «Verificación» más abajo para
   usar una base aparte); sin PostgreSQL o sin `DATA_DIR` se saltan.

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
```

Nueva migración tras cambiar `app/models.py`: `alembic revision --autogenerate -m "descripción"`.

## Estructura

```
backend/app/
  main.py, config.py, db.py, models.py, schemas.py, enums.py
  samples.py      proyectos de ejemplo (Uganda, Paraguay) y su configuración de cruce, como datos
  services/       projects (crear, ajustes, borrar, localizar el informe), sources (subida y Drive a disco),
                  jobs (ejecuciones del análisis en la tabla analysis_job), findings (sync_findings)
  api/            settings, projects, documents, findings
  adapters/       un adaptador por donante (xunta, aecid) tras la interfaz DonorAdapter
  pipeline/       ingest -> match -> checks / classify / budget -> report / export (run.py los encadena)
                  match: conciliador.py (por nombre) + match_content.py (contenido) -> match_hybrid.py
  lectura/        OCR, segmentación y lectura opcional con IA de los PDF
  llm/            interfaz LLMClient + factory (fake, anthropic)
  drive.py        Google Drive (listar y copiar una carpeta)
  review.py, review_view.py   decisiones del revisor y vista por gasto para la app web
  web/            app de revisión web: index.html (que sirve la API en /) y sus módulos en js/
backend/alembic/  migraciones
backend/rules/    reglas por donante (YAML)
src/conciliador/  conciliador original por línea de comandos (legado)
```

## Estado

| Pieza | Estado |
|---|---|
| Proyectos en base de datos (ejemplo, subida, Drive), API de proyectos, ejecuciones persistidas | Hechos |
| Adaptadores Xunta y AECID, ingesta, lectura de PDF (OCR), cruce híbrido, comprobaciones | Hechos |
| Presupuesto y topes, partida sugerida (2b), revisión de incidencias, informe Excel | Hechos |
| App de revisión web (única interfaz) | Hecha |
| Lectura con IA externa | Opción de cada proyecto (`llm_mode`: no / solo páginas dudosas / todas, con tope `llm_max_pages`); el coste estimado sale en el mensaje del análisis. Falta guardarlo y enseñarlo con detalle en la web (ver `docs/integracion_pendiente.md`) |
| Google Drive | Hecho; sin probar con credenciales reales |

## Arquitectura: lo que falta para producción

Decisiones tomadas para que la demo pueda crecer a producto sin rehacerla, y lo que queda:

| Tema | Hoy | Siguiente paso |
|---|---|---|
| Ejecución del análisis | Hilo en la API, estado en `analysis_job` | Cola de tareas (RQ/Celery) sustituyendo `jobs.launch_in_thread` |
| Archivos | Disco local (`DATA_DIR`, `_proyectos/`) | Almacenamiento de objetos (S3/Azure/GCS) detrás de `services/sources.py` |
| Usuarios | Sin autenticación (uso local) | Usuarios, roles (técnico / revisor) y registro de quién decide cada incidencia |
| Plantillas de informe | Un adaptador programado por donante | Plantillas configurables (hoja, cabecera, columnas) sin programar |
| Configuración de cruce | `project.match_config` editable solo por API | Editor en la interfaz |
| Código | `pipeline/checks.py`, `match_content.py` y `match_hybrid.py` muy grandes | Dividir en módulos (checks: en otra rama) |
| Tests | Los de base de datos suponen base vacía | Aislar con base de datos propia por sesión de tests |
| Evaluación | Validación contra la numeración manual (Uganda) | Verdad de referencia de Tigray (otra rama) |
| Observabilidad | Logs de uvicorn | Logs estructurados, métricas de coste de IA y de tiempos |

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
| Un YAML por proyecto | Equivalente en el backend: `project.match_config` sobre los valores por defecto del donante |
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
docker compose up --build api        # o make up

# Conciliador (línea de comandos), desde la raíz, con su propio entorno
pip install -e ".[dev]"
python -m conciliador config/uganda_adjumani.yaml --validar
python -m pytest                     # tests del conciliador (los del backend: make test)
```

`DATA_DIR` de esta aplicación puede apuntar a la carpeta `TUM_SociAI/` que usa el conciliador: es el mismo
conjunto de datos.

**Verificación (15/11/2026, rama `refactor/proyectos`):**

- Tests del conciliador: 57 de 57.
- Tests del backend con PostgreSQL y sin `DATA_DIR`: 307 pasan y 29 se saltan (los que necesitan los datos
  reales o Tesseract con los datos). Migración 0006 probada hacia arriba, hacia abajo y otra vez hacia arriba.
- Prueba de punta a punta contra la API (uvicorn + PostgreSQL) con los datos de Paraguay: proyecto de ejemplo
  creado al arrancar, análisis completo (1.321 gastos, 17 documentos) e informe Excel; proyecto **subido** desde
  la API (informe + 3 PDF), analizado y borrado con sus archivos.
- **Los tests con base de datos suponen que está vacía.** Después de lanzar un análisis desde la app, que sí
  guarda en la base, fallan los que comprueban cosas como «no hay ningún vínculo». Para no borrar lo analizado,
  ejecútalos contra una base aparte:
  ```bash
  docker compose exec db createdb -U revisor revisor_test      # una vez
  docker compose run --rm -e DATABASE_URL=postgresql+psycopg://revisor:revisor@db:5432/revisor_test api sh -c "alembic upgrade head && pytest"
  ```
- **Sin probar:** Google Drive y el cliente de Anthropic contra los servicios reales (solo simulados), y la
  interfaz nueva de «Nuevo proyecto» a mano en el navegador.
