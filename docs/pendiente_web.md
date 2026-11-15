# Interfaz de revisión: lo que falta de la API

Rama `feat/interfaz-revision`. La página (`backend/app/web/`) ya está hecha contra el contrato de abajo.
Con `?mock=1` funciona entera con datos inventados (`web/js/mock.js`). Con la API real, depende de que
llegue a `integracion-revisor` lo que se está haciendo en `fix/criterio-backend`.

## 1. Contrato que usa la página

`GET /api/projects/{key}/review`: cada elemento de `expenses[]` trae, además de lo de hoy:

```json
{
  "verdict": {"code": "ok | review | na", "label": "Bien | Necesita revisión | No aplica"},
  "reasons": [{"code": "NOT_READ_AMOUNT", "label": "No leído: importe", "blocking": false}],
  "decision": {"status": "pending | correct | real_issue", "note": null, "decided_at": null},
  "compare": [{"field": "importe", "label": "Importe", "listed": "1.250.000 PYG", "read": "1.205.000 PYG", "match": false}]
}
```

En la raíz, `counts: {"pending": 42, "reviewed": 118, "ok": 900, "na": 261}`.

- `PATCH /api/expenses/{id}/decision` con `{status, note}` devuelve el gasto actualizado, con la misma forma.
  Si falta la nota en un *Correcto* con motivo bloqueante, responde 422 con `detail` en español, y la página
  lo muestra tal cual.
- `GET /api/documents/{id}/page/{n}.png?rotation=&dpi=`: `dpi` va de 110 por defecto a 300. La página usa los
  niveles 80, 110, 150, 200, 250 y 300.

La página usa dos campos más de `decision` si vienen: `label` (texto de la decisión) y `stale` (se decidió,
pero lo leído o encontrado ha cambiado; se muestra «vuelve a decidir»). `fingerprint` no se usa.

## 2. Estado frente a la API

| Pieza | En `integracion-revisor` | En `fix/criterio-backend` (sin fusionar) |
|---|---|---|
| `verdict` con `ok / review / na`, `reasons`, `compare`, `decision` | No: hay `verdict` con los códigos antiguos | Sí (`app/verdict.py`) |
| `counts` en la raíz | No | Sí |
| `PATCH /api/expenses/{id}/decision` | No | Sí |
| `dpi` en la página del PDF | No (siempre 110) | Sí |

**Hasta la fusión**, la página contra `integracion-revisor` carga, pero:

- todos los gastos salen en *Por revisar* (salvo los de veredicto `ok`/`na`), sin motivos ni tabla
  «Informe frente a PDF»: la página pone listas vacías donde faltan campos, nunca un veredicto propio;
- *Correcto* e *Incidencia real* dan error 404 al guardar: no se avanza y se ve el error;
- el zoom no cambia el tamaño de la página del PDF (el servidor ignora `dpi`).

## 3. Lo que solo funciona con los datos de ejemplo (`?mock=1`)

- **Decisiones por gasto** y paso al siguiente pendiente: en memoria (al recargar se pierden). `&fallo=1`
  hace fallar cada decisión para ver el error.
- **Motivos, «Informe frente a PDF», contadores de las pestañas y decisión caducada**: los genera `mock.js`.
- **Zoom** del visor: la página de ejemplo es un dibujo (SVG) que cambia de tamaño con `dpi`.
- **Nuevo proyecto**: no sube nada. Google Drive aparece como no configurado.
- **Analizar**: termina al instante, sin barra de progreso real.
- **No disponible** con datos de ejemplo (se ocultan): *Descargar Excel* y *Abrir original* del PDF, porque
  son enlaces directos al servidor.

## 4. Peticiones a quien lleva la API

1. **Latencia de cada decisión.** `set_expense_decision` llama dos veces a `project_review` (todo el
   proyecto) por cada decisión. Con los 1.321 gastos de AECID puede tardar, y la página espera esa respuesta
   para pasar al siguiente. Bastaría con calcular la vista de ese gasto solo.
2. **`counts` en la respuesta del PATCH** (opcional). Ahora la página los recalcula con los gastos que ya
   tiene, con el mismo reparto que el servidor (`ok`, `na`; los `review` según `decision.status`).
3. **Gastos con veredicto *Bien* o *No aplica*.** La página deja decidir sobre ellos (por si el técnico ve un
   problema), pero `counts` los sigue contando como *Bien* / *No aplica*. Si se quiere que una *Incidencia
   real* sobre un gasto *Bien* pase a *Revisados*, tiene que decidirlo el servidor.
