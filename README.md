# LibraEdge

Infraestructura edge opcional y premium de la familia Libra.

LibraEdge sostiene el **nodo local** de una sucursal: la instancia que sigue operando durante un corte de internet y sincroniza con el servidor central cuando vuelve. Incluye persistencia de identidad de nodo y outbox, worker de outbox, receptor idempotente, transporte HTTP y adaptador FastAPI.

El dominio de negocio pertenece a cada motor o producto: LibraCommerce (u otro) traduce sus propias operaciones confirmadas al `OutboxOperation` genérico y provee el `operation_handler` que sabe materializarlas; LibraEdge administra identidad de nodo, secuencias locales, persistencia del outbox, transporte, reintentos y recepción, sin conocer qué es una venta, un turno o cualquier otro dominio.

## Forma del nodo (decisiones del 2026-08-29)

El nodo **no** es una mini PC dedicada con Ubuntu Server, como se diseñó en julio. Se instala en una PC designada del cliente y **corre el producto entero con PostgreSQL embebido**: la pantalla local no imita al producto, *es* el producto. Además:

- **El local le habla siempre al nodo, nunca a la nube.** No hay failover que programar: el corte de internet no cambia nada del lado del usuario.
- **Autoridad asimétrica.** El servidor manda sobre los datos de referencia (el nodo sólo los baja); el nodo manda sólo sobre los eventos nuevos que genera, append-only. Sin merge ni resolución de conflictos.

El plan por fases vive en el wiki (`wiki/analyses/nodo-libraedge-espejo-local.md`).

## Las dos direcciones (Fase 2, 2026-08-30)

No son simétricas, porque la autoridad tampoco lo es:

| | Qué viaja | Quién lo genera | Forma |
|---|---|---|---|
| **Subida** | Los *eventos* del corte: una venta, un cobro | El nodo | `OutboxOperation`, append-only |
| **Bajada** | El *estado* de la referencia: precios, catálogo, clientes | El central | `ReferenceChange`, upsert/delete |

Esa asimetría es lo que hace que **no haya merge ni resolución de conflictos**: no existe una fila que los dos lados quieran escribir.

**La bajada sale de un changelog central con cursor monótono** (`db/changelog.py`), no de comparar `updated_at`. Comparar contra una marca de tiempo tiene tres agujeros: obliga a mantener `updated_at` en cada camino de escritura de cada tabla, dos transacciones pueden commitear fuera de orden de timestamp (y el nodo se saltea esa fila para siempre), y no captura los DELETE.

**Se publica por trigger**, no llamando a una función en cada escritura: son 26 tablas de referencia, y el camino de escritura que se olvide no falla — deja de espejarse en silencio. El trigger no puede saltearse ninguno porque no los conoce. Se instala en el central al aprovisionar, no en las migraciones del producto.

**El snapshot inicial no es un mecanismo aparte**: `sembrar()` vuelca el estado actual al changelog, y un nodo nuevo pide desde el cursor 0. El alta de un nodo y una actualización cualquiera recorren el mismo camino, en vez de tener un segundo lugar donde equivocarse — el que menos se ejercita.

**El nodo aplica primero y avanza el cursor después**, nunca al revés. Si se corta la luz en el medio, lo aplicado queda y lo que falta se vuelve a pedir; los upserts son idempotentes, así que reaplicar es inofensivo. Al revés, el cursor diría que ya se aplicó algo que no se aplicó y ese dato quedaría viejo para siempre.

**`MirrorApplier` sólo escribe en las tablas de su lista.** El nombre de la tabla llega desde la red y termina interpolado en un `INSERT`: sin la lista, un central comprometido —o con un trigger de más— podría hacer que el nodo escriba en una tabla de su propia autoridad, como `ventas`. La lista es el reparto de autoridad hecho código.

## El nodo como proceso (Fase 4, 2026-08-30)

Hasta acá esto era una biblioteca que **no corría sola**: tenía outbox, worker, transporte, receptor y espejo, y nadie los invocaba. Ahora se instala como servicio:

```bash
libraedge-nodo correr --intervalo 60
```

Toda la configuración viene del entorno, que es lo que el instalador escribe una vez: `LIBRAEDGE_NODE_ID`, `LIBRAEDGE_NODE_SECRET`, `LIBRAEDGE_CENTRAL_URL`, `LIBRAEDGE_DATABASE_URL`, `LIBRAEDGE_TABLAS_ESPEJO` y `LIBRAEDGE_ESTADO`. `libraedge-nodo estado` imprime el último estado **sin tocar la base ni el central** — tiene que poder contestar con el servicio caído, que es cuando se pregunta.

**El ciclo es la sonda de conectividad.** No hay ping a un `/health`, y es deliberado: el catch-all de una SPA devuelve `200` para cualquier ruta, así que un nodo apuntado a la URL equivocada tendría la sonda en verde eterno; y una sonda separada se desincroniza de lo que mide, con lo cual la pantalla miente justo en el peor momento. El estado sale del resultado real de sincronizar.

> 🔴 Y eso tuvo una trampa: `OutboxWorker` **atrapa** los errores de transporte a propósito —los convierte en reintentos, que es lo que lo hace durable—, así que un `except` alrededor no ve nada, y su cuenta de procesadas incluía a las que fallaron. Por eso ahora devuelve un `ResultadoOutbox` que distingue confirmadas de fallidas: mirar la cuenta sola declaraba el nodo en línea con la cola entera atascada.

**Primero sube, después baja.** Lo que el nodo generó durante el corte no existe en ningún otro lado; los datos de referencia que el central tiene para darle existen igual si el ciclo se corta a la mitad.

**El nodo no se cae porque se caiga internet.** `sincronizar()` nunca propaga un error de transporte: lo registra, marca el nodo fuera de línea y devuelve. Un `raise` acá sería un proceso muerto en la PC de un cliente en el peor momento posible.

**El estado se escribe de forma atómica** (temporal + `os.replace`): lo lee una UI que refresca sola, y un write directo trunca el archivo antes de escribir — si algo falla en el medio, el estado anterior ya no está.

## Despacho, revisión manual y versión del esquema (v0.7, ADR-007)

El central registra un manejador por `(operation_type, schema_version)` con un `Despachador` y lo pasa a `create_sync_router(get_connection, despachador=...)`. Lo que no tiene manejador, o lo que el manejador rechaza, **no se aplica**: queda entero en `sync_inbox` como `manual_review` y el nodo recibe `rejected`. Se revisa con `libraedge-central revision listar|reintentar|descartar` (con `--por` y, al descartar, `--motivo`); después de reintentarla, `libraedge-nodo revision reenviar <id>` la confirma en el nodo.

Con `version_esquema=version_de_alembic` en el router, un nodo cuyo esquema no es el del central recibe **409** en push y pull con la versión que se espera, en vez de romper el espejo con una columna que no tiene. Las operaciones esperan en el outbox hasta que el nodo se actualice.

El espejo admite clave primaria compuesta: `LIBRAEDGE_TABLAS_ESPEJO=party_roles:party_id+role`.

## Aprovisionamiento del central (v0.8, ADR-008)

El producto declara lo suyo en un `Aprovisionamiento` y el central corre `libraedge-central nodo ...`:

```python
from libraedge.aprovisionamiento import Aprovisionamiento

aprovisionamiento = Aprovisionamiento(
    prefijo="VENTALIBRA",                                  # VENTALIBRA_DATABASE_URL, VENTALIBRA_EDGE_NODE_ID
    tablas_de_referencia=["catalog_items", "units", ("party_roles", "party_id+role")],
    listar_sucursales=get_all_sucursales,                  # id, nombre, activa
    # verificar_alta: por defecto, el adicional `offline` de libracore con el `plans` del producto
)
```

```bash
export LIBRAEDGE_APROVISIONAMIENTO=app.offline:aprovisionamiento
libraedge-central nodo publicar                  # triggers y siembra; sale 1 si queda un agujero
libraedge-central nodo sucursales
libraedge-central nodo registrar node-1 --sucursal Centro
libraedge-central nodo estado
libraedge-central nodo dar-de-baja node-1
libraedge-central nodo vigilar --umbral 15       # para el cron del VPS: sale 1 si un nodo se callo
```

`registrar` imprime una sola vez `LIBRAEDGE_NODE_ID`, `LIBRAEDGE_NODE_SECRET`, `LIBRAEDGE_PREFIJO` (el prefijo de numeración del nodo, `S02`, único por nodo y nunca reusado) y `LIBRAEDGE_TABLAS_ESPEJO`. En el producto, `libraedge.identidad.nodo_actual("VENTALIBRA")` dice si la instancia es un nodo.

## El ticket de ARCA prestado (v0.9, ADR-009)

El nodo factura con internet usando el ticket de ARCA del central; la clave no sale del VPS. En el central:

```python
from libracore.nodo_offline import ta_para_el_nodo

router = create_sync_router(get_connection, despachador=..., version_esquema=version_de_alembic,
                            prestar_ticket=ta_para_el_nodo)   # POST /sync/v1/ticket-arca
```

En el nodo, con `LIBRAEDGE_CENTRAL_URL`, `LIBRAEDGE_NODE_ID` y `LIBRAEDGE_NODE_SECRET` en el entorno:

```python
from libraedge.sync.ticket import cliente_desde_entorno
from libracore.nodo_offline import SinConexion, numerador_con_ta

cliente = cliente_desde_entorno(version_esquema=...)   # la misma que manda libraedge-nodo
numerar = numerador_con_ta(cliente.para_numerador(sin_conexion=SinConexion), emisor_id)
```

Sólo nodos activos (401 si no), 409 si el esquema difiere, 501 si el central no pasó `prestar_ticket`. El ticket se guarda en memoria hasta 10 minutos antes de vencer y nunca se loguea. Sin red o con ARCA caída en el central: `SinConexionConElCentral` (la venta va a la bandeja); el central sin ARCA para ese emisor: `TicketRechazado` (`codigo` `nodo_sin_arca`).

## El instalador del nodo, uno para todos los productos (ADR-010)

`instalador/` arma un instalador de Windows (Inno Setup) por producto a partir de una definición,
`instalador/productos/<producto>.json` (hoy RestoLibra, VentaLibra y ContaLibra): `ISCC /DProducto=ventalibra nodo.iss`.
Lo que el central emite para un nodo llega como un archivo:

```bash
libraedge-central nodo registrar node-1 --sucursal Centro --carga carga-nodo.json --central-url https://cliente.ventalibra.com.ar
```

Con `--carga`, `registrar` no imprime el secreto: escribe `carga-nodo.json` (permisos 600) con el entorno del nodo, la URL
del central y los datos del emisor del `config.json` de la instancia (CUIT, condición de IVA, razón social, formato del
ticket), que la facturación del nodo necesita y el espejo no trae. El instalador la toma del asistente o de `/CARGA=` en
modo silencioso. La lectura de la definición, la validación de la carga y el armado del `nodo.env` viven en
`libraedge/instalador.py` (sólo biblioteca estándar: el instalador lo copia y lo corre con el Python del producto).
Compilar, instalar y verificar: [`instalador/README.md`](instalador/README.md); probarlo en una VM:
[`instalador/PROBAR_EN_VM.md`](instalador/PROBAR_EN_VM.md).

## Estado

Contratos propios desacoplados de LibraCommerce (`e0b69e0`) y verificados sin ninguna referencia cruzada (`tests/test_imports.py`). Persistencia propia (`libraedge.db.schema`/`libraedge.db.repository.NodeRepository`) para identidad de nodo y outbox — LibraCommerce ya no mantiene copia propia de estas tablas. Consumido como dependencia Git versionada por LibraCommerce (extra `offline`/`offline-server`), instalación en entorno limpio verificada.

**Los dos motores** (Fase 1, 2026-08-29). El repositorio corre sobre SQLite **y** PostgreSQL, y la suite se ejercita contra los dos: cada test aparece como `[sqlite]` y `[postgres]`, y en CI la ausencia de `LIBRACORE_POSTGRES_URL` **falla** en vez de saltear. No hay capa dual propia acá: el SQL es qmark y portable, y contra PostgreSQL el llamador pasa la conexión de LibraCore —la misma que usa el producto, porque estas tablas viven en SU base para que el enqueue entre en su transacción—, que ya traduce el paramstyle y los errores.

El cruce encontró dos defectos que una suite solo-SQLite no podía ver:

- **Una operación rechazada dejaba la conexión inservible.** En PostgreSQL una sentencia fallida aborta la transacción entera; sin rollback, todo lo que siguiera moría con *"current transaction is aborted"*. En el nodo eso se llevaría puesta la venta que abrió la transacción. Ahora lo cubre `NodeRepository._escribir()`, y el rollback de `SyncReceiver.accept()` tiene test propio.
- **`created_at` dependía del reloj del servidor.** Con el `DEFAULT CURRENT_TIMESTAMP` de la tabla, SQLite escribía `2026-08-30 02:44:13` y PostgreSQL `2026-08-29 23:44:14.017409-03`: el mismo instante en formas distintas, en una columna TEXT. Ahora se estampa en Python, ISO-8601 UTC, como `sent_at` y `acknowledged_at`.

> `SqliteNodeRepository` se conserva como alias de `NodeRepository`: lo importa LibraCommerce.

**Autenticación por nodo**: `POST /sync/v1/push` exige `Authorization: Bearer <secret>` — antes de esto, cualquiera que alcanzara el endpoint podía inyectar operaciones que el `operation_handler` central materializaría como datos de dominio reales (ej. una venta confirmada). `SqliteNodeRepository.register_node()` da de alta un nodo y devuelve su secreto una sola vez (solo se persiste el hash); `deactivate_node()` revoca uno (mini PC robada/dada de baja). `create_sync_app(receiver, node_repository)` ahora requiere el repositorio como segundo argumento — sin consumidores externos todavía, sin impacto real.
