# Pendiente tras la integración

`refactor/proyectos` (proyectos en BD, API, subida y Drive, ejecuciones persistidas, interfaz) y
`refactor/lectura-reglas` (lectura selectiva con IA, `DATE_OUT_OF_PERIOD`, menos falsos avisos,
paquete `checks/`, evaluación con Tigray) están fusionadas en `integracion-revisor`.

## Hecho al fusionar

- `pipeline/run.py` pasa `LlmReadOptions(mode=project.llm_mode, max_pages=project.llm_max_pages)`
  a la lectura y guarda el `LlmReadSummary` en el resumen de la ejecución; el mensaje final del
  análisis incluye páginas enviadas y coste estimado.
- Si el proyecto pide lectura con IA y `ALLOW_EXTERNAL_LLM` no está activo, el análisis termina con
  un error explicado (no falla en silencio).
- `DATE_OUT_OF_PERIOD` lee `project.start_date` / `project.end_date` (migración 0006).
- `checks/` importa `sync_findings` de `app.services.findings`; el alias temporal ya no existe.

## Pendiente

1. **Tigray no se puede medir todavía.** La carpeta `22_ET_217 Crisis Tigray Addis - Xunta de
   Galicia/` solo trae `Resumen incidencias_ Respuestas EC.xlsx` (el Excel del revisor): faltan el
   informe económico y los PDF. Con ellos: crear el proyecto (subida o Drive), analizarlo y ejecutar
   `python eval/evaluar_tigray.py --proyecto ID`. El cruce con la verdad es por nº de orden
   (`Expense.order_no`, numérico 1-128 en el Excel).
2. **Revisar a mano** las 34 incidencias `indeterminado` y las 56 `SIN_REGLA` de Tigray para decidir
   si merece la pena una regla nueva (reparto entre trabajadores, descripción de factura distinta).
3. **Referencia normativa de Xunta** en `DATE_OUT_OF_PERIOD`: cita `Rules.xlsx` y la Orden AH 2024
   de forma genérica; falta el artículo exacto de la convocatoria.
4. **Gasto de IA en la web:** el resumen ya llega al mensaje del análisis; falta guardarlo en
   `analysis_job` (columna JSON) y enseñarlo con detalle. `segmento.extra["campo_fuente"]` (ocr/llm)
   se podría mostrar junto a cada campo.
5. **Formato:** pasar `ruff format` por `pipeline/checks/` y `app/lectura/` en un commit aparte.
