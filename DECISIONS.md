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

## ADR-008 — Aprovisionamiento del central genérico, identidad y prefijo del nodo

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: el aprovisionamiento del central (`publicar`, `registrar`, `dar-de-baja`, `estado`,
  `sucursales`, `vigilar`) vivía en `restolibra/scripts/nodo_offline.py` y era casi todo
  genérico; VentaLibra y ContaLibra lo iban a necesitar igual (fase 1 de LibraEdge en toda la
  suite). Cada producto leía además su propia variable de identidad (`RESTOLIBRA_EDGE_NODE_ID`)
  mientras el proceso del nodo leía `LIBRAEDGE_NODE_ID`. Y la numeración de ventas del nodo
  (libracommerce, E2) necesita un prefijo que no se repita entre nodos.
- Decisión:
  1. **`libraedge.aprovisionamiento`** y el CLI `libraedge-central nodo
     publicar|sucursales|registrar|dar-de-baja|estado|vigilar`. El producto aporta sólo una
     costura declarativa, `Aprovisionamiento(prefijo, tablas_de_referencia, listar_sucursales,
     verificar_alta=..., conectar=None)`, que el CLI carga con `--aprovisionamiento modulo:objeto`
     o `LIBRAEDGE_APROVISIONAMIENTO`. `estado`, `dar-de-baja` y `vigilar` no la necesitan.
  2. **Tablas de referencia con su clave**: simple o compuesta, declarada o leída de la base. Si
     se declara, se verifica contra la PK real; una tabla sin PK o con clave distinta se saltea y
     `publicar` sale 1. Desde v0.7 el espejo aplica claves compuestas, así que ya no se saltean.
     `publicar` converge (retira el trigger de lo que salió de la lista) y siembra padres antes
     que hijos.
  3. **La guarda del adicional `offline`** (libracore ADR-048) es la de libracore por defecto
     (`guarda_offline_de_libracore(plans="plans")`), importada recién al registrar: libraedge no
     depende de libracore en runtime. Sin libracore instalado el alta se rechaza; un producto que
     no cobra el offline pasa `verificar_alta=sin_guarda` explícito. La guarda de un segundo nodo
     activo en la sucursal es del motor.
  4. **Identidad**: `LIBRAEDGE_NODE_ID` y `libraedge.identidad.nodo_actual(prefijo=None)`, que
     también lee `<PREFIJO>_EDGE_NODE_ID` por compatibilidad; si las dos están y difieren,
     `IdentidadEnConflicto`.
  5. **Prefijo de numeración por nodo**: `registrar` asigna y guarda en
     `node_identity.prefijo` (columna nueva, índice único) un prefijo de mayúsculas y dígitos de
     hasta 8: `S02` para el primero de la sucursal 2, después `S02B`, `S02C`… Es único **por
     nodo**, no por sucursal, y **nunca se reusa** el de un nodo dado de baja; re-registrar el
     mismo `node_id` conserva el suyo. Se entrega como `LIBRAEDGE_PREFIJO`, junto con
     `LIBRAEDGE_NODE_ID`, `LIBRAEDGE_NODE_SECRET` y `LIBRAEDGE_TABLAS_ESPEJO`; el producto lo lee
     con `nodo_desde_entorno("LIBRAEDGE_NODE_ID", "LIBRAEDGE_PREFIJO")` de libracommerce.
  6. **`vigilar`** sale 1 si un nodo activo lleva más de `--umbral` minutos (15) sin hablar o
     nunca habló; los revocados no cuentan. Una marca sin zona se lee como UTC (antes
     `nodos_para_vigilar` levantaba `TypeError`).
- Consecuencias: un producto nuevo se aprovisiona declarando su costura, sin copiar el script.
  `node_identity` suma `prefijo` (nulable; `init_schema()` lo agrega con `ALTER`): los nodos de
  antes no tienen prefijo hasta que se los vuelve a registrar. RestoLibra sigue andando con su
  script hasta migrar (E5).

## ADR-009 — El central le presta al nodo el ticket de ARCA

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: decisión del humano (fase 1, E4): el nodo factura cuando tiene internet, pero la
  clave privada de ARCA no sale del VPS. Copiarla tampoco serviría: ARCA da un solo ticket de
  acceso (WSAA) a la vez por certificado y servicio, así que el nodo no puede autenticarse por su
  cuenta mientras el del central vale (`alreadyAuthenticated`). libracore #418 (ADR-074 del motor)
  aporta las dos puntas de negocio: `ta_para_el_nodo(emisor_id)` en el central y
  `numerador_con_ta(obtener_ta)` en el nodo. Falta el transporte.
- Decisión:
  1. **`POST /sync/v1/ticket-arca`** en `create_sync_router(..., prestar_ticket=...)`, con cuerpo
     `{"node_id", "emisor_id"}` (`emisor_id` entero o `null`). Pasa por el mismo `_autenticar`
     que push y pull: secreto de un nodo **activo** (un nodo dado de baja recibe 401) y latido
     anotado; después, el mismo control de esquema (409). Sin la costura, 501 (después de
     autenticar, para no contarle nada a quien no es un nodo).
  2. **La costura es del producto**: `prestar_ticket(emisor_id) -> dict` (sincrónica o `async`;
     normalmente `libracore.nodo_offline.ta_para_el_nodo`). LibraEdge no importa libracore. Si
     levanta: una excepción con `detalle()` (`NodoSinArca`) contesta 422 con ese detalle;
     `ValueError`/`LookupError` (emisor desconocido), 422 genérico; cualquier otra (ARCA no
     autentica), 502. Nunca se usa el texto de una excepción cualquiera.
  3. **En el nodo, `libraedge.sync.ticket.ClienteDeTicket`** (`cliente_desde_entorno()` con
     `LIBRAEDGE_CENTRAL_URL`, `LIBRAEDGE_NODE_ID`, `LIBRAEDGE_NODE_SECRET`): `pedir_ticket(emisor_id)`
     guarda el ticket **sólo en memoria**, por emisor, hasta 10 minutos antes de su `expiracion`
     (sin zona o ilegible, no se guarda); `olvidar(emisor_id)` lo descarta. `para_numerador(
     sin_conexion=SinConexion)` devuelve el `obtener_ta(arca)` de `numerador_con_ta` (pide por
     `arca["id"]`).
  4. **Errores del nodo**: sin red o 5xx → `SinConexionConElCentral` (se arregla solo; con
     `sin_conexion=` se levanta la del motor y la venta va a la bandeja); 401/422/501 →
     `TicketRechazado` (lo tiene que mirar alguien); 409 → `EsquemaIncompatible`.
  5. **El ticket es una credencial**: nunca se loguea (ni en el central ni en el nodo), no va en el
     mensaje de ninguna excepción, la respuesta sale con `Cache-Control: no-store` y no se escribe
     a disco en el nodo. Viaja por el mismo HTTPS que la sincronización.
- Consecuencias: el nodo factura con internet sin la clave; sin central no hay ticket y la venta
  va a la bandeja (coherente con «en línea es ver al central» de libracore ADR-074). Cualquier
  nodo activo de la instancia puede pedir el ticket de cualquier emisor de esa instancia: no se
  ata el emisor a la sucursal del nodo (un central es un solo cliente). `create_sync_app` (el
  proceso dedicado) no tiene la ruta.

## ADR-010 — Un instalador del nodo para todos los productos

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: E7 de la fase 1 (pieza A3). El instalador de Windows (`instalador/`) estaba atado a RestoLibra en 28
  lugares (nombre, una sola base, `RESTOLIBRA_DATABASE_URL`, tres cadenas de migraciones cuando ya son cuatro) y
  VentaLibra y ContaLibra ya adoptaron el nodo (ventalibra#641, contalibra#526). Además: el secreto del nodo y la clave
  de PostgreSQL viajaban como parámetros (Inno Setup escribe su línea de comandos en `/LOG`); los pasos eran entradas de
  `[Run]`, cuyo código de salida Inno no mira; el `config.json` del emisor (CUIT, condición de IVA, razón social), que la
  facturación del nodo lee y el espejo no trae, no lo dejaba nadie; y la lógica sólo se podía probar en Windows.
- Decisión:
  1. **Una definición por producto**, `instalador/productos/<producto>.json` (formato `libraedge-producto/1`): nombre,
     `AppId` (fijo; el de RestoLibra es el de siempre), prefijo de variables, bases (una o varias, cada una con las
     variables que apuntan a ella y una marcada `nodo`, que es `LIBRAEDGE_DATABASE_URL`), `base_auth`, módulo ASGI,
     puerto, ruta de salud, **las migraciones del central en orden** (`configure(migraciones=...)` de
     `scripts/panel_admin.py`), extras de pip y variables fijas. Una clave desconocida es un error. Hay definiciones
     para RestoLibra, VentaLibra y ContaLibra. Se compila con `ISCC /DProducto=<producto> nodo.iss`; el `.iss` sólo
     necesita nombre y `AppId`, en una tabla que un test compara con los `.json`.
  2. **La carga del nodo es un archivo**, `carga-nodo.json` (formato `libraedge-carga/1`), que escribe
     `libraedge-central nodo registrar ... --carga RUTA --central-url URL` (permisos 600, sin pisar uno existente, sin
     imprimir el secreto; URL, ruta libre y CUIT se validan **antes** de dar el alta). Trae las cuatro variables de
     ADR-008 más `LIBRAEDGE_CENTRAL_URL`, la sucursal y el emisor. El instalador la toma del asistente (un selector de
     archivo) o de `/CARGA=` en silencio. Sólo viajan rutas por las líneas de comandos; los mensajes nombran variables,
     nunca valores.
  3. **El `config.json` del emisor viaja en la carga** (y no se baja del central al instalar): se toma en el mismo paso
     que ya emite el secreto, sin una ruta nueva en el central ni credenciales en el instalador, y la instalación no
     necesita internet. Lista cerrada (`empresa_*`, `ticket_*`): MercadoPago, SMTP, el estado del servicio y el logo no
     viajan. Se fusiona en `DATA_DIR\config.json`. No sigue los cambios del central (se reinstala con otra carga).
  4. **Lo que no es Windows va en Python**, `libraedge/instalador.py`: sólo biblioteca estándar y sin importar
     `libraedge`, porque el instalador lo copia y lo corre con el Python embebido del producto, que trae la versión de
     LibraEdge que tenga pineada. Lee y valida definición y carga, arma el `nodo.env` (conserva `SECRET_KEY` y la clave
     del admin local al reinstalar; genera una clave de admin para que ningún producto la invente ni la escriba en el
     log), fusiona el `config.json` y corre los console scripts por sus entry points (el paquete se arma desde Linux y
     no trae los `.exe`).
  5. **El `.iss` sólo copia archivos** y corre `instalar.ps1`; su código de salida es el del instalador
     (`GetCustomSetupExitCode`, 20 si falló la configuración). La clave de PostgreSQL ya no se pide: se genera la
     primera vez y queda en `postgres.clave` con ACL restringida. Antes de arrancar el producto se hace una primera
     sincronización (si bajan los usuarios, el producto no crea un admin local). Un nodo por PC.
  6. **`verificar_nodo.ps1`** chequea servicios, ACL y variables de `nodo.env`, PostgreSQL (zona, checksums), las
     cadenas de migraciones contra la definición, `libraedge-nodo estado`, el `estado.json`, la salud del producto
     (objeto JSON) y el emisor. **`armar_paquete.sh`** arma el paquete desde Linux con
     `uv pip install --python-platform x86_64-pc-windows-msvc` sobre el `uv.lock` del producto.
- Consecuencias: un producto nuevo se instala agregando su `.json` y su fila en el `.iss`. `--carga` llega a los
  productos cuando suban el pin de LibraEdge; mientras tanto la carga se arma a mano desde lo que imprime `registrar`.
  El instalador parametrizado **todavía no corrió en Windows** (`instalador/PROBAR_EN_VM.md`). Quedan abiertos los
  usuarios y altas de referencia del nodo (fase 2): la primera sincronización evita el admin local sólo si hay central
  al instalar.

## ADR-011 — «Sucursal sin internet»: el alta del nodo desde la pantalla del central

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: con ADR-010 el instalador está publicado en los Releases (`libraedge-nodo-<producto>.exe`, v0.10.0) y la
  carga del nodo sólo la emite `libraedge-central nodo registrar --carga`, un comando en el VPS. Decisión del humano
  (2026-10-19): el administrador de la instancia da de alta el nodo de una sucursal desde una pantalla del central y se
  lleva el instalador y la carga.
- Decisión:
  1. **El router es del motor**, `libraedge.pantalla_del_central.create_nodos_router(aprov, abrir_conexion)`: llama a
     las mismas funciones que el CLI (`publicar`, `registrar`, `dar_de_baja`, `armar_carga`); el producto sólo lo monta
     con su `Aprovisionamiento` y su guarda de administrador, y agrega la entrada al menú. La pantalla es del kit
     (`libra-ui/SucursalSinInternet`).
  2. **El secreto sólo viaja en la respuesta que es la carga**: `POST` (nunca `GET`), adjunto, `Cache-Control:
     no-store`, sin cabeceras ni errores que lo lleven; en el central queda sólo el hash. No hay «volver a bajar»: una
     carga perdida se reemplaza con `POST .../{node_id}/carga`, que emite otro secreto e invalida el anterior (la misma
     PC; otra PC es un nodo nuevo con prefijo nuevo).
  3. **El alta genera el `node_id`** (`nodo-<sucursal>-<6 hex>`): una PC nueva nunca reusa uno, así que nunca reusa un
     prefijo de numeración.
  4. **El alta publica** (triggers y siembra, idempotente) antes de registrar: sin eso el nodo arranca con el espejo
     vacío. Si queda una tabla sin publicar (sale 1), es 409 y no se emite nada.
  5. **Antes de emitir** se validan la URL del central (sólo https: `LIBRAEDGE_CENTRAL_URL`, el origen de
     `<PREFIJO>_RESET_URL_BASE`, el `Origin` del pedido o `X-Forwarded-Proto` + `Host`) y el CUIT del emisor; las
     guardas del CLI (sucursal activa, un nodo por sucursal, adicional `offline`) son las mismas y se traducen a 422,
     409 y 403 con `{codigo, mensaje}`.
  6. **El adicional se informa en el `GET`** (`modulos.offline`) para que la pantalla lo explique; el que corta el
     alta es `aprov.verificar_alta`. Listar y dar de baja andan sin él: un nodo instalado tiene ventas sin subir.
  7. **La entrada del menú se ve con el adicional o con nodos activos** (decisión del humano, 2026-10-19): `GET
     /api/nodos-offline/menu` devuelve `{contratado, nodos_activos, visible}` y el layout del kit la pide una vez al
     cargar. Sin el adicional el alta y la carga nueva siguen en 403; los nodos activos sólo se pueden dar de baja.
- Consecuencias: el CLI sigue igual y es el camino para soporte. El `vigilar` del VPS (cron) sigue pendiente del primer
  nodo real. La revisión manual de operaciones sigue siendo sólo CLI.


## ADR-012 — El instalador del nodo para Linux: un `.deb` por producto

- Estado: aceptada
- Fecha: 2026-10-19
- Contexto: el instalador de Windows (ADR-010, v0.11.0) está probado y publicado. Decisión del humano: el nodo también
  se instala en una PC con **Ubuntu 22.04 o 24.04 (amd64)**, con la misma lógica y otra capa de sistema operativo.
- Qué ya existe (y se reusa):
  - `libraedge/instalador.py` (ADR-010): definición del producto, carga, `nodo.env` (conserva `SECRET_KEY` y la clave
    del admin), `config.json` del emisor, `revisar-entorno` y `ejecutar` de console scripts por sus entry points. Se
    copia tal cual al paquete, como en Windows.
  - `instalador/productos/*.json` (las cuatro cadenas de migraciones de `scripts/panel_admin.py` de cada producto) y
    `instalador/carga-de-prueba.json`.
  - El orden y las guardas de `instalar.ps1`, `preparar_postgres.ps1`, `preparar_nodo.ps1`, `respaldo.ps1` y
    `verificar_nodo.ps1`, con lo que encontró la VM del 2026-10-19 (`plans.py` y los módulos sueltos de la raíz, base
    de verificación del respaldo única por corrida, servicios que vuelven si falla antes de tocar la base).
  - `armar_paquete.sh`: `uv export --frozen` del `uv.lock` del producto, `git archive` de `HEAD`, el frontend.
  - `libraedge bandeja --una-vez` (desde v0.6) y `bandeja.py`, la decisión de qué mostrar.
  - En la familia no había empaquetado Linux: las unidades de systemd que existen son las de los backoffices en el VPS
    (`restolibra/admin/deploy/restolibra-admin.service`, root y `EnvironmentFile`), y los productos corren en Docker
    (`python:3.12-slim`, `TZ` del contenedor). Se buscó `dpkg-deb`, `systemd`, `.service`, `python-build-standalone`,
    `uv python install` y `appindicator` en libraedge, libracore, ventalibra, restolibra, contalibra y libra-web-kit.
- Decisión:
  1. **Un `.deb` por producto**, `libraedge-nodo-<producto>_<version>_amd64.deb`, armado con `dpkg-deb` por
     `instalador/linux/armar_deb.sh` (sin sudo). Se instala con `apt install ./x.deb` y **se configura con la carga**:
     `sudo libraedge-nodo-instalar /ruta/carga-nodo.json`, o `LIBRAEDGE_CARGA=/ruta` en el entorno de `apt`. Sin carga
     el paquete queda instalado y sin configurar (el `postinst` sale con 0 y lo explica; el comando sale con 2); con
     `LIBRAEDGE_CARGA` un fallo hace fallar el `postinst`. Actualizar con el nodo configurado migra y rearranca con el
     `nodo.env` que ya está, sin pedir la carga. Un nodo por PC en el `preinst` (antes de desempaquetar), sin
     `Conflicts:` (apt sacaría el otro nodo sin preguntar). `remove` conserva configuración y datos; `purge` borra el
     `nodo.env` (el secreto), el estado, los logs y la regla de ufw, y **los datos quedan** (como en Windows).
  2. **Python 3.12 propio** (python-build-standalone por `uv python install`) en `/opt/libraedge-nodo/python`, con las
     dependencias del `uv.lock` del producto instaladas para `x86_64-manylinux_2_35` (la glibc de 22.04) y el producto
     compilado a `.pyc`. Un `.pth` hace importable el producto desde cualquier directorio.
  3. **PostgreSQL 16 de PGDG** (`postgresql-16` en `Depends`; en 22.04 hay que agregar el repositorio antes, con el
     script oficial de `postgresql-common`) en un **clúster propio**, `pg_createcluster 16 libraedge`, en
     `127.0.0.1:55432`, con la zona en el initdb, `--data-checksums` e ICU `en-US` como en Windows. La clave se genera en
     `postgres.clave` (root 600) y se le pone al rol por la **entrada** de `psql`; crear bases, verificar y respaldar
     usan el socket con autenticación peer como `postgres`, sin la clave.
  4. **systemd**: `libraedge-producto` y `libraedge-nodo` como el usuario de sistema `libraedge` (no root), con
     `EnvironmentFile=/etc/libraedge-nodo/nodo.env` (root 600, lo lee systemd), `Restart=always`, `RestartSec=15`;
     `libraedge-respaldo.timer` cada hora (como `postgres`); regla de ufw para las redes privadas si ufw está activo.
  5. **`ejecutar --entorno ARCHIVO --como USUARIO`** en el ayudante: carga el `nodo.env` dentro del proceso y recién
     después baja de usuario. Las migraciones, la primera sincronización y `libraedge-nodo estado` corren así: el
     entorno no pasa por `env VAR=...` (visible en `/proc/<pid>/cmdline`) ni por un `source` de bash (un `$(...)` en un
     valor se ejecutaría como root).
  6. **La bandeja de Linux** corre con el `python3` del sistema y `python3-gi` (AyatanaAppIndicator), no con el Python
     propio: PyGObject no se instala con pip (se compila contra GTK y girepository del sistema). No importa libraedge
     (22.04 trae 3.10): le pregunta al Python del nodo `libraedge-nodo bandeja --una-vez` y dibuja eso. Las
     dependencias de escritorio van en `Recommends`. Autostart en `/etc/xdg/autostart/` y se abre al terminar de
     instalar en las sesiones gráficas activas (`systemd-run --user`).
  7. **`estado.json` queda 0644** (`escribir_estado`): lo lee la bandeja con el usuario de la sesión y no tiene
     secretos; `mkstemp` lo dejaba 0600. Hasta que los productos suban el pin de LibraEdge, `libraedge-estado.path` lo
     corrige cada vez que cambia (como `libraedge`, no como root).
  8. **CI**: `publicar-instalador.yml` arma el `.deb` de cada producto en `ubuntu-22.04` (con el frontend que ya armó el
     job de Windows) y lo instala y verifica en `ubuntu-22.04` y `ubuntu-24.04`: sin carga no configura, una carga de
     otro producto se rechaza sin crear el clúster, con la carga de prueba configura y `verificar_nodo.sh
     --sin-central` da 0, el respaldo restaura, reinstalar (con y sin carga, con ufw activo) conserva `SECRET_KEY`,
     ningún secreto (el de la carga ni la clave de PostgreSQL) aparece en los logs, y `remove`/`purge` conservan los
     datos. El job `publicar` sube también los `.deb` (con versión y con el alias `libraedge-nodo-<producto>_amd64.deb`)
     y los suma a `SHA256SUMS.txt`.
- Consecuencias: un producto nuevo se suma igual que en Windows (su `.json`). La bandeja en una sesión gráfica real no
  la prueba el CI: se prueba en una VM con Ubuntu Desktop (`instalador/README.md`, sección Linux). El `.deb` depende de
  que la PC tenga el repositorio de PGDG en 22.04. El arreglo de permisos del `estado.json` llega a los nodos cuando los
  productos suban el pin de LibraEdge; mientras tanto lo cubre `libraedge-estado.path`.
