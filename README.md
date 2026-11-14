# Proyecto APS ROJO – Entreculturas

Herramienta de apoyo para **agilizar la justificación económica** de los proyectos de cooperación y ayuda humanitaria de la Fundación Entreculturas.

> **Este README es la fuente de verdad del proyecto.** Está pensado para que cualquier persona, o cualquier IA (Claude, ChatGPT, Gemini, Copilot…), entienda el proyecto completo leyendo solo este archivo. Hay que **actualizarlo en cada cambio** (ver [Mantener este README](#13-mantener-este-readme)).

**Estado (14/11/2026):** fase 0 terminada. Ya funciona un **conciliador gasto ↔ justificante** que no usa IA. Está probado con 2 proyectos reales de donantes distintos: Uganda (Xunta) y ECHO Colombia. La siguiente fase es leer el contenido de los PDF.

---

## Índice
1. [El reto](#1-el-reto)
2. [Datos disponibles](#2-datos-disponibles)
3. [Antecedente: MVP de TUM SociAI](#3-antecedente-mvp-de-tum-sociai)
4. [Estructura del repositorio](#4-estructura-del-repositorio)
5. [Cómo funciona el conciliador](#5-cómo-funciona-el-conciliador)
6. [Configuración por proyecto (YAML)](#6-configuración-por-proyecto-yaml)
7. [Instalación y uso](#7-instalación-y-uso)
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
│   └── echo_colombia.yaml     #   segundo caso (ECHO, Colombia)
├── src/conciliador/           # Código del conciliador
│   ├── __main__.py            #   CLI: lee config → cruza → valida → escribe Excel
│   ├── modelos.py             #   Estructuras: Documento, Gasto, Candidato, Resultado
│   ├── listado.py             #   Lee el listado de gastos (.xls/.xlsx) → lista de Gasto
│   ├── fuentes.py             #   Lista documentos (hoy: carpeta local; mañana: Drive)
│   ├── texto.py               #   Normalización: acentos, plurales, sinónimos, referencias, meses
│   ├── cruce.py               #   Motor de cruce: puntúa cada par gasto-documento y decide
│   ├── validacion.py          #   Métricas comparando con la numeración manual
│   └── salida.py              #   Genera el Excel para el revisor
├── tests/                     # pytest (sin datos reales)
├── output/                    # Excels generados (NO se versiona: contienen datos personales)
├── TUM_SociAI/                # Datos de Entreculturas (NO se versiona)
├── pyproject.toml / requirements.txt
└── Reto Entreculturas-1.pdf   # Enunciado oficial
```

Ignorados por git: `TUM_SociAI/`, `output/`, `CONTEXTO.md`, `.env`, `.venv/`.

## 5. Cómo funciona el conciliador

**Objetivo:** asociar cada línea del listado de gastos con el documento que la justifica, y señalar los gastos sin documento y los documentos que sobran. **Solo usa los nombres de los archivos**: no abre los PDF ni usa IA. Es barato, rápido y respeta la privacidad.

```
 config/<proyecto>.yaml
          │
          ▼
 listado.py ──► Gastos ─┐
                        ├─► cruce.py ──► Resultados ──► salida.py ──► output/<proyecto>.xlsx
 fuentes.py ──► Docs  ──┘   (texto.py)        │
                                              └──► validacion.py (opcional, --validar)
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

## 6. Configuración por proyecto (YAML)

Todo lo específico de un proyecto está en `config/<proyecto>.yaml`. **Un proyecto nuevo es un YAML nuevo, sin tocar código.** Las rutas son relativas a la raíz del repositorio y las columnas se indican por número (A = 0).

| Clave | Para qué sirve |
|---|---|
| `proyecto` | Nombre que aparece en el Excel |
| `documentos.tipo` / `ruta` / `excluir` | De dónde salen los documentos y qué archivos ignorar (normativa, el propio listado…) |
| `listado.archivo` / `hoja` | Excel con el listado de gastos |
| `listado.columnas` | Posición de: `partida`, `orden` (solo para validar), `fecha`, `factura`, `proveedor`, `descripcion`, `importe_eur`, `pago` |
| `listado.columnas_referencia` | Columnas extra con referencias (p. ej. el código contable en ECHO) |
| `listado.imputacion.donante` / `otros` | Columnas con el importe imputado al donante y a otros financiadores |
| `listado.partidas_indirectos` | Regex de las partidas de costes indirectos |
| `listado.obligatorias` | Campos que debe tener una fila para contar como gasto |
| `referencias.patron` | Regex con los grupos `prefijo` y `numero` para reconocer el nº de factura o el código contable |
| `referencias.numeros_largos` | Tratar números sueltos de 5 o más cifras como referencia |
| `prefijo_orden` | Regex del nº de orden manual en el nombre del archivo: grupos `desde`/`hasta`/`sufijo` o `lista`. Se oculta al cruzar y se usa al validar |
| `cruce.*` | Pesos y umbrales (ver sección 5). `conceptos_mes_vencido`: conceptos que se pagan el mes siguiente |
| `sinonimos` | Palabras equivalentes del dominio (`salario: [nomina, payroll]`) |

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
| `--salida ruta.xlsx` | Cambia el Excel de salida (por defecto `output/<nombre_del_yaml>.xlsx`) |
| `--raiz carpeta` | Raíz para las rutas relativas del YAML (por defecto, la carpeta actual) |

Si el Excel de salida está abierto, se guarda una copia con la fecha y hora en el nombre.

**Problemas frecuentes:**
- `ModuleNotFoundError: conciliador`: el entorno no está activado.
- `No existe la carpeta de documentos`: falta `TUM_SociAI/` o la ruta del YAML no coincide.
- Casi todo sale sin justificante: revisa los números de columna en `listado.columnas`.

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

## 10. Decisiones de diseño

- **Empezar sin IA, solo con nombres de archivo.** Es barato, rápido y explicable. Con Drive bastará un permiso de solo metadatos, sin acceso al contenido. Es el paso previo a leer los PDF.
- **No usar la numeración manual para cruzar.** Es el trabajo que ya hizo una persona; se usa solo para validar. Así se mide lo que pasaría con un proyecto nuevo.
- **Configuración por proyecto en YAML.** Cada donante y cada socio nombran distinto: lo que cambia está en la configuración, no en el código.
- **Una referencia basta sola solo si es única.** En ECHO un código contable agrupa varios gastos (una nómina de 18 personas). Si varios documentos comparten la referencia, decide el concepto.
- **Marcar la ambigüedad en vez de elegir a ciegas.** Si dos documentos encajan casi igual, el gasto sale con confianza baja.
- **Separar "sin justificante" de "no se espera documento".** La cofinanciación y los costes indirectos no son incidencias.
- **El Excel explica cada decisión** con un motivo en lenguaje natural, porque es una herramienta de apoyo al revisor.

## 11. Limitaciones conocidas

- Depende de que los nombres de archivo sean informativos. Con nombres genéricos ("Combustible Septiembre"), el cruce es débil.
- No comprueba importes ni fechas dentro del PDF: eso es la fase 1.
- Los pesos se ajustaron con Uganda y solo se han probado 2 proyectos.
- Las series mensuales con criterio de nombrado irregular (cotizaciones de diciembre en Uganda) quedan como ambiguas.
- Solo lee carpetas locales. Google Drive está pendiente.

## 12. Hoja de ruta y preguntas abiertas

| Fase | Descripción | Estado |
|---|---|---|
| 0 | Conciliador gasto ↔ justificante por nombre de archivo | ✅ Hecho (Uganda + ECHO) |
| 1 | Leer la primera página de cada PDF emparejado (LLM con visión) y verificar importe, fecha y proveedor | Pendiente |
| 2 | Revisar justificantes con las reglas del donante (`Rules.xlsx`) y evaluar con las incidencias reales de Tigray | Pendiente |
| 3 | Proceso 2: presupuesto aprobado frente a ejecutado por partida y topes del donante (Rioja, AECID, Uganda) | Pendiente |
| 4 | Fuente de documentos en Google Drive (`fuentes.py`, permiso de solo metadatos) | Pendiente |

**Preguntas abiertas para Entreculturas:**
- ¿Cómo está organizado Drive? ¿Quién nombra los archivos y con qué convención?
- ¿Podemos tener acceso de solo lectura a una carpeta de prueba?
- ¿Qué proveedor de IA está permitido para leer el contenido de los documentos (RGPD)?
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
