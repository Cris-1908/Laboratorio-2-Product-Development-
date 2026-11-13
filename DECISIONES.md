# Decisiones técnicas y de producto

Este documento explica por qué el producto está armado como está. Para levantarlo está el `README.md`.

## 1. Cómo está armado

```
 tiendas ──► API (FastAPI) ──► PostgreSQL del producto  ◄── DAG de Airflow (reentrenamiento)
                │   ▲                                            │
                │   └──── modelo vigente (alias en el registry) ─┤
                └────────────► MLflow (experimentos + Model Registry)
```

- **API**: valida y limpia lo que llega, predice, y guarda todo. No entrena nada.
- **PostgreSQL del producto**: solicitudes, correcciones, predicciones, observaciones y el histórico publicado. Es una instancia distinta de la de Airflow. MLflow guarda sus metadatos en otra base dentro del mismo servidor.
- **MLflow**: cada reentrenamiento queda como un experimento y una versión del modelo. El modelo vigente es el que tiene el alias `produccion`.
- **Airflow**: un solo DAG (`reentrenar_modelo`) que la API dispara por `POST /admin/reentrenar`.
- **Contenedor `init`**: corre una vez al levantar. Crea las tablas, carga el histórico y, si no hay modelo vigente, entrena el primero. Si ya hay datos y modelo, no hace nada, por eso reiniciar el producto no repite trabajo.

La API consulta cada 15 segundos (configurable) si el alias cambió de versión, y de paso revisa MLflow, Airflow y la base. Así se cumple que un modelo promovido se use sin reiniciar, y `/health` responde de inmediato porque solo lee lo último que se vio.

## 2. Política ante datos defectuosos

La regla general es una sola, y de ahí salen todos los casos, incluidos los que no están en el catálogo:

- **Reparar** cuando solo cambió la forma y hay una única lectura posible (`"T05"` es la tienda 5, `"17/11/2025"` es el 17 de noviembre).
- **Advertir** cuando para responder hay que suponer algo. Se responde igual, pero la respuesta dice qué se supuso. El estado queda en `advertencia`.
- **Rechazar** (422) cuando no hay una forma honesta de saber de qué tienda, de qué día o de qué producto se habla.

Para decidir entre reparar y advertir me hago una pregunta: si el que mandó el dato leyera mi corrección, ¿diría "sí, eso quise decir" sin dudar? Si sí, es reparar. Si habría que adivinar, es advertir.

### Solicitudes de pronóstico

| Problema | Qué hace | Por qué |
|---|---|---|
| `tienda_id` como texto, con prefijo o con decimal (`"T05"`, `"5"`, `5.0`) | Repara | Una sola lectura posible. |
| `tienda_id` que no existe, ilegible o ausente | Rechaza | Sin tienda no hay pronóstico, y adivinar una sería inventar. |
| Fecha con hora, o en otro formato (`2025-11-17T00:00:00`, `17/11/2025`, `20251117`, `17 de noviembre de 2025`) | Repara | La fecha es inequívoca. |
| Fecha `a/b/AAAA` con ambos números ≤ 12 (`03/04/2025`) | Advierte | Puede ser día/mes o mes/día. Se asume día/mes (convención local) y se avisa. Si uno de los dos es > 12 no hay duda y se repara. |
| Fecha imposible (`2025-02-30`), ilegible, o `17/11/25` con año de dos cifras | Rechaza | No hay lectura segura. |
| Fecha anterior al inicio de los datos, o anterior a la apertura de la tienda | Rechaza | No hay base para pronosticar. La tienda 10 abrió el 2025-03-15. |
| Fecha posterior al último día de `feriados.csv` | Advierte | Se puede pronosticar, pero sin saber de feriados ni fechas de pago. Se avisa que se asumió un día normal. |
| Nombre de campo distinto (`tienda`, `tiendaId`, `store_id`) | Repara | Se reconoce por nombre normalizado y por una lista corta de alias. |
| Campos de más | Los ignora | No afectan el pronóstico, no es un problema de los datos. |
| Familia con otra forma (`"LÁCTEOS"`, `"lacteos "`, `"Frutas y Verduras"`) | Repara | Se compara contra el id y el nombre del catálogo, sin acentos ni mayúsculas. |
| Familia con un error de dedo (`"lacteoz"`) | Advierte | Es una suposición, aunque razonable (parecido ≥ 0.84 con una sola candidata). |
| Familia que no existe | La ignora y advierte | No hay qué predecir. Si ninguna familia se reconoce, rechaza. |
| `en_promocion` como texto o decimal entero (`"3"`, `2.0`) | Repara | Una sola lectura. |
| `en_promocion` negativo, nulo, ilegible o absurdo (> 500) | Asume 0 y advierte | Hay que suponer algo y 0 es el supuesto neutro. |
| `en_promocion` decimal no entero (`2.6`) | Redondea y advierte | Redondear es una suposición. |
| Faltan familias (10 de 12) | Predice las 12 con `en_promocion=0` y advierte | El piso necesita las 12 familias. El supuesto de promoción 0 es lo único que se asume y queda explícito. |
| Familia repetida con el mismo valor | Repara (deja una) | No hay conflicto. |
| Familia repetida con valores distintos | Usa la última y advierte | No se puede saber cuál es la correcta. La última se toma como corrección. |
| Lista de familias ausente o vacía | Predice las 12 con promoción 0 y advierte | Es el caso extremo de "faltan familias". |
| Lista de familias que no es lista ni objeto | Rechaza | Estructura irreconocible. |
| Cuerpo que no es un objeto JSON | Rechaza | |

Además, si las últimas ventas registradas de la tienda tienen más de 3 días de atraso respecto a la fecha pedida, o no hay ninguna, se responde pero con advertencia: el pronóstico se apoya en información vieja.

### Observaciones de ventas

| Problema | Qué hace | Por qué |
|---|---|---|
| Mismos problemas de tienda, fecha, nombres y familias | Igual que arriba | Misma lógica. |
| Número como texto con separadores (`"1,234"`, `"812,0"`, `"1.234,5"`) | Repara | Ver la regla abajo. |
| Venta negativa (devoluciones registradas como venta) | No guarda esa familia y advierte | No se sabe cuánto se vendió realmente. Guardar un 0 sería inventar y guardar el negativo contamina el entrenamiento. El resto de familias sí se guarda. |
| Venta ilegible o ausente | No guarda esa familia y advierte | Igual. |
| Venta mayor a 20 veces el máximo histórico de esa tienda y familia | No guarda esa familia y advierte | Casi seguro es un error de captura (un cero de más, por ejemplo). |
| Venta decimal no entera | Redondea y advierte | |
| Faltan familias | Guarda las que vienen y advierte | |
| Familia repetida con valores distintos | Usa la última y advierte | |
| Envío repetido idéntico | Responde 200 con `registros_guardados: 0` | Idempotencia (ver sección 6). |
| Reenvío con algún valor distinto | Reemplaza, cuenta solo lo que cambió y advierte | Se entiende como una corrección. |
| Ninguna venta utilizable | Rechaza | |

Regla de los separadores numéricos: con un solo separador, si le siguen exactamente tres dígitos y antes hay algo distinto de cero, es separador de miles (`1,234` es 1234). Si no, es decimal (`812,0` es 812). Con los dos, el último es el decimal (`1.234,5` y `1,234.5` son 1234.5). Las ventas son unidades enteras, así que 1.234 unidades no tiene sentido y es mejor leerlo como mil doscientas treinta y cuatro.

Todo lo que llega, defectuoso o no, queda guardado tal cual en `solicitudes` junto con lo que se respondió, y cada corrección queda en `correcciones`. Los rechazos también.

## 3. Información que usa el modelo al predecir

Para pronosticar el día D se usa solo lo que se sabe antes de D, más lo que la solicitud trae del propio día D.

- **De la solicitud**: tienda, fecha, `en_promocion` por familia.
- **Calendario**: día de la semana, día del mes, mes, día del año, días que faltan para fin de mes, feriados nacionales y locales (según la ciudad de la tienda), medio día, fechas comerciales, fechas de pago, y distancia al feriado más cercano hacia adelante y hacia atrás.
- **Historia de ventas** de esa tienda y familia, hasta D-1: ventas de hace 1, 7, 14, 21 y 28 días, medias de las últimas 1, 2, 4 y 8 semanas, y la media del mismo día de la semana en las últimas 4 semanas. También esos valores como razón del nivel reciente.
- **Edad de la tienda** (tope de 365 días), para la rampa de arranque de una tienda nueva.

Lo que no usa: ventas de D o posteriores (hay una prueba que cambia esos datos y verifica que las variables no se muevan), la hora del reloj del sistema, ni promociones de días pasados (las observaciones no las traen).

Un detalle importante de los datos: la tienda 7 reporta cero en todas las familias del 9 al 15 de septiembre de 2024. Es un cierre, no demanda. Esos días se tratan como dato faltante, tanto como fila de entrenamiento como dentro de los rezagos. Un cero suelto en una familia sí es una venta real y se conserva.

Para entrenar con lo que registra la API, las promociones de los días observados salen de la última solicitud de pronóstico de ese día, porque las observaciones no las incluyen. Si un día no tuvo solicitud, esa fila sirve para calcular rezagos pero no se usa como fila de entrenamiento.

## 4. La cantidad sugerida

La cantidad sugerida no es la venta esperada. Con costos distintos por familia, lo que minimiza el costo esperado es el cuantil `tau = c_faltante / (c_faltante + c_sobrante)` de la demanda (el problema del vendedor de periódicos). Algunos valores: licores 0.95 (quedarse sin licor cuesta 20 veces más que sobrar), abarrotes 0.83, carnes 0.43, panadería 0.29 (sobrar pan cuesta 2.5 veces más que faltar). Para panadería la sugerencia queda por debajo de la venta esperada, y es lo correcto.

Se entrena un modelo de cuantil por familia con ese `tau`, además del modelo de la media. Medido en dos periodos de prueba, sugerir el cuantil en lugar de la media redondeada baja el costo de negocio entre 22 % y 24 %, con el mismo modelo.

Se comprobó que la cobertura empírica (fracción de días en que lo vendido no supera lo sugerido) queda dentro de unos 6 puntos de `tau` en validación, un poco por debajo en las familias de `tau` alto, como es esperable fuera de muestra.

## 5. Los modelos

- **Baseline**: promedio del mismo día de la semana de las últimas 4 semanas, sin promociones ni calendario. La cantidad sugerida es esa media redondeada.
- **Modelo de ML**: `HistGradientBoostingRegressor` de scikit-learn, uno por familia y por objetivo (media con pérdida Poisson y cuantil con `tau`). Aprenden la razón `ventas / nivel`, donde nivel es el promedio de las últimas 2 semanas, y se multiplican por el nivel al predecir. Así una express y un hipermercado comparten forma, y el modelo se adapta cuando el nivel de una tienda cambia.

Resultados con validación temporal (entrenar hasta una fecha y evaluar después, sin mezclar):

| Periodo de prueba | Costo baseline | Costo ML | Mejora | WAPE baseline → ML |
|---|---|---|---|---|
| sep–oct 2025 (entrenado hasta 31 ago) | 294 249 | 189 090 | 35.7 % | 0.217 → 0.182 |
| dic 2025–feb 2026 (entrenado hasta 30 nov) | 603 650 | 332 213 | 45.0 % | 0.278 → 0.188 |

Se eligió scikit-learn sobre LightGBM porque trae el cuantil, Poisson y valores faltantes sin dependencias del sistema extra en la imagen. Los hiperparámetros se dejaron casi por defecto: variaciones razonables movieron el costo menos de 1 %, así que ajustarlos más habría sido ajustar al ruido.

### Alternativas que se probaron y se descartaron

- **Ponderar más lo reciente al reentrenar** (vida media de 7 o 14 días): empeoró el costo (30.8 a 37.0 por fila en la prueba de cambio de demanda). Se pierde tamaño de muestra efectivo y el modelo se vuelve más ruidoso.
- **Corregir cada predicción con el error de los últimos 7 días de esa serie**: ayuda 4 a 6 % cuando la demanda cambia, pero cuesta 0.5 % cuando no cambia, y exige más lecturas y más lógica por solicitud. No compensó.
- **Nivel de 28 días o de 7 días**: con 14 días el costo sin cambio de demanda es el mismo y con cambio de demanda mejora un poco. 7 días mejoraba una décima más pero con más riesgo de ruido.

La mayor parte de la adaptación a un cambio de demanda viene de las variables de nivel reciente, no del reentrenamiento. En una prueba sintética (bebidas +40 % y promoción de snacks +70 % desde una fecha) el costo por fila pasó de 26 a unos 29–33 sin reentrenar, y reentrenar con una semana de datos nuevos lo bajó poco más.

## 6. Persistencia y operación

- **Idempotencia**: `observaciones` tiene llave primaria `(tienda, fecha, familia)` y se escribe con `INSERT ... ON CONFLICT DO UPDATE ... WHERE ventas IS DISTINCT FROM nuevo`. Si el valor es igual, la fila no se toca y no cuenta en `registros_guardados`. Todo el POST (solicitud, correcciones, filas) va en una sola transacción.
- **Persistencia**: todos los datos viven en volúmenes con nombre (`db_data`, `airflow_db_data`, `mlflow_artifacts`, `model_cache`). `docker compose down` los conserva; solo `down -v` los borra.
- **Arranque desde cero**: el contenedor `init` espera a la base y a MLflow, carga `historico_ventas.csv` en una sola transacción (marca `historico_cargado` en `meta`), y entrena y promueve el primer modelo. Toma menos de un minuto.
- **Sin reloj del sistema**: ninguna validación de fecha usa `datetime.now()`. Los límites salen de los datos (inicio del histórico, apertura de la tienda, último día de feriados). El único reloj que se usa es el de la hora de recepción de la solicitud, solo para auditoría.
- **MLflow caído**: el modelo vive en memoria. Además cada vez que se carga una versión se guarda una copia en `/cache`, y si la API arranca con MLflow caído usa esa copia. El vigilante marca MLflow como no disponible en un ciclo (15 s) y `/health` pasa a `degradado`; cuando vuelve, regresa a `ok` en otro ciclo. Se configuró el cliente de MLflow con 1 reintento y 10 s de espera para que una caída no deje colgados a los hilos.
- **`/health`** devuelve 503 mientras no haya modelo cargado, `degradado` con el motivo si falla la base, MLflow o Airflow, y `ok` si todo responde.
- **MLflow 3 y los nombres de host**: MLflow 3 devuelve 403 a las solicitudes cuyo header `Host` no conoce, y desde los otros contenedores se le llama `mlflow:5000`. Por eso el compose lo arranca con `--allowed-hosts`.

## 7. Criterio de reemplazo del modelo

Cada reentrenamiento pasa por tres tareas de Airflow:

1. **`entrenar_candidato`**: lee histórico y observaciones, aparta los últimos 7 días como validación, entrena el baseline y el modelo de ML con lo anterior, los evalúa en esos 7 días y registra una versión nueva en MLflow, sin alias todavía.
2. **`decidir_promocion`**: evalúa el modelo vigente en esa misma ventana y aplica la regla. Solo aquí se mueve el alias.
3. **`cerrar`**: corre siempre. Si algo falló antes, deja constancia en la base y hace fallar la ejecución. Gracias a esto `GET /admin/reentrenar/{id}` puede informar `fallido` aunque la tarea que se cayó no haya escrito nada.

**Regla**: el candidato reemplaza al vigente si se cumplen las tres cosas:

1. No hay modelo vigente (el primero entra), **o**
2. su costo de negocio en validación es al menos 1 % menor que el del vigente, **y**
3. le gana al baseline en ese mismo costo.

El 1 % evita cambiar de modelo por ruido: con unas 800 filas de validación, diferencias menores son indistinguibles de la variación normal. La condición del baseline es una red de seguridad: un modelo que ni siquiera supera al promedio simple no debería entrar aunque el vigente esté mal.

El `motivo` que devuelve `GET /admin/reentrenar/{id}` es el texto de la regla con los números, por ejemplo: `Costo en validación 18 210 vs 19 940 del vigente (mejora de 8.7%, se exige al menos 1.0%)`.

**Por qué se promueve el modelo validado y no uno reentrenado con todos los datos**: si el modelo vigente se hubiera entrenado con la ventana de validación de la siguiente comparación, llevaría ventaja injusta (ya vio esas respuestas). Al promover el mismo modelo que se validó, el vigente nunca ha visto los datos con los que se le compara después. El costo es que el modelo en producción siempre está entrenado con datos de al menos 7 días atrás; las variables de nivel reciente compensan buena parte de eso.

**Fallas**: si algo falla, el alias no se mueve. La versión candidata puede quedar registrada en MLflow, pero sin alias no la usa nadie.

**Tiempos**: el reentrenamiento tarda del orden de 20 a 30 segundos con el histórico actual, muy por debajo del límite de 10 minutos. El DAG tiene un límite de 10 minutos y cada tarea de 8.

## 8. Limitaciones conocidas

- Lo del reentrenamiento depende de que haya solicitudes de pronóstico para conocer las promociones de los días observados. Si un día solo se mandan observaciones, ese día sirve para rezagos pero no para entrenar.
- Un cambio de demanda tarda al menos 7 días en entrar al modelo, por el diseño anterior. Hasta entonces lo absorben solo las variables de nivel.
- El modelo solo conoce un año de feriados comerciales como Navidad (2024 y 2025), así que el comportamiento de diciembre se aprende de dos observaciones.
- La tienda 10 abrió hace menos de un año; su rampa de arranque se aprendió de una sola tienda.
- Si una familia aparece repetida con valores distintos, se usa la última. Es una convención, y podría ser incorrecta.
