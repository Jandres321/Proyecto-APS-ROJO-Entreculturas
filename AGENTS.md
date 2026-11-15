# Instrucciones para asistentes de IA

Este archivo lo leen automáticamente Codex, Cursor, GitHub Copilot, Gemini CLI y otras herramientas. Claude Code lo lee a través de `CLAUDE.md`.

## Antes de nada
El repositorio tiene dos piezas (ver `CLAUDE.md`, «Repositorio integrado»):

- **Revisor de justificación** (`backend/`): la aplicación. Su documentación es `README.md` y `CLAUDE.md`. Código en inglés, textos al usuario en español.
- **Conciliador** (`src/conciliador/`, `config/`, `tests/`): el programa de línea de comandos, **legado**: se conserva como referencia y ya no se mantiene (su cruce está portado a `backend/app/pipeline/conciliador.py`, que es la versión que evoluciona). Su documentación es `docs/conciliador.md`. Código en español.

Para trabajar en el **conciliador**:
1. **Lee `docs/conciliador.md` entero.** Es su fuente de verdad: el reto, los datos, la estructura, cómo funciona el código, la configuración, los resultados, las decisiones y la hoja de ruta.
2. El código está en `src/conciliador/` y cada proyecto se configura con un YAML en `config/`.
3. Cada módulo tiene una sola responsabilidad (ver `docs/conciliador.md`, sección 4). Lo opcional (Drive, lectura del contenido) se importa solo al usarse y es un extra de `pyproject.toml`.
4. `backend/app/pipeline/conciliador.py` es un port de `src/conciliador/cruce.py`: un arreglo del cruce por nombre se lleva a los dos.

## Reglas (valen para las dos piezas)
- **Datos confidenciales:**
  - `TUM_SociAI/` y `output/` contienen datos personales y financieros (nóminas, contratos, extractos). No se versionan.
  - **Nunca** copies su contenido (nombres, importes, cuentas) a archivos que se suban: README, código, tests, commits.
  - Los tests usan datos inventados.
- **Credenciales de Google:** van en `.secrets/` (ignorado por git). Nunca las leas, las muestres ni las copies a otro sitio. Para Drive se usa el permiso mínimo: metadatos; lectura solo si el listado está en Drive.
- **Clave de la API de Anthropic:** va en `.env` (ignorado por git). Nunca la leas, la muestres ni la copies; el código tampoco la toca (la recoge el cliente oficial).
- **La lectura del contenido (`--contenido`) envía documentos a la API de Anthropic.** No la ejecutes con documentos reales; los tests usan una API simulada.
- **No envíes documentos de `TUM_SociAI/` a servicios externos** (APIs de IA, webs) sin la aprobación explícita del equipo. Entreculturas aún no ha confirmado qué proveedor se puede usar.
- **Lo específico de un proyecto va en su YAML**, no en el código.
- **El nº de orden manual nunca se usa para cruzar**, solo para validar (ver `docs/conciliador.md`, sección 10).
- Idioma: en el conciliador (`src/`, `config/`, `tests/`), el código, los comentarios, los mensajes y la documentación van en **español**. En `backend/`, código y comentarios en inglés y textos al usuario en español.

## Al terminar cualquier cambio en el conciliador
1. Ejecuta `python -m pytest` en la raíz y que pase.
2. Si el cambio afecta al cruce, ejecuta `python -m conciliador config/<proyecto>.yaml --validar` en todos los YAML de `config/` y anota las métricas nuevas en `docs/conciliador.md` (sección 9).
3. **Actualiza `docs/conciliador.md`** en las secciones afectadas y añade una línea al *Registro de cambios* (sección 14). Lo que ya no sea cierto se corrige.
4. Si ha surgido una decisión de diseño o una limitación nueva, añádela a las secciones 10 u 11.

## Al terminar cualquier cambio en el backend
1. Ejecuta sus tests (`make test`, o `pytest` dentro de `backend/` con Python 3.12) y que pasen.
2. Actualiza `README.md` en lo que cambie.
