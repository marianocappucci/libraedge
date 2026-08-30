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

## Estado

Contratos propios desacoplados de LibraCommerce (`e0b69e0`) y verificados sin ninguna referencia cruzada (`tests/test_imports.py`). Persistencia propia (`libraedge.db.schema`/`libraedge.db.repository.NodeRepository`) para identidad de nodo y outbox — LibraCommerce ya no mantiene copia propia de estas tablas. Consumido como dependencia Git versionada por LibraCommerce (extra `offline`/`offline-server`), instalación en entorno limpio verificada.

**Los dos motores** (Fase 1, 2026-08-29). El repositorio corre sobre SQLite **y** PostgreSQL, y la suite se ejercita contra los dos: cada test aparece como `[sqlite]` y `[postgres]`, y en CI la ausencia de `LIBRACORE_POSTGRES_URL` **falla** en vez de saltear. No hay capa dual propia acá: el SQL es qmark y portable, y contra PostgreSQL el llamador pasa la conexión de LibraCore —la misma que usa el producto, porque estas tablas viven en SU base para que el enqueue entre en su transacción—, que ya traduce el paramstyle y los errores.

El cruce encontró dos defectos que una suite solo-SQLite no podía ver:

- **Una operación rechazada dejaba la conexión inservible.** En PostgreSQL una sentencia fallida aborta la transacción entera; sin rollback, todo lo que siguiera moría con *"current transaction is aborted"*. En el nodo eso se llevaría puesta la venta que abrió la transacción. Ahora lo cubre `NodeRepository._escribir()`, y el rollback de `SyncReceiver.accept()` tiene test propio.
- **`created_at` dependía del reloj del servidor.** Con el `DEFAULT CURRENT_TIMESTAMP` de la tabla, SQLite escribía `2026-08-30 02:44:13` y PostgreSQL `2026-08-29 23:44:14.017409-03`: el mismo instante en formas distintas, en una columna TEXT. Ahora se estampa en Python, ISO-8601 UTC, como `sent_at` y `acknowledged_at`.

> `SqliteNodeRepository` se conserva como alias de `NodeRepository`: lo importa LibraCommerce.

**Autenticación por nodo**: `POST /sync/v1/push` exige `Authorization: Bearer <secret>` — antes de esto, cualquiera que alcanzara el endpoint podía inyectar operaciones que el `operation_handler` central materializaría como datos de dominio reales (ej. una venta confirmada). `SqliteNodeRepository.register_node()` da de alta un nodo y devuelve su secreto una sola vez (solo se persiste el hash); `deactivate_node()` revoca uno (mini PC robada/dada de baja). `create_sync_app(receiver, node_repository)` ahora requiere el repositorio como segundo argumento — sin consumidores externos todavía, sin impacto real.
