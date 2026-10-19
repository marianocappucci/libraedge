# Decisiones arquitectónicas — LibraEdge

Registro ADR. Las decisiones no se borran; si dejan de aplicar, se marcan como
reemplazadas. Fechas y motivos salen del código y de la historia registrada en el
wiki (entidad `libraedge` y el análisis `nodo-libraedge-espejo-local`).

## ADR-001 — Motor de infraestructura edge, agnóstico del negocio

- Estado: aceptada
- Fecha: 2026-07-25
- Contexto: una sucursal necesita seguir operando durante un corte de conexión con
  la nube; el mecanismo de nodo + sincronización es transversal, no de un
  producto.
- Decisión: LibraEdge aporta el nodo local, el buffer de escritura (outbox) y el
  transporte de sync; el dominio pertenece a cada motor/producto (LibraCommerce u
  otro traduce sus operaciones).
- Consecuencias: LibraEdge no sabe qué es una venta, sólo que hay una operación a
  propagar; se reusa entre verticales.

## ADR-002 — Autoridad asimétrica entre nube y nodo

- Estado: aceptada
- Fecha: 2026-08-29
- Contexto: hay que resolver quién gana ante un conflicto de datos entre el
  servidor y el nodo.
- Decisión: el servidor manda sobre los datos de referencia (el nodo los espeja
  por pull); el nodo sólo origina las operaciones locales (push). El local le
  habla siempre al nodo, nunca a la nube — no hay failover que programar.
- Consecuencias: el modelo de conflicto es simple y direccional; el nodo nunca es
  autoridad sobre la referencia.

## ADR-003 — El nodo corre el producto entero con PostgreSQL embebido

- Estado: aceptada
- Fecha: 2026-08-29
- Reemplaza: el diseño de julio de 2026 (una mini PC dedicada con Ubuntu Server
  corriendo una app local reducida).
- Contexto: si el producto escribe la venta en un PostgreSQL local y LibraEdge
  encola en un SQLite aparte, se pierde la atomicidad del enqueue — el outbox
  tiene que vivir en la misma base que la venta, en la misma transacción.
- Decisión: el nodo instala el **producto entero con PostgreSQL embebido** en la
  PC del cliente; la pantalla local no imita al producto, es el producto.
- Consecuencias: el outbox comparte la transacción con el dato; se descarta la
  mini PC dedicada. Restolibra es mejor piloto que LibraCommerce justamente por
  esa atomicidad.

## ADR-004 — Captura de cambios por trigger, no por llamada

- Estado: aceptada
- Fecha: 2026-08
- Contexto: obligar al producto a notificar cada cambio para sincronizarlo es
  frágil (se olvida un call site y el cambio no viaja).
- Decisión: `db/changelog` instala triggers sobre las tablas publicadas
  (`instalar_trigger`, `tablas_publicadas`), que llenan el outbox solos.
- Consecuencias: la captura es del motor, invisible para el consumidor (mínima
  huella); a cambio, hay que gestionar triggers y validar identificadores de
  tabla.

## ADR-005 — Separar el modelo de sincronización del transporte

- Estado: aceptada
- Fecha: 2026-08
- Contexto: el cómo se transmite (HTTP hoy) puede cambiar sin que cambie qué se
  transmite.
- Decisión: `domain/sync` tiene el modelo puro (`OutboxOperation`,
  `ReferenceChange`, estados) y `sync/` el transporte (`OutboxWorker` push,
  `PullWorker`/`MirrorApplier` pull, `SyncReceiver` lado servidor,
  `HttpSyncTransport`).
- Consecuencias: se puede cambiar el transporte sin tocar el dominio; las dos
  direcciones (push de locales, pull de referencia) quedan explícitas.

## ADR-006 — Bandeja de estado del nodo para el operador de la sucursal

- Estado: aceptada
- Fecha: 2026-08
- Contexto: quien atiende la sucursal necesita saber, sin abrir una terminal, si
  el nodo está al día o atrasado con la nube.
- Decisión: `bandeja` calcula un resumen con severidad y antigüedad del último
  sync; `bandeja_windows` lo muestra como icono de bandeja en Windows.
- Consecuencias: el estado de sync es visible para un no técnico; el diagnóstico
  no depende de leer logs.

## ADR-007 — Despacho por tipo y versión, revisión manual en el central y control de esquema

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: LibraEdge pasa de un piloto (RestoLibra) a toda la suite (fase 1:
  RestoLibra, VentaLibra y ContaLibra). Con un solo manejador y una sola versión
  por router, cada manejador filtraba con `if operation_type != ...: return`, y
  ese `return` sin error hacía que **una operación de un tipo desconocido quedara
  `applied` y el nodo la confirmara**: se perdía en silencio. Además, una
  restricción violada por el manejador salía `duplicate` (otra pérdida
  silenciosa), lo rechazado no dejaba rastro en el central, no había control de
  versión entre la base del nodo y la del central (si el central migraba,
  `MirrorApplier` fallaba por una columna nueva) y el espejo sólo admitía clave
  primaria simple (VentaLibra tiene `party_roles (party_id, role)`).
- Decisión:
  1. **`Despachador`** (`sync/despacho.py`): el central registra un manejador por
     `(operation_type, schema_version)`, con firma `(conexion, operacion)`; varias
     versiones de un tipo conviven. Lo que no tiene manejador **no se aplica**.
     `create_sync_router(..., despachador=)` y `SyncReceiver(despachador=)`; el
     `operation_handler` único se conserva tal cual (excluyente con el despachador).
  2. **Revisión manual en el central:** lo que no se pudo aplicar (sin manejador,
     versión distinta, `KeyError`/`TypeError`/`ValueError` o `IntegrityError` del
     manejador) se guarda **entero** en `sync_inbox` con estado `manual_review` y el
     motivo; el nodo recibe `rejected` y lo pasa a revisión de su lado. Un reenvío
     de algo en revisión o descartado contesta `rejected` con el motivo, sin
     reprocesar. `sync/revision.py` (`listar_en_revision`, `reintentar`,
     `descartar`, con quién y motivo) y el CLI `libraedge-central revision`; del
     lado del nodo, `libraedge-nodo revision listar|reenviar`. Nada se borra: lo
     descartado queda como constancia.
  3. **Versión del esquema:** texto opaco comparado por igualdad. El producto la
     declara en el router con `version_esquema=` (texto o `(conexion) -> str`);
     lo normal es `version_de_alembic`, que lee todas las tablas `alembic_version*`
     y da lo mismo en el nodo y en el central si están migrados igual. El nodo la
     manda en `X-LibraEdge-Esquema` (`LIBRAEDGE_VERSION_ESQUEMA`: `alembic` por
     defecto, `modulo:funcion` o `ninguna`). Si difiere o falta, push y pull
     contestan **409** con la esperada; el nodo lo lee como `EsquemaIncompatible`
     y la subida lo trata como reintento (las operaciones esperan en el outbox).
     Un central sin versión declarada no controla nada.
  4. **Clave compuesta en el espejo:** `MirrorApplier`, `instalar_trigger` y
     `sembrar` aceptan varias columnas (`("party_id", "role")`, o
     `tabla:party_id+role` en `LIBRAEDGE_TABLAS_ESPEJO`); el `row_id` de una clave
     compuesta es un arreglo JSON de textos, igual en el trigger y en `sembrar`.
- Consecuencias: ningún tipo ni versión desconocidos se pierden en silencio; el
  central tiene con qué revisar. `sync_inbox` suma columnas nulables
  (`node_id`, `operation_type`, `schema_version`, `operation_json`,
  `resuelto_por`, `resuelto_at`, `resolucion`) que agrega `init_schema()` con
  `ALTER`. Mientras un producto siga con el `operation_handler` único, el agujero
  del tipo desconocido sigue abierto **en ese producto**: se cierra al pasar al
  despachador. Actualizar el central antes que los nodos deja a los nodos sin
  sincronizar (409) hasta que se actualicen, a propósito.
