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
- **`central.py`** (`libraedge-central`) — las herramientas del central: `nodo` (aprovisionamiento)
  y `revision`.
- **`aprovisionamiento.py`** (`Aprovisionamiento`, `publicar`, `registrar`, `dar_de_baja`, `estado`,
  `sucursales`, `vigilar`, `asignar_prefijo`) — el aprovisionamiento del central, genérico; el
  producto aporta tablas de referencia, sucursales, una guarda opcional del alta y prefijo (ADR-008; desde ADR-014 sin
  adicional: LibraEdge está incluido en todos los planes).
- **`identidad.py`** (`nodo_actual`, `prefijo_actual`) — `LIBRAEDGE_NODE_ID` y
  `LIBRAEDGE_PREFIJO`, con compatibilidad para `<PREFIJO>_EDGE_NODE_ID`.
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
- **`mapeo.py`** (`siguiente_id_provisorio`, `anotar_mapeo`, `traducir_id`, `SinMapeo`) — el id provisorio negativo de las altas
  del nodo y la tabla `libraedge_mapeo` con la que el central las traduce (ADR-021).
- **`altas.py`** (`AltaEnNodo`, `Referencia`, `parsear_altas`, `formatear_altas`) — las tablas con alta en el nodo y lo que las
  referencia sin FK; viaja por la carga como `LIBRAEDGE_ALTAS_EN_NODO`.
- **`domain/sync.py`** — el modelo de la sincronización, puro:
  `OutboxOperation`, `ReferenceChange`, `ReferenceOperation`,
  `SyncOperationStatus`. Distingue las dos direcciones (operaciones locales que
  suben vs. cambios de referencia que bajan).
- **`sync/`** — el transporte, separado del dominio:
  - `worker` (`OutboxWorker`, `SyncTransport`, `PushResult`, `ResultadoOutbox`) —
    drena el outbox hacia el servidor (push).
  - `pull` (`PullWorker`, `MirrorApplier`, `HttpPullTransport`) — trae y aplica
    los cambios de referencia del servidor (pull), con clave simple o compuesta; con `altas` reapunta las altas del nodo
    cuando baja su mapeo (ADR-021).
  - `reconciliacion` (`reapuntar`, `claves_foraneas_hacia`) — pasa lo que referenciaba al id provisorio al definitivo y
    borra la fila provisoria (ADR-021).
  - `receiver` (`SyncReceiver`) — el lado servidor que recibe el push del nodo.
  - `despacho` (`Despachador`, `SinManejador`) — un manejador por
    `(operation_type, schema_version)`; lo que no tiene manejador va a revisión
    manual, nunca a `applied` (ADR-007).
  - `revision` (`listar_en_revision`, `reintentar`, `descartar`) — la revisión
    manual del central; CLI `libraedge-central revision` (`central.py`).
  - `esquema` (`version_de_alembic`, `EsquemaIncompatible`) — la versión del
    esquema que nodo y central comparan; 409 si difiere.
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

Los instaladores del nodo (`instalador/`, ADR-010/012) se publican en el Release de
cada tag, con la app de escritorio de cada producto adentro (`escritorio/`, Tauri 2,
ADR-015): un binario por producto, compilado en el CI para Windows y Ubuntu 22.04.

## Referencias

- `README.md` — forma del nodo y decisiones de agosto de 2026.
- Wiki: entidad `libraedge`, el análisis `nodo-libraedge-espejo-local`, y la
  auditoría `auditoria-estructural-familia-libra-2026-09`.

## Dónde se arregla (regla de la familia, 2026-10-03)

**El arreglo de fondo vive siempre en el motor** (`libracore`; `libracommerce` y `libra-ui` para
lo suyo), **nunca en este repo.** Lo que otro producto comparte o podría compartir —el protocolo
con ARCA, las reglas fiscales y sus validaciones, las guardas contra duplicados, la numeración, la
cuenta corriente, los componentes de pantalla compartidos— se escribe y se arregla en el motor.
Este repo sólo aporta **costuras** (los hooks que el motor ya expone) y lo suyo: pantallas,
textos y su modelo de datos propio. Si falta la costura, se agrega al motor; si el arreglo hace
falta antes, igual se hace en el motor (PR y tag) y acá se sube el pin: nada «provisorio» en este
repo. Un hueco que se encuentra acá se busca en los demás productos antes de darlo por acotado.
Detalle y motivo: `reglas/producto.md` del wiki.
