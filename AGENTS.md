# Instrucciones para asistentes de IA

Este archivo lo leen automáticamente Codex, Cursor, GitHub Copilot, Gemini CLI y otras herramientas. Claude Code lo lee a través de `CLAUDE.md`.

## Antes de nada
1. **Lee `README.md` entero.** Es la fuente de verdad: el reto, los datos, la estructura, cómo funciona el código, la configuración, los resultados, las decisiones y la hoja de ruta.
2. El código está en `src/conciliador/` y cada proyecto se configura con un YAML en `config/`.

## Reglas
- **Datos confidenciales:**
  - `TUM_SociAI/` y `output/` contienen datos personales y financieros (nóminas, contratos, extractos). No se versionan.
  - **Nunca** copies su contenido (nombres, importes, cuentas) a archivos que se suban: README, código, tests, commits.
  - Los tests usan datos inventados.
- **No envíes documentos de `TUM_SociAI/` a servicios externos** (APIs de IA, webs) sin la aprobación explícita del equipo. Entreculturas aún no ha confirmado qué proveedor se puede usar.
- **Lo específico de un proyecto va en su YAML**, no en el código.
- **El nº de orden manual nunca se usa para cruzar**, solo para validar (ver README, sección 10).
- Idioma: el código, los comentarios, los mensajes y la documentación van en **español**.

## Al terminar cualquier cambio
1. Ejecuta `python -m pytest` y que pase.
2. Si el cambio afecta al cruce, ejecuta `python -m conciliador config/<proyecto>.yaml --validar` en todos los YAML de `config/` y anota las métricas nuevas en el README (sección 9).
3. **Actualiza `README.md`** en las secciones afectadas y añade una línea al *Registro de cambios* (sección 14). Lo que ya no sea cierto se corrige.
4. Si ha surgido una decisión de diseño o una limitación nueva, añádela a las secciones 10 u 11.
