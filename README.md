# Proyecto APS ROJO – Entreculturas

Herramienta de apoyo para **agilizar la justificación económica** de los proyectos de cooperación y ayuda humanitaria de la Fundación Entreculturas.

> **Este README es la fuente de verdad del proyecto.** Está pensado para que cualquier persona, o cualquier IA (Claude, ChatGPT, Gemini, Copilot…), entienda el proyecto completo leyendo solo este archivo. Hay que **actualizarlo en cada cambio** (ver [Mantener este README](#13-mantener-este-readme)).

**Estado (14/11/2026):** fase 0 terminada. Ya funciona un **conciliador gasto ↔ justificante** que por defecto no usa IA. Está probado con 2 proyectos reales de donantes distintos: Uganda (Xunta) y ECHO Colombia. Los documentos pueden estar en una **carpeta local o en Google Drive**; la conexión con Drive está probada con una API simulada y falta probarla con el Drive real de Entreculturas. La **lectura del contenido de los PDF** (fase 1) está programada como opción desactivada por defecto y probada solo con una API simulada: **no se ha ejecutado con documentos reales**, a la espera de que Entreculturas confirme el proveedor de IA.

---

## Índice
1. [El reto](#1-el-reto)
2. [Datos disponibles](#2-datos-disponibles)
3. [Antecedente: MVP de TUM SociAI](#3-antecedente-mvp-de-tum-sociai)
4. [Estructura del repositorio](#4-estructura-del-repositorio)
5. [Cómo funciona el conciliador](#5-cómo-funciona-el-conciliador)
6. [Configuración por proyecto (YAML)](#6-configuración-por-proyecto-yaml)
7. [Instalación y uso](#7-instalación-y-uso) (incluye [Lectura del contenido](#lectura-del-contenido) y [Google Drive](#google-drive))
8. [El Excel de salida](#8-el-excel-de-salida)
9. [Resultados](#9-resultados)
10. [Decisiones de diseño](#10-decisiones-de-diseño)
11. [Limitaciones conocidas](#11-limitaciones-conocidas)
12. [Hoja de ruta y preguntas abiertas](#12-hoja-de-ruta-y-preguntas-abiertas)
13. [Mantener este README](#13-mantener-este-readme)
14. [Registro de cambios](#14-registro-de-cambios)

---

## 1. El reto

Entreculturas justifica ante donantes (AECID, Xunta de Galicia, Gobierno de La Rioja, ECHO…) el gasto de cada proyecto. Hoy un técnico revisa a mano cada justificante frente al **listado de gastos** y la **normativa del donante**. Si algo falla, se lo devuelve al socio local y el ciclo se repite. Cerrar un informe lleva entre **2 y 4 meses**.

Lo que piden:
- **Señalar incidencias y explicarlas**, no decidir. Siempre con revisión humana (*human-in-the-loop*).
- No hace falta un 100 % de precisión.
- La salida, en **Excel**.
- Los documentos están en **Google Drive**.
- Según Entreculturas, lo que más tiempo consume es **revisar facturas y detectar incidencias**.

**Proceso 1: revisar la documentación justificativa.** Comprobar que cada justificante cumple la normativa y coincide con su línea del listado. Las incidencias más frecuentes son:
- recibos o proformas presentados como factura
- pagos en efectivo sin sello de "pagado" ni firma del proveedor
- documentos ilegibles
- falta el justificante de pago o los soportes complementarios (nóminas, contratos, listas de asistencia…)
- falta el sello del donante (expediente, convocatoria, % de imputación)
- fecha, importe o concepto que no coinciden
- idioma no admitido

**Proceso 2: revisar la imputación presupuestaria.** Comprobar que cada gasto está en la partida correcta, que los totales por partida del informe económico cuadran con el presupuesto aprobado y que se respetan los topes del donante. Por ejemplo, en la Xunta: personal ≤ 70 %, indirectos ≤ 5 %, auditoría ≤ 2.500 €.

El enunciado oficial está en `Reto Entreculturas-1.pdf`.

## 2. Datos disponibles

Toda la documentación de Entreculturas está en la carpeta **`TUM_SociAI/`**. **No se versiona** porque contiene datos personales y financieros (nóminas, contratos, extractos bancarios). Cada persona del equipo debe copiarla en la raíz del repositorio.

```
TUM_SociAI/
├── Cuestiones procedimiento TUM versión 091225.docx  # Alcance: Excel de salida, Drive, recomendador con explicación
├── Preguntas iniciales.docx                          # Flujo actual, incidencias frecuentes, organización de carpetas
├── Notas reuniones.docx
├── Revisión informes economicos.docx                 # El enunciado del reto en Word
├── Prototipo/                                        # MVP de TUM (ver sección 3)
│   ├── Rules.xlsx                                    # ~33 reglas de la Xunta + 13 reglas genéricas de factura
│   ├── Entreculturas MVP Presentation.pdf
│   └── Justificantes/                                # Muestra: Marruecos, Sudáfrica, Uganda
└── DocJustificativa/                                 # 6 proyectos reales ya justificados
```

Casi todos los PDF son **escaneados, sin capa de texto**: para leer su contenido hará falta OCR o un LLM con visión.

| Carpeta en `DocJustificativa/` | Donante · país | Qué contiene | Uso en el proyecto |
|---|---|---|---|
| `24-UG-226 Adjumani - Xunta 2024` | Xunta · Uganda | Normativa, presupuesto, informe final con la **relación de facturas (203 gastos)** y **~100 justificantes** numerados | **Piloto** del conciliador |
| `23-CO-034 - ECHO` | ECHO · Colombia | Listado de ~3.000 gastos, presupuesto (MR), guías de ECHO y **12 PDF de muestra** | **Segundo caso** del conciliador |
| `22_ET_217 Crisis Tigray Addis - Xunta de Galicia` | Xunta · Etiopía | Facturas antes y después de corregir y **lista de incidencias del revisor con las respuestas de Entreculturas** | Futuro: evaluar la detección de incidencias (proceso 1) |
| `21-CO-035 Gobierno de la Rioja` | La Rioja · Colombia | Bases reguladoras, presupuesto, informe (listado + resumen por partida) y 19 justificantes | Futuro: proceso 2 |
| `22-PY-136 - AECID` | AECID · Paraguay | Formulario oficial AECID completo (balance presupuestario, listado de comprobantes…) y ~18 justificantes | Futuro: proceso 2 en formato AECID |
| `22-22LB-02 - Líbano Convenio AECID 2023` | AECID · Líbano | Listado parcial con el nombre del PDF de cada gasto y ~42 justificantes | Poco útil (el cruce ya viene hecho) |

## 3. Antecedente: MVP de TUM SociAI

Un equipo de estudiantes de la TUM hizo un MVP con **n8n + Gemini + Google Sheets**:
1. La factura se sube a Drive.
2. Gemini extrae sus datos.
3. Se evalúa contra `Rules.xlsx` con un veredicto correcto / dudoso / incorrecto por regla.
4. Se avisa por email.

**Limitaciones:**
- Se probó con solo 10 facturas.
- Las reglas son de un solo donante (Xunta).
- No cubre el proceso 2.
- El workflow de n8n **no está** en la documentación.

Nosotros no partimos de su código. Sus reglas (`Rules.xlsx`) servirán para la fase 2.

## 4. Estructura del repositorio

```
.
├── README.md                  # ESTE archivo: contexto completo del proyecto (mantener al día)
├── AGENTS.md                  # Instrucciones para asistentes de IA (Codex, Cursor, Copilot…)
├── CLAUDE.md                  # Remite a AGENTS.md (Claude Code)
├── config/                    # Un YAML por proyecto: describe sus datos
│   ├── uganda_adjumani.yaml   #   piloto (Xunta, Uganda)
│   ├── echo_colombia.yaml     #   segundo caso (ECHO, Colombia)
│   ├── ejemplo_drive.yaml     #   plantilla para un proyecto con documentos en Google Drive
│   └── prueba_contenido.yaml  #   prueba de la lectura del contenido con pocas facturas (Uganda)
├── src/conciliador/           # Código del conciliador
│   ├── __main__.py            #   CLI: lee config → cruza → valida → escribe Excel
│   ├── modelos.py             #   Estructuras: Documento, Gasto, Candidato, Resultado
│   ├── texto.py               #   Normalización: acentos, plurales, sinónimos, referencias, meses, formato de importes
│   ├── listado.py             #   Lee el listado de gastos (.xls/.xlsx) → lista de Gasto
│   ├── fuentes.py             #   Lista documentos: CarpetaLocal o CarpetaDrive (misma interfaz)
│   ├── drive.py               #   (opcional) Google Drive: autenticación, listado recursivo, descarga del listado
│   ├── cruce.py               #   Motor de cruce: puntúa cada par gasto-documento y decide
│   ├── lectura.py             #   (opcional) Lee el contenido de un documento con la API de Claude, con caché
│   ├── validacion.py          #   Métricas comparando con la numeración manual
│   └── salida.py              #   Genera el Excel para el revisor
├── tests/                     # pytest (sin datos reales; Drive y la API de Claude simuladas)
├── .secrets/                  # Credenciales y tokens de Google (NO se versiona)
├── .env.example               # Plantilla de .env (clave de la API de Anthropic). El .env NO se versiona
├── output/                    # Excels generados y caché de lecturas (NO se versiona: datos personales)
├── TUM_SociAI/                # Datos de Entreculturas (NO se versiona)
├── pyproject.toml             # Dependencias y extras opcionales: dev, drive, contenido (única fuente de verdad)
├── requirements.txt           # Atajo: instala el proyecto con todos los extras
└── Reto Entreculturas-1.pdf   # Enunciado oficial
```

**Cómo se reparte el código:** `modelos.py` y `texto.py` no dependen de nada; `listado.py` y `fuentes.py` leen los datos; `cruce.py` decide (no toca disco ni red); `salida.py` y `validacion.py` presentan el resultado; `__main__.py` los encadena. `drive.py` y `lectura.py` son los únicos que hablan con servicios externos y sus librerías se importan solo al usarse.

Ignorados por git: `TUM_SociAI/`, `output/`, `.secrets/`, `credentials*.json`, `token*.json`, `CONTEXTO.md`, `.env`, `.venv/`.

## 5. Cómo funciona el conciliador

**Objetivo:** asociar cada línea del listado de gastos con el documento que la justifica, y señalar los gastos sin documento y los documentos que sobran. **Por defecto solo usa los nombres de los archivos**: no abre los PDF ni usa IA. Es barato, rápido y respeta la privacidad. De forma opcional puede [leer el contenido](#lectura-del-contenido-opcional) de los documentos dudosos.

```
 config/<proyecto>.yaml
          │
          ▼
 listado.py ──► Gastos ─┐            (listado local o descargado de Drive)
                        ├─► cruce.py ──► Resultados ──► salida.py ──► output/<proyecto>_<fecha>_<hora>.xlsx
 fuentes.py ──► Docs  ──┘   (texto.py)        │
 (carpeta local o Drive)       ▲              └──► validacion.py (opcional, --validar)
                               │
                 lectura.py (opcional, --contenido): lee los documentos dudosos
                 y cruce.py vuelve a decidir solo esos gastos
```

### Paso a paso
1. **Preparar cada documento** a partir de su nombre:
   - Se **oculta el nº de orden manual** ("1. ", "10.A ", "_50_51_" …), porque esa numeración la hizo una persona a mano.
   - Se extraen las **referencias** (nº de factura o código contable), los **meses** (por nombre o por fecha dd.mm.aa) y el **concepto**: las palabras sin acentos, en singular, sin palabras vacías y con los sinónimos unificados.
2. **Preparar cada gasto** del listado: las referencias salen del nº de factura y de las columnas extra que indique el YAML; el concepto sale de la descripción, el proveedor y la factura; el mes, de la descripción o de la fecha.
3. **Puntuar cada par gasto–documento** (`cruce.py → puntuar`):

   | Señal | Efecto en la puntuación |
   |---|---|
   | Similitud de concepto `sim` (0–1) | Base = `0,85 × sim` |
   | Misma referencia (prefijo y número) | `+0,35`. Si la referencia está en **un único** documento, mínimo garantizado de `0,62` |
   | Mismo número con otro prefijo (EB/CV) | `+0,20` |
   | Los dos tienen referencia y no coinciden | `−0,15` |
   | El documento nombra el mes del gasto | `+0,08` (mes contiguo: `+0,04`) |
   | El documento nombra otro mes | `−0,25` |

   `sim = 0,35 × (palabras del documento que están en el gasto) + 0,50 × (palabras de la descripción que están en el documento) + 0,15 × similitud global`. La segunda parte premia a los documentos **específicos** frente a los genéricos.
4. **Decidir:**
   - Se asigna el mejor documento si la puntuación es ≥ `0,60`.
   - **Confianza alta:** ≥ `0,80` y sin rival cercano.
   - **Baja (ambiguo):** si otro documento distinto queda a menos de `0,05`.
   - **Media:** el resto.
5. **Clasificar los gastos sin documento:**
   - **cofinanciación:** imputado entero a otros financiadores, sin nada al donante.
   - **coste indirecto:** su partida cumple `partidas_indirectos`; se justifica con certificado.
   - **revisar:** el resto.
6. **Explicar:** cada resultado lleva un **motivo** en lenguaje natural para el revisor.
7. **Validar** (opcional, `--validar`): se compara con la numeración manual (nº de orden del listado ↔ nº de orden del nombre del archivo).

Los pesos y umbrales tienen valores por defecto en `cruce.py → DEFECTOS` y se pueden cambiar en el YAML de cada proyecto.

### Lectura del contenido (opcional)

Habrá archivos mal nombrados, y la mayoría son escaneos (algunos manuscritos). Con `--contenido` (o `contenido.activado: true`), después del cruce por nombre se **leen algunos documentos** y se vuelven a decidir los gastos dudosos. **Está desactivado por defecto porque envía los documentos a un servicio externo** (la API de Anthropic).

1. **Qué documentos se leen**, por este orden y hasta `max_documentos` llamadas por ejecución (`cruce.py → documentos_a_leer`):
   1. los `candidatos` (3) mejores documentos de cada gasto con **confianza baja**;
   2. los 3 mejores de cada gasto **sin justificante a revisar** (no los de cofinanciación ni indirectos);
   3. los **documentos sin gasto**.

   Solo se leen PDF e imágenes (JPG, PNG) de una carpeta local.
2. **Cómo se lee cada uno** (`lectura.py → leer_documento`):
   - Las primeras `max_paginas` (3) páginas del PDF se convierten en JPEG a 130 dpi con PyMuPDF.
   - Se mandan en **una sola llamada** al modelo `claude-sonnet-5-5`, que responde con un JSON de esquema fijo (salida estructurada): `tipo`, `importe_total`, `moneda`, `fecha`, `proveedor`, `num_factura`, `manuscrito`, `legible`.
   - Las instrucciones piden dejar vacío lo que no se lea, sin inventarlo.
   - **Caché:** el resultado se guarda en `output/cache_lectura/`, con clave = hash del archivo (más el modelo y `max_paginas`). Un documento ya leído no vuelve a costar nada, aunque cambie de nombre.
   - Si una lectura falla (respuesta cortada, rechazada o error de la API), el documento queda como «no leído» y se sigue con el resto. Los fallos no se guardan en la caché.
3. **Señales nuevas** al puntuar un par gasto–documento leído (`cruce.py → _senales_contenido`):

   | Señal | Efecto en la puntuación |
   |---|---|
   | El importe del gasto coincide con el total del documento (±1 %) | `+0,30` |
   | Dentro del documento se lee el nº de factura del gasto | `+0,35` |
   | Dentro del documento se lee otro nº de factura | `−0,15` |
   | La fecha del documento está a ±3 días de la del gasto | `+0,10` |
   | El proveedor del documento se parece al del gasto | `+0,10` |

   - El importe se compara en **moneda local** (`listado.columnas.importe_local`); el importe en euros solo se usa si el documento está en euros.
   - Un importe distinto **no resta**: un documento puede cubrir varios gastos.
   - Un documento ilegible no suma ni resta; el motivo lo dice.
4. **Volver a decidir** (`cruce.py → revisar_con_contenido`): solo los gastos del punto 1. Los gastos con confianza alta o media no se tocan.
5. **Conflicto entre nombre y contenido:** el gasto sale con **confianza baja** y un aviso en el motivo si
   - por el nombre el justificante sería un documento, pero el contenido de otro encaja mejor; o
   - el documento elegido tiene dentro otro nº de factura, o ni el importe ni la fecha ni el proveedor coinciden (habiendo al menos dos de ellos para comparar).
6. El **motivo** explica lo que se ha leído dentro del documento y, si el resultado ha cambiado, cuál era solo por el nombre.

Los pesos tienen valores por defecto en `cruce.py → DEFECTOS_CONTENIDO` y se cambian en la sección `contenido:` del YAML.

## 6. Configuración por proyecto (YAML)

Todo lo específico de un proyecto está en `config/<proyecto>.yaml`. **Un proyecto nuevo es un YAML nuevo, sin tocar código.** Las rutas son relativas a la raíz del repositorio y las columnas se indican por número (A = 0).

| Clave | Para qué sirve |
|---|---|
| `proyecto` | Nombre que aparece en el Excel |
| `documentos.tipo` | `local` o `drive` |
| `documentos.ruta` | (local) carpeta de los justificantes |
| `documentos.carpeta` | (drive) URL o id de la carpeta de Drive |
| `documentos.recursivo` / `excluir` | Recorrer subcarpetas (por defecto sí) y patrones de archivos a ignorar (normativa, el propio listado…) |
| `drive.credenciales` / `drive.token` | (drive) JSON de credenciales de Google y dónde guardar el token. Por defecto `.secrets/credentials.json` y `.secrets/token.json` |
| `listado.archivo` / `hoja` | Excel local con el listado de gastos y su hoja |
| `listado.drive` | (opcional) URL del listado en Drive (Excel u Hoja de cálculo de Google). Si se indica, tiene prioridad sobre `archivo` y necesita permiso de lectura |
| `listado.columnas` | Posición de: `partida`, `orden` (solo para validar), `fecha`, `factura`, `proveedor`, `descripcion`, `importe_eur`, `importe_local` (opcional; se compara con el total leído en el documento), `pago` |
| `listado.columnas_referencia` | Columnas extra con referencias (p. ej. el código contable en ECHO) |
| `listado.imputacion.donante` / `otros` | Columnas con el importe imputado al donante y a otros financiadores |
| `listado.partidas_indirectos` | Regex de las partidas de costes indirectos |
| `listado.obligatorias` | Campos que debe tener una fila para contar como gasto |
| `referencias.patron` | Regex con los grupos `prefijo` y `numero` para reconocer el nº de factura o el código contable |
| `referencias.numeros_largos` | Tratar números sueltos de 5 o más cifras como referencia |
| `prefijo_orden` | Regex del nº de orden manual en el nombre del archivo: grupos `desde`/`hasta`/`sufijo` o `lista`. Se oculta al cruzar y se usa al validar |
| `cruce.*` | Pesos y umbrales (ver sección 5). `conceptos_mes_vencido`: conceptos que se pagan el mes siguiente |
| `sinonimos` | Palabras equivalentes del dominio (`salario: [nomina, payroll]`) |
| `contenido.activado` | Leer el contenido de los documentos dudosos. Por defecto `false`; también se activa con `--contenido` |
| `contenido.modelo` | Modelo de la API de Claude (por defecto `claude-sonnet-5-5`) |
| `contenido.max_paginas` | Páginas que se leen de cada PDF (por defecto 3) |
| `contenido.max_documentos` | Tope de llamadas a la API por ejecución (por defecto 50). Los documentos ya leídos salen de la caché y no cuentan |
| `contenido.candidatos` | Documentos candidatos que se leen por cada gasto dudoso (por defecto 3) |
| `contenido.bonus_*`, `penalizacion_factura`, `tolerancia_importe`, `dias_fecha`, `similitud_proveedor` | Pesos y tolerancias de las señales del contenido (ver sección 5) |

Ejemplos completos: `config/uganda_adjumani.yaml` (nº de factura `EB UGA04 602`) y `config/echo_colombia.yaml` (código contable `206-15000`).

## 7. Instalación y uso

Requisitos: **Python 3.10 o superior** y la carpeta `TUM_SociAI/` en la raíz del repositorio.

**Windows, en la terminal cmd de VS Code** (abre la carpeta del repositorio en VS Code):
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e ".[dev]"
```
Con eso queda instalado. Después, para usarlo:
```cmd
python -m conciliador config/uganda_adjumani.yaml --validar
python -m conciliador config/echo_colombia.yaml --validar
python -m pytest
```
- En cada terminal nueva hay que activar antes el entorno con `.venv\Scripts\activate.bat`. No hace falta si en VS Code eliges el intérprete `.venv` (`Ctrl+Shift+P` → *Python: Select Interpreter*).
- En PowerShell la activación es `.\.venv\Scripts\Activate.ps1`; en Linux/Mac, `source .venv/bin/activate`.

| Opción | Efecto |
|---|---|
| `--validar` | Mide el acierto con la numeración manual. Solo tiene sentido en proyectos ya justificados |
| `--salida ruta.xlsx` | Guarda el Excel exactamente en esa ruta, sin fecha ni hora (por defecto `output/<nombre_del_yaml>_<AAAAMMDD>_<HHMMSS>.xlsx`) |
| `--raiz carpeta` | Raíz para las rutas relativas del YAML (por defecto, la carpeta actual) |
| `--listar-documentos` | Solo lista los documentos encontrados y termina. Sirve para probar la conexión con Drive |
| `--contenido` | Lee además el contenido de los documentos dudosos con la API de Claude (ver *Lectura del contenido*). **Envía documentos a un servicio externo** |

**Cada ejecución genera un Excel nuevo** con la fecha y la hora en el nombre (p. ej. `output/uganda_adjumani_20261114_113404.xlsx`), para poder comparar ejecuciones y pruebas sin pisar las anteriores. Los Excel antiguos no se borran solos: limpia `output/` de vez en cuando. Con `--salida`, si ese Excel está abierto, se guarda una copia con la fecha y hora en el nombre.

### Lectura del contenido

> **No lo ejecutes con documentos de `TUM_SociAI/` hasta que Entreculturas confirme que se puede usar este proveedor** (ver preguntas abiertas, sección 12). Los tests no llaman a la API.

1. Instala las librerías (una vez): `pip install -e ".[dev,contenido]"` (`anthropic`, `pymupdf`, `python-dotenv`).
2. Copia `.env.example` como `.env` en la raíz del repositorio y pon tu clave en `ANTHROPIC_API_KEY`. El `.env` no se sube a git; el programa nunca muestra la clave.
3. Ejecuta con `--contenido`:
   ```cmd
   python -m conciliador config/mi_proyecto.yaml --contenido
   ```
   Antes de llamar, el programa dice cuántos documentos va a enviar y cuántos salen de la caché. Al terminar, cuántos ha leído, cuántos han quedado sin leer por el tope y en cuántos gastos ha cambiado el resultado.
4. Para controlar el gasto, empieza con un `contenido.max_documentos` bajo. Lo ya leído queda en `output/cache_lectura/`: repetir la ejecución no vuelve a pagar por esos documentos. Para forzar una lectura nueva, borra esa carpeta.

De momento solo funciona con documentos en una **carpeta local**. Con Drive se avisa y se cruza solo por nombre.

#### Primera prueba con pocas facturas

`config/prueba_contenido.yaml` es el YAML de Uganda apuntando a una carpeta pequeña, `TUM_SociAI/prueba_contenido/` (no se versiona), con la lectura activada y un tope de **5 llamadas**. **Solo los archivos que copies ahí se envían a la API**, así que eliges tú qué sale del equipo.

1. Crea el `.env` (si no existe, copia `.env.example`) y pega tu clave después de `ANTHROPIC_API_KEY=`, sin comillas ni espacios.
2. Copia en `TUM_SociAI/prueba_contenido/` entre 3 y 5 facturas de `DocJustificativa/24-UG-226 Adjumani - Xunta 2024/`, **con su nombre original** (la validación usa el nº de orden del nombre). Evita nóminas, contratos y cualquier documento con datos personales.
3. Comprueba qué se va a enviar, sin gastar nada:
   ```cmd
   python -m conciliador config/prueba_contenido.yaml --listar-documentos
   ```
4. Ejecuta la prueba:
   ```cmd
   python -m conciliador config/prueba_contenido.yaml --validar
   ```
   Imprime cuántos documentos envía y, al final, cuántos ha leído, cuántos han fallado y en cuántos gastos cambia el resultado.
5. Abre el Excel nuevo de `output/` y mira:
   - **Documentos**: las columnas *Leído por contenido* (`sí`, `ilegible` o `no leído: motivo`) y *Datos del contenido*. Compara lo leído con el PDF: ¿importe, fecha, proveedor y nº de factura son correctos?
   - **Gastos**: el *Motivo* de los gastos que se han cruzado con esos documentos; explica lo que se ha leído dentro.
   - **Resumen**: el bloque *Lectura del contenido*.
6. Repite la ejecución: no debe volver a llamar a la API (los documentos ya leídos están en `output/cache_lectura/`). Para empezar de cero, borra esa carpeta.

Como el listado es el de todo el proyecto (203 gastos) y la carpeta solo tiene unas pocas facturas, casi todos los gastos saldrán sin justificante: es lo esperado, lo que importa es la lectura de esos documentos.

### Google Drive

El conciliador puede leer los documentos **directamente de una carpeta de Google Drive**, subcarpetas y unidades compartidas incluidas.

- Para listarlos solo pide permiso de **metadatos** (`drive.metadata.readonly`): ve nombres y enlaces, **nunca el contenido** de los archivos.
- En el Excel, cada documento enlaza a su página en Drive.
- El listado de gastos puede seguir en local o descargarse de Drive con `listado.drive`. En ese caso el permiso pasa a ser de **lectura** (`drive.readonly`), porque hay que abrir el Excel.

**1. Instalar las librerías de Google** (una vez):
```cmd
pip install -e ".[dev,drive]"
```
Para Drive se recomienda Python 3.11 o superior: las librerías de Google avisan de que dejarán de dar soporte a la 3.10.

**2. Crear las credenciales** (una vez por equipo u organización). Hay dos opciones:

*Opción A, con tu cuenta de Google (OAuth)*. Es la más sencilla para probar:
1. Entra en [Google Cloud Console](https://console.cloud.google.com/) y crea un proyecto, o usa uno existente.
2. *APIs y servicios → Biblioteca* → activa **Google Drive API**.
3. *APIs y servicios → Pantalla de consentimiento OAuth*: tipo *Interno* si es una cuenta de la organización, o *Externo* añadiendo tu correo como usuario de prueba.
4. *Credenciales → Crear credenciales → ID de cliente de OAuth → Aplicación de escritorio* → descarga el JSON.
5. Guárdalo como **`.secrets/credentials.json`** en la raíz del repositorio.
6. La primera ejecución abrirá el navegador para dar permiso. El token se guarda en `.secrets/` y no vuelve a preguntar.

*Opción B, con una cuenta de servicio*. Sin navegador, para un uso automatizado:
1. Pasos 1 y 2 de la opción A.
2. *Credenciales → Crear credenciales → Cuenta de servicio* → en ella, *Claves → Agregar clave → JSON*.
3. Guarda el JSON como `.secrets/credentials.json`.
4. **Comparte la carpeta de Drive** (y el listado, si está en Drive) con el email de la cuenta de servicio (`…@….iam.gserviceaccount.com`), con permiso de *Lector*.

El programa detecta solo qué tipo de credencial es. **Las credenciales nunca se suben a git** (`.secrets/` está en el `.gitignore`).

**3. Configurar el proyecto:** copia `config/ejemplo_drive.yaml`, pega la URL de la carpeta en `documentos.carpeta` y ajusta el listado y las columnas.

**4. Probar la conexión y ejecutar:**
```cmd
python -m conciliador config/mi_proyecto.yaml --listar-documentos
python -m conciliador config/mi_proyecto.yaml
```

**Problemas frecuentes:**
- `ModuleNotFoundError: conciliador`: el entorno no está activado.
- `No existe la carpeta de documentos`: falta `TUM_SociAI/` o la ruta del YAML no coincide.
- Casi todo sale sin justificante: revisa los números de columna en `listado.columnas`.
- `No encuentro el archivo de credenciales de Google`: falta `.secrets/credentials.json` (ver *Google Drive*).
- Drive devuelve 0 documentos: la cuenta no tiene acceso a la carpeta. Con cuenta de servicio, comparte la carpeta con su email.
- Error 403 *accessNotConfigured*: la Google Drive API no está activada en el proyecto de Google Cloud.
- `Faltan las librerías para leer contenidos`: instala el extra `.[contenido]`.
- `No encuentro la clave de la API` o `La API ha rechazado la clave`: revisa `ANTHROPIC_API_KEY` en `.env`.

## 8. El Excel de salida

| Hoja | Contenido |
|---|---|
| **Resumen** | Cifras generales y, con `--validar`, el acierto global y por nivel de confianza |
| **Gastos** | Cada gasto con su estado, confianza, documento (con enlace), puntuación, motivo y 2º candidato |
| **Sin justificante** | Solo los gastos sin documento que **hay que revisar** |
| **Cofinanciación e indirectos** | Gastos sin documento que no se esperan en la carpeta del donante |
| **Documentos** | Cada archivo, cuántos gastos cubre y si es copia u otra versión de otro |
| **Documentos sin gasto** | Archivos que no corresponden a ninguna línea del listado |
| **Validación** | Con `--validar`: veredicto gasto a gasto |

Con la lectura del contenido activada se añaden:
- En *Gastos*, *Documentos* y *Documentos sin gasto*, las columnas **Leído por contenido** (`sí`, `ilegible`, `no` o `no leído: motivo`) y **Datos del contenido** (tipo, importe, fecha, proveedor y nº de factura leídos). En *Gastos* se refieren al documento asignado o, si no hay, al mejor candidato.
- En *Resumen*, cuántos documentos se han leído y en cuántos gastos ha cambiado el resultado.

**Cómo revisarlo:** filtra *Gastos* por **Confianza = baja**. Después mira *Sin justificante* y *Documentos sin gasto*.

## 9. Resultados

### Uganda – Adjumani (Xunta 2024), piloto
203 gastos y 96 documentos. Se mide con los 181 gastos que tienen nº de orden manual.

| Métrica | Resultado |
|---|---|
| Exactitud | **89 %** (161/181) |
| Acierto con confianza alta | 78/78 (100 %) |
| Acierto con confianza media | 54/54 (100 %) |
| Acierto con confianza baja | 29/41 (71 %): la herramienta avisa de que hay que revisarlos |

**Gastos sin documento:**
- **7 para revisar.** Son fallos de cruce: archivos con nombres muy genéricos, como "Combustible Septiembre".
- **8 de cofinanciación.**
- **9 costes indirectos.**

También aparecen **7 documentos que no corresponden a ningún gasto** del listado.

Los pesos se ajustaron con este proyecto, así que estas cifras son optimistas.

### ECHO Colombia, con los mismos pesos (sin ajustar)
12 PDF de muestra para ~3.000 gastos. Otra convención de nombres (`Equipos_50_51_52_53-206-15000.pdf`) y como referencia el código contable.

- Encuentra **16 de los 17** gastos que tienen documento en la muestra y **no asigna ninguno a un documento equivocado**.
- Hay 16 asignaciones que la validación cuenta como fallos. Son gastos que comparten comprobante de egreso con el documento. Se revisó a mano el comprobante 15935 y **el PDF contiene de verdad todos esos gastos**: son aciertos que la numeración manual no recogía.

### Lectura del contenido
**Sin medir todavía.** No se ha ejecutado con documentos reales (falta la aprobación del proveedor). Con la lectura desactivada, las cifras de arriba no cambian: se ha comprobado volviendo a ejecutar `--validar` en los dos proyectos.

## 10. Decisiones de diseño

- **Drive con el permiso mínimo.** Para listar documentos se pide solo acceso a metadatos. El permiso de lectura solo se pide si el propio listado está en Drive. Las librerías de Google son opcionales (`.[drive]`): sin ellas, el modo local sigue funcionando.
- **Empezar sin IA, solo con nombres de archivo.** Es barato, rápido y explicable. Con Drive bastará un permiso de solo metadatos, sin acceso al contenido. Es el paso previo a leer los PDF.
- **No usar la numeración manual para cruzar.** Es el trabajo que ya hizo una persona; se usa solo para validar. Así se mide lo que pasaría con un proyecto nuevo.
- **Configuración por proyecto en YAML.** Cada donante y cada socio nombran distinto: lo que cambia está en la configuración, no en el código.
- **Una referencia basta sola solo si es única.** En ECHO un código contable agrupa varios gastos (una nómina de 18 personas). Si varios documentos comparten la referencia, decide el concepto.
- **Marcar la ambigüedad en vez de elegir a ciegas.** Si dos documentos encajan casi igual, el gasto sale con confianza baja.
- **Separar "sin justificante" de "no se espera documento".** La cofinanciación y los costes indirectos no son incidencias.
- **El Excel explica cada decisión** con un motivo en lenguaje natural, porque es una herramienta de apoyo al revisor.
- **El contenido se lee solo donde el nombre no basta.** Leer cuesta dinero y envía datos fuera: se leen los candidatos de los gastos dudosos y los documentos sin gasto, con un tope de llamadas por ejecución y una caché por hash del archivo. Los gastos con confianza alta o media no se vuelven a decidir.
- **La lectura está desactivada por defecto** y es una dependencia opcional (`.[contenido]`): sin ella todo funciona igual que antes.
- **Salida estructurada en vez de una tool forzada.** `claude-sonnet-5-5` rechaza `tool_choice` forzado; con `output_config.format` (JSON schema) se obtiene el mismo JSON garantizado en una llamada.
- **Dato que no se lee, dato vacío.** El modelo no debe inventar: un dato vacío no suma; uno inventado asociaría el gasto al documento equivocado.
- **Un importe distinto no penaliza.** Un mismo documento puede cubrir varios gastos (comprobantes de egreso de ECHO), así que su total no tiene por qué ser el del gasto.
- **Si el nombre y el contenido no apuntan al mismo documento, no se elige a ciegas:** el gasto sale con confianza baja y el motivo lo explica.
- **La clave de la API vive en `.env`** y la recoge directamente el cliente oficial: el código no la lee, no la imprime y no la guarda.

## 11. Limitaciones conocidas

- Depende de que los nombres de archivo sean informativos. Con nombres genéricos ("Combustible Septiembre"), el cruce es débil.
- La lectura del contenido **no se ha probado con documentos reales**: solo con una API simulada. No se sabe aún cuánto mejora el cruce, cuánto cuesta ni cómo de bien lee los manuscritos.
- Los pesos de las señales del contenido son un punto de partida **sin calibrar**.
- La lectura solo funciona con documentos en una carpeta local, no con Drive (haría falta permiso de lectura y descargar los archivos).
- Solo se leen PDF e imágenes, y solo las primeras páginas: en un PDF con varios justificantes se extrae el principal.
- En proyectos con muchos gastos del mismo importe (nóminas mensuales), la coincidencia de importe puede dar falsos positivos.
- Sin `importe_local` en el YAML, el importe solo se compara si el documento está en euros.
- Un rechazo del modelo se trata como «no leído»; no se reintenta con otro modelo.
- Los pesos se ajustaron con Uganda y solo se han probado 2 proyectos.
- Las series mensuales con criterio de nombrado irregular (cotizaciones de diciembre en Uganda) quedan como ambiguas.
- La conexión con Google Drive está probada con una API simulada y con los nombres reales de Uganda, pero **aún no contra el Drive de Entreculturas**.

## 12. Hoja de ruta y preguntas abiertas

| Fase | Descripción | Estado |
|---|---|---|
| 0 | Conciliador gasto ↔ justificante por nombre de archivo | ✅ Hecho (Uganda + ECHO) |
| 1 | Leer el contenido de los documentos dudosos (LLM con visión) y usar importe, fecha, proveedor y nº de factura en el cruce | 🟡 Programado y probado con API simulada; **falta ejecutarlo con documentos reales**, medirlo y calibrar los pesos |
| 1b | Leer también documentos de Drive (descarga con permiso de lectura) y verificar los gastos ya asignados con confianza alta o media | Pendiente |
| 2 | Revisar justificantes con las reglas del donante (`Rules.xlsx`) y evaluar con las incidencias reales de Tigray | Pendiente |
| 3 | Proceso 2: presupuesto aprobado frente a ejecutado por partida y topes del donante (Rioja, AECID, Uganda) | Pendiente |
| 4 | Fuente de documentos en Google Drive (`drive.py` + `fuentes.py`, permiso de solo metadatos) | ✅ Hecho; falta probarlo con el Drive real |

**Preguntas abiertas para Entreculturas:**
- ¿Cómo está organizado Drive? ¿Quién nombra los archivos y con qué convención?
- ¿Podemos tener acceso de solo lectura a una carpeta de prueba? ¿Con nuestra cuenta (OAuth) o con una cuenta de servicio creada por ellos?
- ¿El listado de gastos está también en Drive (Excel u Hoja de cálculo de Google)?
- ¿Qué proveedor de IA está permitido para leer el contenido de los documentos (RGPD)? **Bloquea la fase 1:** el código usa la API de Anthropic y no se ejecutará con documentos reales hasta tener respuesta.
- ¿Qué significa el nº de orden "N/A" en el listado?
- ¿Los costes indirectos se justifican siempre con certificado?
- ¿Hay un proyecto en curso para probarlo en real?
- ¿Tienen el export del workflow de n8n de TUM?

## 13. Mantener este README

Este archivo es el **contexto compartido** del equipo y de las IAs que usemos. Reglas:

1. **Cualquier cambio de código, configuración o decisión actualiza este README en el mismo commit**: estructura, funcionamiento, YAML, resultados, decisiones y limitaciones. Lo que ya no sea cierto se corrige o se borra.
2. **Añade una línea al [Registro de cambios](#14-registro-de-cambios)** con la fecha y qué cambió.
3. **Si cambian las métricas**, vuelve a ejecutar `--validar` en los proyectos de `config/` y actualiza la sección 9.
4. **Si cambian las preguntas abiertas** (o las responde Entreculturas), actualiza la sección 12 y anota la respuesta.
5. **Nunca pongas datos personales** (nombres, importes de nóminas, cuentas): este archivo se sube a GitHub.
6. Si usas una IA, pídele que lea primero `README.md` y `AGENTS.md`, y que actualice el README al terminar.

## 14. Registro de cambios

| Fecha | Cambio |
|---|---|
| 14/11/2026 | Primera versión del repositorio: README, `.gitignore` (excluye `TUM_SociAI/` y `output/`) |
| 14/11/2026 | Conciliador v0.1: cruce por referencia, concepto y mes; Excel de salida; validación; piloto Uganda |
| 14/11/2026 | Generalizado para ECHO: códigos contables, nº de orden en medio del nombre, columnas extra de referencia; regla "una referencia basta sola solo si es única" |
| 14/11/2026 | Motivos en lenguaje natural; si el Excel está abierto se guarda una copia |
| 14/11/2026 | Clasificación de gastos sin documento: revisar / cofinanciación / coste indirecto |
| 14/11/2026 | README reescrito como fuente de verdad; añadidos `AGENTS.md` y `CLAUDE.md` |
| 14/11/2026 | **Google Drive**: documentos desde una carpeta de Drive (recursivo, unidades compartidas, atajos), listado opcional desde Drive, OAuth o cuenta de servicio, `--listar-documentos`, plantilla `config/ejemplo_drive.yaml`, tests con API simulada |
| 14/11/2026 | **Lectura del contenido** (opcional, desactivada por defecto): `lectura.py` (API de Claude con salida estructurada y caché por hash), señales de importe, fecha, proveedor y nº de factura en el cruce, aviso de conflicto nombre/contenido, sección `contenido:` del YAML, `--contenido`, columnas nuevas en el Excel, `importe_local` en el listado, extra `.[contenido]`, tests con API simulada. Sin ejecutar con documentos reales |
| 14/11/2026 | Limpieza: un único formato de importes (`texto.formato_importe`) y una única firma de documento (`Documento.firma`); `lectura.py` queda solo con la API y el resumen para el Excel pasa a `salida.py`; `requirements.txt` remite a `pyproject.toml`; `AGENTS.md` recoge las reglas de la clave y de la lectura del contenido. Sin cambios en el cruce ni en las métricas |
| 14/11/2026 | El Excel de salida lleva siempre la fecha y la hora en el nombre (`output/<yaml>_<AAAAMMDD>_<HHMMSS>.xlsx`): cada ejecución genera un archivo distinto. `--salida` sigue usando la ruta exacta |
| 14/11/2026 | Preparada la primera prueba real de la lectura del contenido: `config/prueba_contenido.yaml` (carpeta pequeña `TUM_SociAI/prueba_contenido/`, tope de 5 llamadas) y guía en la sección 7. Los errores de la API ahora muestran su mensaje en «Leído por contenido» |
