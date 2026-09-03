# Arquitectura — LibraEdge

## Propósito y límites

LibraEdge es la **infraestructura edge** opcional y premium de la familia Libra:
sostiene el **nodo local** de una sucursal, la instancia que sigue operando
durante un corte de conexión con la nube y sincroniza cuando vuelve. El dominio
de negocio **no** vive acá — pertenece a cada motor o producto (LibraCommerce u
otro traduce sus operaciones a la forma que LibraEdge sincroniza). LibraEdge
aporta el nodo, el buffer de escritura (outbox) y el transporte de
sincronización; no sabe qué es una venta, sólo que hay una operación a propagar.

## Forma del nodo (decisiones del 2026-08-29)

- **El local le habla siempre al nodo, nunca a la nube.** No hay failover que
  programar: cuando la conexión cae, el nodo ya era el destino.
- **Autoridad asimétrica.** El servidor manda sobre los datos de referencia; el
  nodo los espeja (pull) y sólo origina las operaciones locales (push).
- El nodo corre el **producto entero con PostgreSQL embebido** en la PC del
  cliente — no una app local reducida. Es lo que hace que el outbox pueda vivir
  en la misma base que la venta, dentro de la misma transacción: sin eso se
  pierde la atomicidad del encolado. (Ver `nodo-libraedge-espejo-local` en el
  wiki.)

## Componentes

- **`cli.py`** (`libraedge-nodo`, console script) — arma el nodo:
  `construir_nodo`, `tablas_espejo`, `main`. Es la entrada operativa.
- **`nodo.py`** (`Nodo`, `EstadoNodo`, `escribir_estado`/`leer_estado`) — el
  objeto nodo y su estado persistido.
- **`db/`** — el almacenamiento del nodo:
  - `schema` (`init_schema`).
  - `repository` (`NodeRepository`, con secretos hasheados).
  - `changelog` — el mecanismo de captura de cambios: instala **triggers** sobre
    las tablas publicadas (`instalar_trigger`, `tablas_publicadas`,
    `desinstalar_trigger`, `sembrar`, `listar_cambios`), con validación de
    identificadores. Es lo que llena el outbox sin que el producto tenga que
    llamar a nadie.
- **`domain/sync.py`** — el modelo de la sincronización, puro:
  `OutboxOperation`, `ReferenceChange`, `ReferenceOperation`,
  `SyncOperationStatus`. Distingue las dos direcciones (operaciones locales que
  suben vs. cambios de referencia que bajan).
- **`sync/`** — el transporte, separado del dominio:
  - `worker` (`OutboxWorker`, `SyncTransport`, `PushResult`, `ResultadoOutbox`) —
    drena el outbox hacia el servidor (push).
  - `pull` (`PullWorker`, `MirrorApplier`, `HttpPullTransport`) — trae y aplica
    los cambios de referencia del servidor (pull).
  - `receiver` (`SyncReceiver`) — el lado servidor que recibe el push del nodo.
  - `api` (`create_sync_app`, `create_sync_router`) — la app/router FastAPI de
    sincronización.
  - `http` (`HttpSyncTransport`) — la implementación HTTP del transporte.
- **`bandeja.py`** / **`bandeja_windows.py`** — la bandeja de estado del nodo
  (`resumen_para_la_bandeja`, severidades, antigüedad del último sync), con su
  integración de icono en Windows: el operador de la sucursal ve de un vistazo si
  el nodo está al día o atrasado.

## Diseño: outbox transaccional + triggers

Las dos piezas que definen el motor:

1. **Outbox en la misma base que el dato.** La operación local y su encolado se
   escriben en una sola transacción. Por eso el nodo corre el producto con
   PostgreSQL embebido y no un SQLite aparte: partir la base partiría la
   atomicidad.
2. **Captura por trigger, no por llamada.** El `changelog` instala triggers sobre
   las tablas publicadas, así el producto no tiene que acordarse de notificar
   cada cambio — la captura es del motor, invisible para el consumidor. Mismo
   principio de mínima huella que el resto de la familia.

`domain/sync` (el modelo) y `sync/` (el transporte) están separados a propósito:
se puede cambiar el cómo se transmite sin tocar qué se transmite.

## Distribución

Paquete `libraedge` (build `hatchling`) con el console script `libraedge-nodo`,
versión pineada al tag (`v0.6.10` al 2026-09).

## Referencias

- `README.md` — forma del nodo y decisiones de agosto de 2026.
- Wiki: entidad `libraedge`, el análisis `nodo-libraedge-espejo-local`, y la
  auditoría `auditoria-estructural-familia-libra-2026-09`.
