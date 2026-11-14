# Proyecto APS ROJO – Entreculturas

Herramienta de apoyo con IA para **agilizar la justificación económica** de proyectos de cooperación al desarrollo y ayuda humanitaria de la Fundación Entreculturas.

## El reto

Entreculturas justifica ante donantes públicos y privados (AECID, Xunta de Galicia, Gobierno de La Rioja, ECHO…) el gasto de sus proyectos. Hoy el equipo técnico revisa a mano cada justificante frente al listado de gastos y la normativa del donante. Es un proceso lento, propenso a errores y que puede alargar el cierre de un informe entre 2 y 4 meses.

El objetivo es que la IA **detecte incidencias y las explique**, con validación humana final (*human-in-the-loop*). No se exige un 100 % de precisión.

### Proceso 1: revisión de documentación justificativa
Comprobar que cada justificante cumple la normativa del donante. Incidencias más frecuentes:
- Recibos o proformas presentados como factura.
- Pagos en efectivo sin sello de "pagado" ni firma del proveedor.
- Documentos ilegibles o mal escaneados.
- Falta de justificante de pago o de soportes complementarios (nóminas, contratos, listas de asistencia…).
- Falta del sello del donante (nº de expediente, convocatoria, % de imputación).
- Fecha, importe o concepto de la factura que no coinciden con el listado de gastos.

### Proceso 2: revisión de la imputación presupuestaria
Comprobar que cada gasto está en la partida correcta y que los totales por partida del informe económico son coherentes con el presupuesto aprobado. También que se respetan los límites del donante; por ejemplo, en Xunta: personal ≤ 70 %, costes indirectos ≤ 5 %, auditoría ≤ 2.500 €.

## Punto de partida: MVP de TUM SociAI

Un equipo de estudiantes de la TUM desarrolló un MVP de *Invoice Check Automation* con **n8n + Gemini + Google Sheets**:

1. La factura se sube a Google Drive.
2. Gemini extrae la información general (nº, fecha, proveedor, cliente, importe, moneda, forma de pago, idioma, categoría de gasto).
3. Se evalúa contra un conjunto de reglas (`Rules.xlsx`, basadas en la normativa de Xunta de Galicia) con un veredicto *correcto / dudoso / incorrecto* por regla.
4. Los resultados se guardan en Google Sheets y se notifican por email.

Limitaciones conocidas: validado solo con 10 facturas, reglas exclusivas de un donante, sin cobertura del proceso 2 y sin el workflow de n8n disponible en este repositorio.

## Plan de trabajo

| Fase | Descripción | Estado |
|---|---|---|
| 1 | Pipeline en Python del proceso 1: PDF → LLM con visión → JSON con campos y tipo de documento → evaluación por reglas → Excel de incidencias | Pendiente |
| 2 | Evaluación con un caso real etiquetado (incidencias del revisor externo y respuestas de Entreculturas, proyecto Xunta – Tigray) | Pendiente |
| 3 | Proceso 2 para un donante: comparación presupuesto aprobado vs. informe económico por partida y comprobación de topes | Pendiente |
| 4 | Reglas configurables por donante/proyecto | Futuro |

## Estructura del repositorio

```
.
├── README.md
├── .gitignore
└── Reto Entreculturas-1.pdf   # Descripción oficial del reto
```

## Datos y confidencialidad

La documentación justificativa, los presupuestos, los informes económicos y el material del prototipo de TUM (carpeta `TUM_SociAI/`) contienen **datos personales y financieros** (nóminas, contratos, extractos bancarios). Por eso **no se versionan** y están excluidos en `.gitignore`. Cada miembro del equipo debe tenerlos en local.

Del mismo modo, las claves de API van en un fichero `.env` que nunca se sube al repositorio. Antes de enviar documentos a un modelo en la nube, hay que confirmar con Entreculturas qué proveedor y qué condiciones de tratamiento de datos son aceptables.
