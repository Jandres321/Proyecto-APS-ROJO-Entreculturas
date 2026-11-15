# Integración pendiente entre ramas

Dos ramas trabajan en paralelo desde `integracion-revisor`:

- `refactor/proyectos`: proyectos en base de datos, API, fuentes (subida y Drive), ejecuciones persistidas,
  servicio de incidencias, interfaz y limpieza (Streamlit y frontend React eliminados).
- `refactor/lectura-reglas`: lectura selectiva con IA, fecha fuera del periodo, menos falsos avisos, división de
  `pipeline/checks.py` y evaluación con Tigray.

Al fusionarlas:

1. **Lectura con IA.** `pipeline/run.py` llama hoy a
   `read_project_documents(..., use_llm=project.llm_mode is not LlmMode.OFF)`. Cuando entre
   `LlmReadOptions`, sustituirlo por
   `llm=LlmReadOptions(mode=project.llm_mode.value, max_pages=project.llm_max_pages, ...)` y quitar `use_llm`.
2. **Periodo del proyecto.** `project.start_date` / `project.end_date` ya existen (migración 0006); la regla
   `DATE_OUT_OF_PERIOD` puede leerlos directamente en lugar de con `getattr`.
3. **Servicio de incidencias.** `pipeline/match.py` mantiene el alias `_sync_findings` solo porque
   `pipeline/checks.py` lo importa. Cuando `checks/` sea un paquete, importar
   `from app.services.findings import sync_findings` y borrar el alias.
4. **Formato.** En `refactor/proyectos` no se ha pasado `ruff format` por `pipeline/checks.py` ni por
   `app/lectura/` para no provocar conflictos.
