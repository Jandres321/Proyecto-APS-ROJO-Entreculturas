# Integración pendiente con el modelo de proyectos, la API y la web

Rama `refactor/lectura-reglas`. Esta rama **no toca** `backend/app/models.py`, `backend/app/api/`,
`backend/app/pipeline/run.py`, `backend/app/projects.py`, `backend/app/drive.py`, `backend/app/web/`
ni `alembic/`. Lo que necesita de esas zonas queda aquí como parámetro opcional ya implementado en
el lado de lectura/comprobaciones, y como tarea para quien las mantiene.

## 1. Lectura selectiva con Claude

Implementado en `app/lectura/` (`LlmReadOptions`, `process_pdfs(..., llm=)`,
`read_project_documents(..., llm=, on_llm_summary=)`). Falta conectarlo:

- **`pipeline/run.py`** llama a `read_project_documents(...)`. Para activar la lectura selectiva
  debe pasar `llm=LlmReadOptions(mode, max_pages, min_ocr_confidence)` y, si quiere mostrar el
  gasto, `on_llm_summary=callback`. Sin esos parámetros no cambia nada (modo `off`).
- **Resumen** (`LlmReadSummary`): `pages_sent`, `pages_candidate`, `pages_cached`,
  `pages_over_limit`, `pages_failed`, `input_tokens`, `output_tokens`, `estimated_cost_usd`,
  `model`, `errors`. Conviene guardarlo en el resultado de la ejecución (p. ej. una columna JSON en
  la tabla de ejecuciones, migración de Alembic) y enseñarlo en la web.
- **API/web**: exponer `mode` (`off|dudosas|todas`), `max_pages` y `min_ocr_confidence` al lanzar
  una ejecución. Si falta `ALLOW_EXTERNAL_LLM=true` se lanza `ExternalLlmNotAllowed` (un
  `PermissionError`): la API debería devolverlo como 403 con ese mensaje.
- Los precios para la estimación son `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK`
  (por defecto los de Claude Haiku 5.5: 0,10 y 0,50 USD por millón de tokens).
- `segmento.extra["campo_fuente"]` indica por campo si el valor vino del `ocr` o del `llm`; la web
  puede mostrarlo junto a cada campo.

## 2. Periodo del proyecto (`DATE_OUT_OF_PERIOD`)

La regla ya está en `rules/xunta.yaml` y `rules/aecid.yaml` y en `pipeline/checks/`
(`project_period`). Lee el periodo con `getattr(project, "start_date", None)` /
`getattr(project, "end_date", None)` (acepta `date` o `datetime`; basta uno de los dos):

- **`models.py` + Alembic**: añadir `Project.start_date` y `Project.end_date` (`Date`, nulos).
  Hasta entonces `build_context` devuelve `period=None` y la regla no se aplica.
- **API/web**: permitir informar el periodo al crear/editar el proyecto (o leerlo del informe del
  donante).
- Referencia normativa de Xunta: el YAML cita `Rules.xlsx` y la Orden AH 2024 de forma genérica;
  falta confirmar el artículo exacto de la convocatoria (no hay copia en el repo).
