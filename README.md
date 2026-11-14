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
| 0 | **Conciliador gasto ↔ justificante** por nombre de archivo, sin IA (piloto: Uganda) | ✅ Prueba de concepto |
| 1 | Verificar importe y fecha leyendo la primera página del PDF (LLM con visión) | Pendiente |
| 2 | Revisión de justificantes contra reglas del donante (proceso 1) y evaluación con las incidencias reales de Tigray | Pendiente |
| 3 | Proceso 2: presupuesto aprobado vs. informe económico por partida y comprobación de topes | Pendiente |
| 4 | Fuente de documentos en Google Drive (permiso de solo metadatos) | Pendiente |

## Conciliador gasto ↔ justificante

Asocia cada línea del listado de gastos con el documento que la justifica, usando **solo el nombre de los archivos**: no abre los PDF ni usa IA. Genera un Excel para revisión humana con:

- **Gastos**: cada gasto con su documento, puntuación, nivel de confianza (alta / media / baja) y el motivo del emparejamiento.
- **Sin justificante**: gastos para los que no se ha encontrado documento.
- **Documentos**: cada archivo, cuántos gastos cubre y si es copia u otra versión de otro.
- **Documentos sin gasto**: archivos que no corresponden a ninguna línea del listado.
- **Validación** (opcional): comparación con la numeración manual.

### Cómo cruza

1. **Oculta el nº de orden manual** del nombre del archivo ("1. ", "10.A ", "33-39 "). Esa numeración es el trabajo ya hecho a mano, así que el algoritmo no la ve; solo se usa para validar.
2. **Referencia de factura** (EB UGA04 602, FP UGA04 108…) presente en el listado y en el nombre del archivo.
3. **Concepto**: cuánto del nombre del archivo está en el gasto y cuánto de la descripción del gasto explica el archivo. Se comparan sin acentos, en singular y con sinónimos del dominio (nómina = salario…).
4. **Mes**: el que nombra el archivo frente al de la descripción o la fecha del gasto. Las cotizaciones se nombran por el mes que cubren, que es el anterior a la fecha de pago.
5. Si el segundo mejor candidato está muy cerca, el gasto se marca como **ambiguo** (confianza baja) para revisarlo.

Todo lo específico de cada proyecto (columnas del listado, formato de las referencias, pesos y umbrales, sinónimos) está en un YAML en `config/`. Un proyecto nuevo es un YAML nuevo.

### Uso

```bash
pip install -e ".[dev]"            # o: pip install -r requirements.txt
python -m conciliador config/uganda_adjumani.yaml --validar
# -> output/uganda_adjumani.xlsx
python -m pytest                   # tests
```

Sin instalar el paquete: `PYTHONPATH=src python -m conciliador ...`

### Resultados del piloto (Uganda – Adjumani, Xunta 2024)

203 gastos y 96 documentos. Se valida con los 181 gastos que tienen nº de orden manual.

| Métrica | Resultado |
|---|---|
| Exactitud | 89,5 % (162/181) |
| Precisión de lo asignado | 93,1 % |
| Acierto con confianza **alta** | 78/78 (100 %) |
| Acierto con confianza **media** | 54/54 (100 %) |
| Acierto con confianza **baja** | 30/42 (71 %): la herramienta avisa de que hay que revisarlos |

Los errores que quedan son ambigüedades reales del propio nombrado. Por ejemplo, las cotizaciones de diciembre están nombradas por el mes de pago y no por el mes que cubren, al contrario que en el resto del año. También aparecen hallazgos útiles: documentos en la carpeta que no están en el listado y gastos sin justificante.

> Los pesos se ajustaron sobre este mismo proyecto, así que el resultado es optimista. El siguiente paso es probarlo en otro proyecto sin retocar el código, solo con su YAML.

## Estructura del repositorio

```
.
├── config/
│   └── uganda_adjumani.yaml   # Configuración del proyecto piloto
├── src/conciliador/
│   ├── __main__.py            # CLI
│   ├── listado.py             # Lectura del listado de gastos (.xls/.xlsx)
│   ├── fuentes.py             # Origen de documentos (carpeta local; Drive más adelante)
│   ├── texto.py               # Normalización, referencias, meses, sinónimos
│   ├── cruce.py               # Motor de cruce y puntuación
│   ├── validacion.py          # Métricas contra la numeración manual
│   └── salida.py              # Excel de resultados
├── tests/
├── output/                    # Resultados generados (no se versiona)
├── pyproject.toml / requirements.txt
└── Reto Entreculturas-1.pdf   # Descripción oficial del reto
```

## Datos y confidencialidad

La documentación justificativa, los presupuestos, los informes económicos y el material del prototipo de TUM (carpeta `TUM_SociAI/`) contienen **datos personales y financieros** (nóminas, contratos, extractos bancarios). Por eso **no se versionan** y están excluidos en `.gitignore`. Cada miembro del equipo debe tenerlos en local.

Del mismo modo, las claves de API van en un fichero `.env` que nunca se sube al repositorio. Antes de enviar documentos a un modelo en la nube, hay que confirmar con Entreculturas qué proveedor y qué condiciones de tratamiento de datos son aceptables.
