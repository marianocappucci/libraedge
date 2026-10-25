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

- Estado: aceptada; los puntos 6 y 7 (el adicional y el menú) los reemplaza ADR-014
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
     `Conflicts:` (apt sacaría el otro nodo sin preguntar). **`remove` borra el `nodo.env`** (el secreto del nodo), como
     Windows (decisión del humano, 2026-10-20), y antes guarda `SECRET_KEY` y la clave del admin local en
     `/var/lib/libraedge-nodo/conservado.env` (root 600): reinstalar pide la carga y, con ella, configura sobre los
     datos que estaban sin perder lo que libraauth cifró (`entorno --previo`). `purge` borra además el resto de
     `/etc/libraedge-nodo`, el estado, los logs y la regla de ufw. **Los datos quedan siempre** (como en Windows).
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
  8. **El script de una línea**, `instalar-nodo.sh <producto> <carga>` (decisión del humano, 2026-10-20), se publica
     en el Release al lado de los `.deb` y en `SHA256SUMS.txt`: `curl -fsSL .../releases/latest/download/instalar-nodo.sh
     | sudo bash -s <producto> /ruta/carga-nodo.json`. Controla Ubuntu 22.04/24.04 amd64 y root, agrega PGDG con el
     script oficial de `postgresql-common` (llave en archivo y `signed-by`, sin `apt-key`), baja el `.deb` del mismo
     Release (`LIBRAEDGE_VERSION` para una versión fija), lo verifica contra `SHA256SUMS.txt` y lo instala con las
     recomendadas y `LIBRAEDGE_CARGA`. Va entero en una función con la entrada en `/dev/null`: con `curl | bash`, apt no
     se come el resto del script.
  9. **CI**: `publicar-instalador.yml` arma el `.deb` de cada producto en `ubuntu-22.04` (con el frontend que ya armó el
     job de Windows) y lo instala y verifica en `ubuntu-22.04` y `ubuntu-24.04`: sin carga no configura, una carga de
     otro producto se rechaza sin crear el clúster, con la carga de prueba configura y `verificar_nodo.sh
     --sin-central` da 0, el respaldo restaura, reinstalar (con y sin carga, con ufw activo) conserva `SECRET_KEY`,
     ningún secreto (el de la carga ni la clave de PostgreSQL) aparece en los logs, `remove` borra el secreto y
     conserva los datos, reinstalar sin carga la pide y con ella configura sobre los datos (mismo `SECRET_KEY`), y
     `purge` deja los datos. `instalar-nodo.sh` se prueba en 22.04 y 24.04 sobre el `.deb` del job
     (`LIBRAEDGE_DEB_DIR`): guardas, una suma que no coincide no instala, y `curl | sudo bash -s` con las recomendadas
     configura y verifica. El job `publicar` sube también los `.deb` (con versión y con el alias `libraedge-nodo-<producto>_amd64.deb`)
     y los suma a `SHA256SUMS.txt`, junto con `instalar-nodo.sh`.
- Consecuencias: un producto nuevo se suma igual que en Windows (su `.json`). La bandeja en una sesión gráfica real no
  la prueba el CI: se prueba en una VM con Ubuntu Desktop (`instalador/README.md`, sección Linux). El `.deb` suelto depende de
  que la PC tenga el repositorio de PGDG en 22.04; `instalar-nodo.sh` lo agrega. El arreglo de permisos del `estado.json` llega a los nodos cuando los
  productos suban el pin de LibraEdge; mientras tanto lo cubre `libraedge-estado.path`.

**Windows igualado (2026-10-20):** el desinstalador de Windows (`CurUninstallStepChanged`, `usUninstall`) guarda
`SECRET_KEY` y la clave del admin local en `C:\ProgramData\LibraEdge\conservado.env` (creado vacío, ACL SYSTEM y
Administradores por SID con `icacls`, recién después el contenido) antes de borrar `nodo.env`; `preparar_nodo.ps1` se lo
pasa al ayudante con `--previo` y lo borra. El secreto del nodo no se conserva en ninguno de los dos sistemas. Antes,
desinstalar en Windows perdía el `SECRET_KEY` y lo cifrado en los datos que quedaban era ilegible al reinstalar.

## ADR-013 — En el local se entra siempre por la copia local: el acceso directo «<Producto>»

- Estado: aceptada; el destino del acceso directo (punto 1) y la espera del punto 8 los reemplaza ADR-015: abre la app
  de escritorio y el navegador en modo aplicación queda de respaldo
- Fecha: 2026-10-20
- Contexto: el humano, probando un nodo de ContaLibra en una VM, entró desde la PC del local a la versión en la nube
  (`demo.contalibra.com.ar`); al cortar internet esa pestaña dejó de andar y tuvo que abrir `localhost:8000` y volver a
  loguearse. Nadie sabe que **en la PC con LibraEdge se entra siempre por la copia local, aunque haya internet** (el
  nodo sincroniza por detrás). Decisión del humano: hacerlo obvio, sin una app aparte.
- Qué ya existe (se miró antes):
  - Windows: `preparar_nodo.ps1` crea con `WScript.Shell` el acceso directo de la bandeja en el inicio común
    («LibraEdge - estado del nodo»), y el `.iss` lo borra en `[UninstallDelete]` (lo crea un script, no `[Icons]`). La
    bandeja de Windows (`bandeja_windows.py`) **no tenía «Abrir»**: sólo el estado y «Salir».
  - Linux: la bandeja tenía «Abrir <Producto>» con `xdg-open http://127.0.0.1:<puerto>` (otro host que `localhost`: otra
    sesión del navegador). El único `.desktop` era el autostart de la bandeja (`NoDisplay=true`); el `postrm` no tenía
    nada de escritorio.
  - Íconos: `armar_icono.py` armaba sólo `libraedge.ico` desde `marca/libraedge*.svg` (ADR-078 de libra-ui), con Chrome
    y el `.ico` commiteado. Las marcas de los productos están en libra-ui (`marcas/<producto>.svg` y `-favicon.svg`,
    las mismas que usan las webs).
  - Ningún mensaje del instalador decía cómo entrar: «escucha en http://<esta-pc>:<puerto>». La pantalla del central
    (`SucursalSinInternet` de libra-ui) tampoco.
- Decisión:
  1. **El acceso directo «<Producto>»** abre `http://localhost:<puerto>/` en modo aplicación (ventana sin barra de
     direcciones, con el título y el ícono de la página):
     - Windows: en el escritorio público y en el menú Inicio comun, creado por `preparar_nodo.ps1` (junto al de la
       bandeja), con destino `msedge.exe --app=http://localhost:<puerto>/`. Edge se busca en `App Paths` del registro y
       después en las rutas conocidas; sin Edge, `explorer.exe <url>` (el navegador por defecto). Ícono:
       `iconos/<producto>.ico`, instalado como `{app}\producto.ico`. El `.iss` los borra en `[UninstallDelete]` por
       `{#NombreProducto}.lnk`, el mismo nombre que la definición.
     - Ubuntu: `/usr/share/applications/libraedge-nodo-<producto>.desktop` (lo quita dpkg) con
       `Exec=/opt/libraedge-nodo/bandeja/abrir_producto.sh http://localhost:<puerto>/` e
       `Icon=/opt/libraedge-nodo/marca/<producto>.svg`. `abrir_producto.sh` usa Google Chrome, Chromium o Edge con
       `--app=` y, si no hay ninguno (Ubuntu trae Firefox, sin modo aplicación), `xdg-open`. **No se deja ícono en el
       escritorio de cada usuario**: GNOME lo muestra como no confiable hasta que el usuario lo habilita, y `/etc/skel`
       sólo llega a los usuarios nuevos; el acceso está en el menú y **fijado en el dock** (punto 7).
  2. **«Abrir <Producto>» de la bandeja usa el mismo lanzamiento:** en Windows abre el `.lnk` del menú Inicio (si no
     está, la URL en el navegador) y es la acción por defecto; en Linux corre `abrir_producto.sh`. Las dos suman
     **«Otros puestos: http://<IP>:<puerto> (copiar)»**, que al hacer clic copia la dirección (`clip.exe`; el portapapeles
     de GTK). La bandeja de Windows no recibe argumentos nuevos (lee `<RaizNodo>\instalador\producto.json` al lado de su
     Python): corre con el LibraEdge pineado por el producto, que puede ser anterior.
  3. **El final de la instalación dice cómo se entra, con la IP ya completa**, desde un solo lugar:
     `libraedge_instalador.py acceso --definicion producto.json [--json]`. La IP es la privada (10/8, 172.16/12,
     192.168/16, sin loopback ni link-local) de la ruta por defecto (un socket UDP conectado, que no manda nada); sin
     ruta por defecto, la primera privada del nombre de la PC; sin ninguna, la dirección con la IP a completar. El texto
     va sin acentos (Inno lo lee con la página de códigos del sistema). Windows: `preparar_nodo.ps1` lo escribe en
     `instalador\acceso.txt` y en `logs\instalacion.log`; el `.iss` lo agrega a la página final (`CurPageChanged`,
     `wpFinished`) y al log de Inno. Linux: `instalar.sh` (y por él el `postinst`) y `instalar-nodo.sh` al final.
  4. **Íconos:** `armar_icono.py` arma `libraedge.ico` y un `iconos/<producto>.ico` por cada `productos/*.json`, desde
     `marca/<marca>.svg` y `-favicon.svg` (copias de libra-ui). Se commitean; los tests miran los archivos.
  5. **Abrir el producto al iniciar sesión: opción documentada, apagada** (`instalador/README.md`): copiar el acceso
     directo al inicio común (Windows) o el `.desktop` a `/etc/xdg/autostart/` (Ubuntu). Desinstalar borra la copia en
     los dos (`[UninstallDelete]` y `postrm`).
  6. **La pantalla del central lo dice** (libra-ui ADR-084): la tarjeta del instalador y el alta del nodo.
  7. **Ubuntu: el acceso directo fijado en el dock de todos los usuarios** (decisión del humano, 2026-10-20).
     `instalador/dock.sh` (desde el `postinst`) borra su override, recompila y lee el valor por defecto de
     `org.gnome.shell favorite-apps` **del sistema** con `gsettings` en un entorno vacío (`GSETTINGS_BACKEND=memory`, sin
     la configuración de ningún usuario), para la sesión genérica (`XDG_CURRENT_DESKTOP=GNOME`) y para la «ubuntu»
     (`ubuntu:GNOME`, los favoritos de `10_ubuntu-settings`, distintos en 22.04, 24.04 y 26.04). Les suma
     `libraedge-nodo-<producto>.desktop` al final (`libraedge_instalador.py favoritos`) y los escribe en
     `/usr/share/glib-2.0/schemas/90_libraedge-nodo.gschema.override`, de prioridad propia, y recompila. El `postrm`
     (`remove` y `purge`) lo borra y recompila. Un trigger `interest-noawait /usr/share/glib-2.0/schemas` lo rehace si
     el sistema cambia sus favoritos (una actualización de `ubuntu-settings`). Sin GNOME Shell no hace nada. Se descartó
     una base de dconf del sistema (`/etc/dconf/db/local.d`): exige crear `/etc/dconf/profile/user`, que Ubuntu no trae,
     y eso cambia cómo lee dconf toda la PC. **Cambia el valor por defecto:** quien ya ordenó su dock a mano conserva lo
     suyo y lo agrega desde el menú.
  8. **Navegador en Ubuntu (decisión del humano, 2026-10-20): esperar a Tauri.** No se agrega Chromium: modo aplicación
     si ya hay Chrome, Chromium o Edge; si no, una pestaña del navegador por defecto.
- CI (`publicar-instalador.yml`): en Windows, que estén los dos `.lnk` con destino `msedge.exe`, argumentos
  `--app=http://localhost:<puerto>/` e ícono `producto.ico`, que `acceso.txt`, `instalacion.log` y el log de Inno digan
  cómo se entra con una IP privada, y que desinstalar los borre. En Ubuntu 22.04/24.04/26.04, que el `.desktop` pase
  `desktop-file-validate` con su `Exec` y su ícono, que `abrir_producto.sh` llame al navegador con `--app=` (con uno
  falso adelante en el `PATH`), que la salida de `libraedge-nodo-instalar` y de `instalar-nodo.sh` diga cómo se entra
  con una IP privada, que `remove` borre el `.desktop` y la copia del autostart, y que el valor por defecto de `favorite-apps` (con
  `gnome-shell-common` y `ubuntu-settings` instalados) sea el de antes más el `.desktop` después de instalar y
  exactamente el de antes después de `remove`, en las sesiones genérica y «ubuntu».
- Consecuencias: lo que el CI no puede ver se prueba en una VM (`PROBAR_EN_VM.md`, V8b; README, sección Linux): cómo se
  ve la ventana en modo aplicación, el ícono en el escritorio y en el dock, la página final del asistente, y si Edge pide
  su configuración inicial la primera vez. En Windows, «Abrir» y «Otros puestos» de la bandeja llegan cuando los
  productos suban el pin de LibraEdge; el acceso directo y el cartel final llegan con el instalador.



## ADR-014 — LibraEdge incluido en todos los planes y activación del nodo con un código de vinculación

- Estado: aceptada
- Fecha: 2026-10-20
- Contexto: decisiones del humano del 2026-10-20. (1) **LibraEdge es parte del sistema e incluido en todos los planes**:
  el alta, la carga nueva y la baja de nodos están disponibles para el administrador de cualquier instancia de los
  productos con LibraEdge, sin el adicional `offline`. (2) **El nodo se activa con «dirección + código»** en lugar de
  llevar el archivo `carga-nodo.json` a la PC del local; el archivo queda como alternativa para soporte.
- Qué ya existe (se miró antes de diseñar):
  - En LibraEdge: el router de «Sucursal sin internet» (`pantalla_del_central.create_nodos_router`, ADR-011) con el alta
    que publica y registra (`aprovisionamiento.registrar`, que devuelve el secreto y guarda sólo su hash,
    `NodeRepository.register_node`), la carga como adjunto con `Cache-Control: no-store` y la URL pública del central
    (`resolver_url_del_central`); la carga y su validación en el ayudante del instalador (`instalador.armar_carga`,
    `carga_desde_dict`, `escribir_en_archivo_protegido`, ADR-010), la entrada por `/CARGA=` (Windows) y
    `LIBRAEDGE_CARGA`/argumento (Ubuntu, ADR-012); la guarda del adicional `guarda_offline_de_libracore` (ADR-008) y el
    `contratado` del `GET` y del `/menu` (ADR-011).
  - En libraauth: el **token de un solo uso** del reseteo de clave (`password_reset.py`: `secrets`, se guarda sólo el
    SHA-256, vencimiento con reloj inyectable, **un solo error** `InvalidResetToken` para «no existe / usado / vencido»);
    el **límite de intentos por IP** del login (`auth_events.ip_del_request`, que lee `X-Forwarded-For` desde la derecha
    detrás de los proxies de confianza, y el conteo de fallidos en la base para que valga entre procesos); y el captcha
    (ADR-014 de libraauth), que explica por qué un tope por IP solo no frena a quien reparte intentos entre muchas.
  - En libracore: el catálogo de adicionales (ADR-048) donde los productos declaran `offline`, sin lógica propia de
    LibraEdge. LibraEdge **no importa** libracore ni libraauth en runtime: lo de libraauth se replica en su forma (hash,
    error único, conteo en la base) y la IP real entra por una costura (`ip_del_request`).
- Decisión:
  1. **Sin adicional.** `Aprovisionamiento.verificar_alta` pasa a `sin_guarda` por defecto; `guarda_offline_de_libracore`
     y `adicional_offline_contratado` quedan exportadas por compatibilidad (un producto que todavía las pase sigue
     andando). **Compatibilidad hacia atrás:** `GET /api/nodos-offline` sigue mandando `contratado`, siempre `true`, y
     `GET /api/nodos-offline/menu` contesta `visible: true` siempre: una pantalla o un layout del kit anterior ven todo
     habilitado sin cambiar nada. Un rechazo de la guarda del producto (si alguno la pone) es 403 `alta_rechazada`.
  2. **El código.** 8 símbolos de un alfabeto de 31 sin caracteres confusos (`23456789ABCDEFGHJKMNPQRSTUVWXYZ`: sin
     `0 O 1 I L`), en dos grupos (`K7Q4-M2XP`), con `secrets`: 31^8 ≈ 8,5 × 10^11 (≈ 39,6 bits). **Un solo uso, vence a
     los 15 minutos, se puede anular**; pedir otro para el mismo nodo anula los pendientes, y también los anulan la baja
     y el archivo de soporte. Se guarda **sólo el SHA-256** (`libraedge_vinculacion`); el estado muestra que hay un código
     pendiente y su vencimiento, nunca el código. Al tipearlo se aceptan minúsculas, espacios y guiones; nada se
     «corrige» (una `O` no pasa a `0`).
  3. **El alta con código registra el nodo** (como el alta con archivo: publica, `node_id` nuevo, prefijo) con un
     secreto que nadie ve, y devuelve `{node_id, prefijo, sucursal, direccion, central_url, codigo, vence}` sin caché.
     **El canje lo vuelve a registrar** (`ap.registrar`, el mismo paso que la carga nueva): ahí nace el secreto que se
     usa, y sólo viaja en la respuesta del canje. Pedir otro código no rota el secreto en uso: un nodo instalado sigue
     sincronizando hasta que se reinstale con el código.
  4. **El canje es una ruta pública** (`create_canje_router`, `POST /sync/v1/vinculacion`, al lado de las del nodo, que
     tampoco llevan sesión), montada por el producto **sin guarda**: sólo https (directo o `X-Forwarded-Proto`), el
     producto del instalador tiene que ser el de la instancia (si no, 422 sin mirar el código), y antes de gastar nada
     se valida que se pueda armar la carga (URL pública, CUIT del emisor). El código se marca usado con **una sola
     sentencia** (`UPDATE ... WHERE usado_at IS NULL AND anulado_at IS NULL AND vence_at > ? RETURNING node_id`): con dos
     canjes simultáneos, PostgreSQL toma la fila en el primero y el segundo, al reevaluar el `WHERE`, no encuentra nada
     (probado con dos conexiones en los dos motores). El commit es el de `ap.registrar`: si el registro falla, el código
     no queda gastado. Un código que no sirve —inexistente, vencido, usado, anulado o de un nodo dado de baja— es **un
     solo** 404 `codigo_invalido`.
  5. **Límite de intentos.** Canjes **fallidos** en una ventana de 15 minutos, contados en la base
     (`libraedge_canje_fallido`, para que valga entre procesos y sobreviva a un reinicio): **10 por IP y 60 en total**.
     Pasado cualquiera, 429 `demasiados_intentos` con `Retry-After`, **sin mirar el código** y sin anotar el intento (el
     bloqueo no se extiende solo). Justificación: el tope global vale aunque el atacante reparta los intentos entre
     muchas IP; en los 15 minutos de vida de un código entran como mucho dos ventanas, ≤ 120 intentos, y la
     probabilidad de acertar un código vivo es ≤ 120 / 31^8 ≈ 1,4 × 10^-10 (con diez códigos vivos a la vez, ≈ 1,4 ×
     10^-9). Costo aceptado: quien sature el tope global frena 15 minutos el canje legítimo; para eso queda el archivo,
     que no pasa por esta ruta. La IP real la da el producto (`libraauth.auth_events.ip_del_request`).
  6. **Logs sin el código ni el secreto**: dicen IP, resultado y `node_id` (probado con `caplog`).
  7. **Las tablas** (`libraedge_vinculacion`, `libraedge_canje_fallido`) las crea `init_schema`, que el arranque y la
     revisión `*_libraedge` de los productos ya llaman: aparecen en las instancias vivas sin una migración nueva.
  8. **Los instaladores piden dirección + código.** El canje lo hace el ayudante (`libraedge_instalador.py canjear`,
     sólo biblioteca estándar, `urllib` con `ssl.create_default_context()`, que verifica certificado y nombre; en
     Linux se suma el almacén del sistema por si el OpenSSL del Python propio busca en otro lado) y escribe la carga
     con permisos de secreto, como el archivo; después sigue el camino de siempre (`validar`, `entorno`...). El ayudante
     **no acepta el código por argumento**: lo lee de `LIBRAEDGE_CODIGO` o de `--archivo-codigo`.
     - **Windows:** una página «Vincular con el sistema» (dirección y código) antes de la del archivo, que se saltea con
       el código. En silencio: `/DIRECCION=` y `LIBRAEDGE_CODIGO` (o `/CODIGO=`). El `.iss` pasa el código a
       `instalar.ps1` por la variable del proceso (`SetEnvironmentVariableW`), nunca en la línea de comandos que anota
       el transcript; `instalar.ps1 -Direccion` canjea **antes de tocar nada** (después de controlar «un nodo por PC»)
       a `C:\ProgramData\LibraEdge\carga-nodo.json`, creado antes con la ACL de SYSTEM y Administradores, y lo borra al
       terminar bien. 🔴 **`/CODIGO=` queda en el `/LOG` de Inno**, que escribe su línea de comandos: riesgo
       documentado y aceptado (un solo uso, 15 minutos, el instalador lo canjea apenas copia); con la variable no queda
       en ningún lado.
     - **Ubuntu:** `instalar-nodo.sh <producto> --codigo C --direccion D` y `libraedge-nodo-instalar --codigo C
       --direccion D` (o `LIBRAEDGE_CODIGO`/`LIBRAEDGE_DIRECCION`, que es como el postinst los recibe de apt), además
       del archivo. `instalar.sh` crea la carga canjeada 600 de root en `/var/lib/libraedge-nodo/carga-nodo.json`, la
       borra al terminar bien y, si falla después del canje, dice cómo reintentar con ella. El código no se escribe en
       el log; por argumento queda un momento en `/proc/<pid>/cmdline` y, con `sudo`, en su registro: mismo riesgo
       aceptado, y `sudo --preserve-env=LIBRAEDGE_CODIGO` lo evita.
  9. **El CI lo prueba de punta a punta** (`publicar-instalador.yml`): un central de prueba con https de verdad en el
     runner (`tests/central_de_prueba.py`: el router del motor sobre SQLite, con una autoridad de `trustme` instalada en
     el almacén del sistema) y Windows (`/DIRECCION=` + `LIBRAEDGE_CODIGO`, después `/CODIGO=`, después un código usado
     que sale con 20 sin tocar el `nodo.env`) y Ubuntu 22.04/24.04/26.04 (apt con `LIBRAEDGE_CODIGO`,
     `libraedge-nodo-instalar --codigo`, `instalar-nodo.sh --codigo`, un código usado que sale con 2) se instalan con el
     código; se verifica que el secreto del `nodo.env` es el que el central verifica (`/prueba/secreto`), que el código
     no sirve dos veces, que la carga canjeada no queda y que ni el código ni el secreto aparecen en los logs.
- Descartadas: mostrar el secreto (o un QR con él) en la pantalla; un código de 6 símbolos (≈ 30 bits: con el mismo
  tope sería ≈ 1,4 × 10^-7 por código, aceptable, pero 8 cuesta dos teclas y deja margen si el límite fallara); el
  límite en memoria (con varios procesos se multiplica, como el login de libraauth antes de su tabla); guardar el código
  cifrado para volver a mostrarlo (con el hash alcanza: se pide otro).
- Consecuencias: los productos montan `create_canje_router` **sin guarda** con su `ip_del_request`, sacan `offline` de su
  catálogo de adicionales, de la siembra de `modulos` y de `medir`, y dejan de pasar `guarda_offline_de_libracore`. La
  pantalla del kit (libra-ui ADR-085) muestra el código; las webs dejan de decir «se contrata por sucursal».

## ADR-015 — La versión de escritorio: una app propia por producto (Tauri 2) en la PC principal

- Estado: aceptada (parte A; la parte B, los puestos secundarios que encuentran la PC principal, es ADR-017)
- Fecha: 2026-10-20
- Contexto: decisiones del humano del 2026-10-20. Comercialmente LibraEdge es la **«versión de escritorio»** de cada
  producto. Hasta ahora el acceso directo abría `http://localhost:<puerto>/` en Edge `--app` (Windows) o en Chrome,
  Chromium o Firefox (Ubuntu) (ADR-013), con lo que eso trae: la ventana es del navegador (su error si el servicio se
  reinicia, su perfil, sus pestañas para los enlaces externos), en Ubuntu sin modo aplicación salvo que haya Chrome, y la
  vinculación (ADR-014) sólo en el instalador o en una terminal. Se pidió: **una app por producto** («ContaLibra»,
  «VentaLibra», «RestoLibra»), con un solo código fuente en Tauri 2, sin firma de código por ahora, dentro de los
  instaladores existentes, con ventana propia, vinculación desde la app, reconexión sola, pantalla completa opcional y la
  sesión conservada.
- Qué ya existe (se miró antes de diseñar):
  - ADR-010 y ADR-012: los instaladores (`nodo.iss` + `instalar.ps1`; `.deb` + `instalar.sh`), con la definición del
    producto (`instalador/productos/<producto>.json`: nombre, `app_id`, puerto, ruta de salud) que también usa la app.
  - ADR-013: el acceso directo «<Producto>» (`preparar_nodo.ps1`; `libraedge-nodo-<producto>.desktop` y su favorito en
    el dock, `dock.sh`), `abrir_producto.sh` (Chrome/Chromium/Edge `--app`, si no `xdg-open`), «Abrir <Producto>» de las
    dos bandejas (Windows abre el `.lnk` del menú Inicio; Linux, `abrir_producto.sh`), los `.ico` por producto
    (`armar_icono.py`) y las marcas SVG (`instalador/marca`, copias de libra-ui). El punto 8 decía «esperar a Tauri».
  - ADR-014: el canje del código (`libraedge_instalador.py canjear`, que lee el código de `LIBRAEDGE_CODIGO` o de
    `--archivo-codigo`, nunca de un argumento), y los configuradores que ya lo usan como root/Administrador
    (`instalar.sh --codigo/--direccion`, `instalar.ps1 -Direccion` con el código en el entorno).
  - En la familia no había app de escritorio: se buscó `tauri`, `electron`, `webview`, `pkexec`, `polkit` y `runas` en
    libraedge, libracore, libra-ui y los productos; el único lanzador era `abrir_producto.sh`.
- Decisión:
  1. **Un solo código, un producto por compilación.** La app vive en `escritorio/` (crate `libraedge-escritorio`, Tauri
     2.12). `build.rs` toma `LIBRAEDGE_PRODUCTO`, lee **la misma definición del instalador** (nombre, puerto, salud), la
     marca (`instalador/marca/<producto>.svg`, para las pantallas propias) y los íconos (`instalador/iconos/<producto>.ico`
     y `.png`, el PNG de 256 px es la entrada PNG del mismo `.ico`), y le pasa a Tauri nombre, versión, iconos e
     identificador `ar.com.librasuite.<producto>` por `TAURI_CONFIG`. `escritorio/armar.py` compila los tres en el mismo
     `target/` y verifica con `--version` que cada binario sea su producto. Binarios: Windows
     `{app}\escritorio\<Producto>.exe` (el ícono del recurso es el del producto: UAC y el Explorador lo muestran);
     Ubuntu `/opt/libraedge-nodo/escritorio/libraedge-nodo-<producto>`, **el mismo nombre que el `.desktop`**: en
     Wayland el `app_id` de GTK es el nombre del programa y en X11 el `WM_CLASS` (más `StartupWMClass`), así GNOME
     agrupa la ventana con el favorito del dock. El puerto y la salud se releen al arrancar de
     `<raíz>/instalador/producto.json` (la raíz es la carpeta de arriba del ejecutable en los dos sistemas).
  2. **Ventana propia** (`principal`, maximizada la primera vez) que carga `http://localhost:<puerto>/` (`localhost`, la
     misma sesión que ADR-013); recuerda tamaño, posición y maximizado (`tauri-plugin-window-state`, sólo la principal);
     instancia única (`tauri-plugin-single-instance`: el acceso directo con la app abierta la trae al frente); Ctrl +/-
     para el zoom. El webview guarda su perfil por usuario y producto (WebView2: `%LOCALAPPDATA%\<identificador>`;
     WebKitGTK: `~/.local/share/<identificador>`, con cookies persistentes en disco).
  3. **Reconexión sola.** Un hilo pide `GET <salud>` a `127.0.0.1:<puerto>` (HTTP/1.0 a mano, plazo de 3 s) cada 3 s
     con el producto en pantalla y cada 1,5 s en las pantallas propias. Dos fallos seguidos (~6 s: un corte de un
     instante no saca al usuario de donde está) muestran la pantalla propia «Conectando con <Producto>…» (a los 45 s
     agrega qué hacer); cuando responde, vuelve a la URL donde estaba. Nunca se ve el error del navegador.
  4. **Vincular desde la app.** Si el producto no responde y el nodo no está configurado (no existe `nodo.env`: Linux
     `/etc/libraedge-nodo/nodo.env`, Windows `<raíz>\nodo.env`; la app sólo mira si está, no lo lee; si no puede saberlo
     asume que sí), muestra «Vincular con el sistema» (dirección + código, con la misma validación que el `.iss`: el
     alfabeto de ADR-014 y `[a-z0-9.:/-_]` para la dirección). Configurar el nodo es de root/Administrador y la app corre
     como el usuario: **no se agrega un servicio ni un canal nuevo; se reusa el configurador del instalador, lanzado con
     la elevación del sistema**, que pide la clave de un administrador:
     - **Ubuntu:** `pkexec /opt/libraedge-nodo/instalador/instalar.sh --direccion D --codigo-entrada`, con el código
       por la **entrada estándar** (opción nueva de `instalar.sh`; pkexec la pasa tal cual y limpia el entorno). Una
       acción de polkit propia (`ar.com.librasuite.libraedge-nodo.vincular`, `exec.path` = ese script) da el mensaje en
       castellano y pide `auth_admin_keep` (en una sesión activa, `auth_admin` en las demás). `instalar.sh` anota
       `PKEXEC_UID`. 126/127 de pkexec se traducen a «No se autorizó»; otro error muestra las últimas líneas del
       configurador (que no imprime secretos).
     - **Windows:** la app se relanza a sí misma con el verbo `runas` (`ShellExecuteExW`; UAC muestra el nombre y el
       ícono del producto) y `--vincular-elevado --direccion D --archivo-codigo P`; esa copia elevada no abre ventana:
       corre `powershell.exe` (de `GetSystemDirectoryW`) con `instalar.ps1 -Direccion D -ArchivoCodigo P`, sin consola, y
       sale con su código. UAC no le pasa a la copia elevada ni el entorno ni la entrada, así que **el código viaja en un
       archivo nuevo de la carpeta temporal del usuario** (su perfil: el usuario, SYSTEM y Administradores, que es
       quien lo lee; `create_new`), que `instalar.ps1` le pasa al ayudante (`--archivo-codigo`) y borra apenas canjea, y
       la app borra igual al terminar. Una cancelación de UAC (1223) es «No se autorizó»; un fallo muestra la cola de
       `logs\instalacion.log` (legible por los usuarios, sin secretos). Al terminar bien abre la bandeja (el `.lnk` del
       inicio común), que en Linux abre `instalar.sh`.
     - Por qué así y no un servicio que acepte pedidos por un canal local autenticado: un servicio privilegiado escuchando
       todo el tiempo es superficie nueva (autenticar al cliente, limitar qué puede pedir) para algo que se hace una vez
       por PC; la elevación del sistema autentica a un administrador, que es exactamente a quien hay que pedirle permiso
       para configurar un servicio, y el configurador es el mismo que ya probó el CI con el instalador. Quien lance
       `--vincular-elevado` o `pkexec instalar.sh` a mano igual necesita la clave de un administrador: no hay escalada.
     - **El código nunca va en una línea de comandos ni en un log**: por la entrada (pkexec) o por un archivo del perfil
       (UAC); la dirección sí (no es secreta). `--vincular --direccion D` (el código por la entrada o `LIBRAEDGE_CODIGO`)
       corre la misma función sin ventana: la usa el CI.
  5. **Pantalla completa opcional:** F11 la alterna en cualquier página, la del producto incluida, y se guarda en
     `preferencias.json` (carpeta de configuración del usuario); `--pantalla-completa` / `--ventana` la fijan desde el
     acceso directo (puestos dedicados, README). F11 lo escucha un script que la app inyecta en todas las páginas y llama
     al comando `alternar_pantalla_completa`. **La página del producto sólo puede llamar a ese comando**: su capacidad
     remota (`http://localhost:<puerto>/*` y `127.0.0.1`) se arma al arrancar con ese único permiso; `estado` y
     `vincular` son sólo de las pantallas propias (capacidad local). Sin barra de menú: no le suma nada a la ventana del
     producto.
  6. **La sesión se conserva entre aperturas.** La cookie de sesión de libraauth no tiene vencimiento (cookie «de
     sesión»: `session_auth.py` no pone `max_age`), así que el navegador embebido la descarta al cerrarse y había que
     volver a entrar cada vez. La app guarda las cookies de sesión de `http://localhost:<puerto>/` cada 20 s mientras
     el producto está en pantalla (`sesion.json`, carpeta de datos del usuario, 0600 en Linux) y las repone antes de la
     primera carga, con el mismo dominio (cookie del host, no de dominio). El producto sigue mandando: la sesión vence
     igual por inactividad (8 h) y cerrar sesión la borra (el archivo también). Las cookies con vencimiento y el
     almacenamiento local ya los guarda el webview.
  7. **Los enlaces externos van al navegador del sistema** (`tauri-plugin-opener`): toda navegación que no sea el
     producto local ni las pantallas propias (ARCA, Mercado Pago, ayudas, `mailto:`, `whatsapp:`) se cancela y se abre
     afuera; `file:` y `javascript:` no se abren. `target=_blank`/`window.open` del producto abren otra ventana de la
     app con la misma sesión (un comprobante, un reporte); lo externo, el navegador. Las descargas van a la carpeta
     Descargas y se muestran al terminar.
  8. **El navegador queda de respaldo.** Si la ventana no se puede crear (falta WebView2 o WebKitGTK), la app abre la
     copia local en el navegador (Edge `--app`; en Ubuntu `abrir_producto.sh --navegador`). Windows: el acceso directo
     «<Producto>» apunta a la app sólo si está el runtime de WebView2 (`Test-WebView2`); si no, a Edge `--app` como
     antes; y el menú Inicio suma «<Producto> (navegador)». Ubuntu: la acción «Abrir en el navegador» del `.desktop`
     (clic derecho en el dock), y `abrir_producto.sh` sin `--navegador` abre la app si está (la bandeja). Linux: la app
     apaga el renderizador DMA-BUF de WebKitGTK (`WEBKIT_DISABLE_DMABUF_RENDERER=1`, si no está puesta), que deja la
     ventana en blanco en máquinas virtuales y con algunos drivers.
  9. **En los instaladores.** Windows: el `.iss` copia `paquete\escritorio\<producto>.exe` como
     `{app}\escritorio\<Producto>.exe` (`skipifsourcedoesntexist`: sin la app compila igual y el acceso directo es Edge),
     el asistente ofrece «Abrir <Producto>» al terminar, una actualización cierra la app de cualquier sesión antes de
     copiar (como al Python de la bandeja) y desinstalar la cierra (`taskkill`) y la borra con el «(navegador)». Los
     accesos directos pasan a `Crear-AccesosDelProducto` (`comun.ps1`). **Instalar sin vincular**: en el asistente,
     sin código ni archivo se pregunta; en silencio, `/SINVINCULAR`; `instalar.ps1 -SinVincular` sólo deja los accesos
     directos (no toca servicios ni la base) y la app pide la vinculación. Ubuntu: `armar_deb.sh --escritorio DIR` mete
     el binario y la acción de polkit; el `.desktop` apunta a la app (`Exec`, `StartupWMClass`) y el favorito del dock
     es el mismo `.desktop`; `Depends` suma `libwebkit2gtk-4.1-0, libgtk-3-0` (en 24.04 y 26.04 lo provee
     `libgtk-3-0t64`) y `Recommends` `pkexec | policykit-1`. Se compila en `ubuntu-22.04` contra WebKitGTK 4.1 y glibc
     2.35, y el mismo binario anda en 24.04 y 26.04 (mismos sonames). Sin `--escritorio` el `.deb` sale como antes.
  10. **Firma de código: preparada, apagada.** `armar.py` firma cada `.exe` si existe `LIBRAEDGE_FIRMA_CMD` (un comando
      con `{archivo}`, por ejemplo `signtool sign ... {archivo}`), que el CI toma del secreto del mismo nombre; el
      `.iss` firma instalador y desinstalador con `ISCC /DFirmar "/Sfirma=..."`. Agregarla es cargar el certificado y
      el secreto, y pasar `/DFirmar` en el paso de ISCC. Mientras tanto, SmartScreen y UAC muestran «Editor
      desconocido».
  11. **CI** (`publicar-instalador.yml`): el job `escritorio` compila los tres en `windows-latest` y `ubuntu-22.04`
      (con `Swatinem/rust-cache`) y corre `cargo test` (navegación, validación del código y la dirección, salud,
      preferencias y sesión); los `.exe` van al paquete de Inno y los binarios al `.deb`. En Ubuntu 22.04/24.04/26.04,
      con el `.deb` instalado sin recomendadas: las bibliotecas resuelven (`ldd`), sin vincular la app muestra
      «vincular» bajo `xvfb-run` y su ventana tiene el `WM_CLASS` del `.desktop` (`xdotool`); configurado, carga el
      producto (el informe de la app y el `GET /` en el journal del producto), con el producto parado muestra la espera
      y vuelve sola al arrancarlo; vincula por pkexec contra el central de prueba (secreto verificado, código fuera de
      los logs); el `.desktop` apunta a la app y `remove` la saca con su acción de polkit. En Windows: `/SINVINCULAR`
      deja el acceso directo en la app y la app muestra «vincular»; configurado, un arranque cronometrado con el
      proceso vivo, la carga en el informe y el `GET /` en el log del producto; espera y vuelta tras parar el servicio;
      vincular desde la app por UAC (en el runner no pide nada) con el secreto verificado y sin el archivo del código;
      desinstalar saca la app y el «(navegador)». `LIBRAEDGE_ESCRITORIO_INFORME` es el archivo donde la app anota lo
      que muestra (sin secretos), sólo para esto.
- Descartadas: un servicio/ayudante privilegiado permanente con un canal local (ver punto 4); poner el código en la
  línea de comandos de pkexec o de UAC (queda en `/proc` y en el registro de auditoría); una barra de menú (sólo para
  F11); atajo global de teclado (tomaría F11 en toda la PC); compilar la app en cada versión de Ubuntu (el binario de
  22.04 sirve en las tres); el bundler de Tauri (los instaladores ya existen y son los que se prueban); cambiar libraauth
  para que la cookie tenga `max_age` (es del motor y afecta a la web: queda como alternativa para el humano).
- Consecuencias: la app sale con cada tag dentro de los instaladores; no hay actualización propia (se actualiza con el
  instalador). El tamaño de los instaladores sube unos MB (README). Lo que el CI no ve —cómo se ve la ventana, el pedido
  de clave de pkexec/UAC con un usuario real, F11, la sesión al cerrar y abrir, los enlaces externos, el dock y la
  vinculación contra un central de verdad— se prueba en las VMs (`PROBAR_EN_VM.md`, V16 a V18; README, sección Linux).
  Lo que pide la parte B: la app de un puesto secundario no tiene nodo (no hay `nodo.env` ni producto local), así que
  necesitará otra fuente para la URL (la PC principal encontrada en la red) y no debe ofrecer «Vincular»; la pantalla de
  espera y la reconexión sirven tal cual.

**Actualizar sin vincular (2026-10-21):** actualizar con `/SINVINCULAR` (o sin código ni archivo en el asistente) un
nodo que **ya estaba vinculado** dejaba el local sin sistema hasta reiniciar: `PrepareToInstall` para los servicios
para copiar y `instalar.ps1 -SinVincular` sólo hacía los accesos directos. Ahora, si está el `nodo.env`,
`Actualizar-NodoVinculado` (`comun.ps1`) arranca PostgreSQL, corre las migraciones de la definición con ese entorno
(el producto nuevo puede traerlas) y vuelve a arrancar producto, nodo y descubrimiento; si una migración falla, el
producto queda parado y el instalador sale con 20. El CI lo prueba (actualizar con `/SINVINCULAR` un nodo vinculado:
servicios corriendo y `verificar_nodo.ps1` en 0).

## ADR-016 — La cookie de sesión del producto sale sin `Secure` en el nodo (`LIBRAAUTH_COOKIE_SECURE=0`)

- Estado: aceptada (propuesta; entra en vigor cuando los productos suban el pin de libraauth a la versión del ADR-026)
- Fecha: 2026-10-20
- Contexto: en una VM Ubuntu 26.04 con el nodo de ContaLibra (v0.15.0), la app de escritorio (Tauri sobre WebKitGTK)
  no podía entrar: `POST /api/login` daba 200, la cookie no se guardaba y el pedido siguiente daba 401. El nodo sirve el
  producto por **http** (`http://localhost:<puerto>` en la PC, `http://<IP>:<puerto>` desde los otros puestos, ADR-013) y
  libraauth ponía la cookie de sesión con `secure=True` fijo. WebKitGTK no hace la excepción de `localhost` para cookies
  `Secure` por http; WebView2 (Windows) y Chrome sí, pero **ningún** navegador la guarda desde una IP, así que los puestos
  secundarias tampoco podían entrar en ningún sistema operativo.
- Qué ya existe (se miró antes de diseñar):
  - libraauth: la cookie de `SessionAuth.create_session_cookie` (login, login de la demo y renovación deslizante) es la
    única que pone el producto en el nodo; el captcha (ALTCHA) no usa cookies, no hay CSRF ni «recordarme» con cookie
    propia, y la lectura de la sesión no mira el esquema. `AdminAuth` (backoffice) tiene su propia cookie `Secure`, pero
    no se sirve en nodos. En los tres productos del nodo no hay otros `set_cookie` (se buscó en ContaLibra, RestoLibra y
    VentaLibra); el portal de jugadores de LibraClub tiene una cookie `Secure` propia, pero LibraClub no tiene nodo.
  - libracore pone `Strict-Transport-Security`, que por http los navegadores ignoran (RFC 6797 §8.1) y no aplica a IPs.
  - `armar_entorno` (este repo) ya fija variables iguales en todos los nodos (`TZ`) y `revisar_entorno` exige las que
    arma; una definición de producto no puede pisarlas (`variables` lo rechaza).
  - La actualización por `apt` (Linux) y la del instalador de Windows sin vincular sobre un nodo ya vinculado no vuelven
    a armar el `nodo.env` (ADR-012, ADR-015).
- Decisión:
  1. **El arreglo de fondo está en libraauth** (ADR-026 de libraauth, propuesta v0.53.0): `LIBRAAUTH_COOKIE_SECURE`,
     default `Secure`; sólo `0`, `false`, `no` u `off` la apagan, y cualquier otro valor la deja puesta. En la nube no
     cambia nada.
  2. **El nodo la apaga.** `VARIABLES_FIJAS_DEL_NODO = {"LIBRAAUTH_COOKIE_SECURE": "0"}` en `libraedge/instalador.py`:
     `armar_entorno` la escribe en el `nodo.env` de Windows y de Linux (es la misma función), `revisar_entorno` la exige
     (`verificar_nodo` la nombra si falta), la carga no la puede traer y la definición del producto no la puede pisar.
  3. **Los nodos que se actualizan sin carga la reciben igual**: `libraedge_instalador.py completar-entorno --entorno
     nodo.env` agrega las variables fijas que falten al final del archivo, sin tocar lo demás (ni el orden, ni los
     secretos, ni la ACL: escribe sobre el archivo existente). Lo llaman `preparar_nodo.sh` sin carga (actualización por
     `apt`) e `instalar.ps1 -SinVincular` si ya hay `nodo.env`. Una variable que ya está, con cualquier valor, se respeta.
  4. **Un producto con libraauth anterior a v0.53.0 ignora la variable** y queda como hoy (login roto por http en
     WebKitGTK y desde los otros puestos) hasta que suba el pin: no hace falta coordinar las dos versiones.
  5. **La app de escritorio no cambia**: `sesion.json` guarda la marca `Secure` tal como vino (`segura`) y la repone
     igual; una cookie sin `Secure` vuelve sin `Secure` (test nuevo en `preferencias.rs`). Ni la app ni el producto
     exigen https para leer la sesión (`GET /api/me`, `/auth/me`).
  6. **CI** (`publicar-instalador.yml`): en Windows (después de V6) y en Ubuntu 22.04/24.04/26.04 (después de
     `--sin-central`), un **login real por http contra la IP de la red local** (sin la excepción de `localhost`) con el
     admin local del nodo: el `nodo.env` tiene `LIBRAAUTH_COOKIE_SECURE=0`; `tests/login_por_http.py`, con el Python del
     nodo, lee la versión de libraauth instalada y arma el cuerpo del login (la clave del admin sale del `nodo.env` —por
     la entrada en Linux, que es de root— y el captcha ALTCHA se resuelve ahí; nada pasa por una línea de comandos ni se
     imprime, y el archivo se borra apenas se usa); `curl -c` hace `POST <prefijo>/login` (`/api` en ContaLibra y
     RestoLibra, `/auth` en VentaLibra: se detecta por la ruta del captcha) y `curl -b` el `GET <prefijo>/me`. Con
     libraauth >= 0.53.0 se exige 200 en el login, ningún `Set-Cookie` con `Secure` y 200 con el admin en `/me`; con una
     versión anterior se exige el 200 del login y se deja un *warning* con lo que dio. En Linux la clave del admin se
     suma a «Ningún secreto en los logs».
- Descartadas: servir el nodo por https con un certificado local (otra autoridad que instalar en cada puesto, y no
  resuelve el problema hoy); que libraauth apague `Secure` solo cuando el pedido llega por http (dejaría a una instancia
  de la nube mal configurada detrás del proxy emitiendo cookies sin `Secure` sin que nadie lo decida); poner la variable
  en la definición de cada producto (son tres copias de lo mismo y `variables` no puede fijar lo que arma el instalador).
- Consecuencias: en el nodo la cookie viaja en claro por la red del local, igual que ya viajaba la clave en el
  `POST /login` por http; el riesgo es el de una LAN, no uno nuevo. Para que el arreglo llegue a una PC hacen falta las
  dos cosas: el instalador de esta versión (o `completar-entorno` por una actualización) y el producto empaquetado desde
  `main` con libraauth >= 0.53.0. Lo que el CI no ve —la app de escritorio entrando de verdad en WebKitGTK y otro puesto
  entrando por la IP desde un navegador— va a `PROBAR_EN_VM.md`.

## ADR-017 — Los otros puestos del local: la app de puesto y el descubrimiento de la PC principal

- Estado: aceptada (es la parte B de ADR-015)
- Fecha: 2026-10-20
- Contexto: decisión del humano del 2026-10-20: **una app por producto, sin firma por ahora**, también para las otras
  PC del local (las «puestos secundarios»). Hasta ahora esas puestos entraban por `http://<IP-de-la-PC-principal>:<puerto>`
  en un navegador, con la IP copiada de la bandeja o del cartel final: si la PC principal cambiaba de IP (DHCP), el puesto
  quedaba en el error del navegador y alguien tenía que buscar la dirección nueva.
- Qué ya existe (se miró antes de diseñar):
  - ADR-013: la dirección para los otros puestos sale de un solo lugar, `libraedge_instalador.py acceso` (la IPv4 privada
    de la ruta por defecto, `ip_de_la_lan`/`es_ip_de_la_lan`), y la muestran el cartel final de los dos instaladores y
    «Otros puestos: http://<IP>:<puerto> (copiar)» de las dos bandejas. La regla de firewall del producto (Windows
    `New-NetFirewallRule ... -Profile Private, Domain` en `preparar_nodo.ps1`; Linux `ufw allow proto tcp from <red>`
    para 10/8, 172.16/12 y 192.168/16 si ufw está activo, en `preparar_nodo.sh`; las borran el `.iss` y el `postrm`
    de `purge`).
  - ADR-015: la app (`escritorio/`): ventana propia, reconexión con «Conectando con <Producto>…», F11 con una capacidad
    remota sólo para `localhost`/`127.0.0.1` en el puerto, sesión recordada (`preferencias.rs`, cookies del host),
    «Vincular» si no hay `nodo.env`; el clasificador de navegación (`navegacion.rs`) mandaba cualquier otra URL al
    navegador del sistema. La nota «Para la parte B» pedía otra fuente para la URL y no ofrecer «Vincular».
  - ADR-012: `armar_deb.sh` y el favorito del dock (`dock.sh`, override de `org.gnome.shell favorite-apps`); ADR-010:
    `nodo.iss` con la tabla nombre/AppId comparada con las definiciones; NSSM para los servicios de Windows.
  - En la familia no había descubrimiento en la red: se buscó `broadcast`, `SO_BROADCAST`, `mdns`, `zeroconf`,
    `avahi`, `dns-sd`, `bonjour`, `ssdp` y `47823` en libraedge, libracore, libra-ui y los productos.
- Decisión:
  1. **Descubrimiento por broadcast UDP en el puerto fijo 47823**, con biblioteca estándar de los dos lados. El nodo
     contesta con `libraedge/descubrimiento.py` (copiado como `libraedge_descubrimiento.py`, como el ayudante: llega con
     el instalador y no depende del pin de LibraEdge del producto); la app pregunta con `escritorio/src/descubrimiento.rs`.
     Pedido `{"libraedge": "descubrir/1", "producto": P}` con relleno hasta 512 bytes; respuesta `{"libraedge": "nodo/1",
     producto, nombre, puerto, nodo, sucursal, negocio}`. El puesto manda a `255.255.255.255` y al broadcast de su red /24
     (en Windows el primero sale por una sola interfaz) y junta respuestas 2 s.
     - **Por qué no mDNS:** en Windows el puerto 5353 ya lo tiene el servicio DNS Cache del sistema y en Ubuntu Avahi; un
       responder propio tendría que compartir el puerto (`SO_REUSEADDR`, que en Windows deja robarlo) o registrarse en
       cada sistema con su API (Bonjour/`dnssd.dll`, D-Bus de Avahi), con dependencias y dos implementaciones. Con
       broadcast el que contesta son ~250 líneas de biblioteca estándar y el puesto no necesita ningún crate nuevo. Lo que
       se pierde (cruzar subredes, un router que filtre broadcast) tampoco lo resolvería mDNS, que es de enlace local:
       para eso queda escribir la IP.
  2. **Seguridad del que contesta.** No ve ningún secreto: lee **sólo** `descubrimiento.json` (producto, nombre, puerto,
     identificador del nodo, sucursal y negocio; 0644), que escribe el ayudante al configurar (`libraedge_instalador.py
     descubrimiento`, desde la carga o, al actualizar sin carga, del `LIBRAEDGE_NODE_ID` del `nodo.env`, del
     `empresa_nombre` del `config.json` y del archivo anterior; textos libres cortados a 80). Corre sin privilegios: en
     Ubuntu `libraedge-descubrimiento.service` con `DynamicUser=yes` (ni root ni el usuario `libraedge` del producto, que
     lee `DATA_DIR`), `ProtectSystem=strict`, sin capacidades, sólo `AF_INET`/`AF_UNIX`; en Windows el servicio NSSM
     `LibraEdgeDescubrimiento` como `NT AUTHORITY\LocalService` (no lee `nodo.env`, cuya ACL es SYSTEM y
     Administradores), sin log propio. **No sirve para amplificar:** un pedido de menos de 512 bytes no se contesta y la
     respuesta nunca pasa de 512 (se acortan negocio y sucursal). Sólo contesta a IPv4 privadas, de enlace local o
     loopback, a pedidos de su producto, y con tope (5 respuestas por origen cada 10 s, 50 por segundo en total). No
     anota cada pedido. Firewall como el del producto: `LibraEdge descubrimiento` UDP 47823 en Privado y Dominio
     (Windows) y `ufw allow proto udp ... port 47823` para las redes privadas si ufw está activo; se borran al
     desinstalar (`.iss`) y en el `purge`.
  3. **Lo que el puesto le cree a una respuesta:** la IP es **la de quien contestó**, nunca un dato del mensaje, y sólo
     IPv4 privada, de enlace local o loopback; el puerto sí viene en la respuesta. Una respuesta falsa sólo puede venir de
     alguien en la misma red, que con `http` en la LAN ya podía interceptar el tráfico (riesgo aceptado, el mismo de
     ADR-013); cuando hay más de una PC principal se muestra la sucursal y el negocio para elegir, y la elegida se
     reconoce después **por su identificador de nodo**.
  4. **Un solo binario.** La app decide el modo al arrancar: si al lado está la definición instalada
     (`<raíz>/instalador/producto.json`, que sólo pone el instalador del nodo) es la de la **PC principal** (modo nodo,
     todo como ADR-015); si no, es una **puesto** (modo puesto). Mismo código y misma compilación (`armar.py`); el instalador
     de puesto pone el mismo archivo en otra carpeta. Se descartó un binario por modo (una feature de Cargo): duplicaba la
     compilación de los tres productos en los dos sistemas y el riesgo de que diverjan, para ahorrar unas líneas. En modo
     puesto la app usa **otro identificador**, `ar.com.librasuite.<producto>.puesto` (`Context::config_mut`): otra sesión
     del navegador embebido, otras preferencias y otra instancia única, así las dos pueden convivir (en el CI conviven).
     `--version` dice el modo.
  5. **La app en modo puesto.** La primera vez busca («Buscando la PC principal…»): si hay una la usa, si hay varias
     muestra la lista (sucursal · negocio, y la dirección) y si no hay ninguna pide la IP a mano (con el texto de dónde la
     muestra la PC principal: la bandeja y el cartel final) y sigue buscando sola cada ~10 s. La IP escrita se valida
     (IPv4 de la red del local, opcional `:puerto` o `http://`), se le pregunta por el descubrimiento y, si no contesta,
     se prueba la salud del producto en ese puerto (o el de la definición). La elegida se guarda en `principal.json`
     (carpeta de configuración del usuario; IP, puerto, nodo, sucursal y negocio). **Si deja de responder**, la misma
     espera de ADR-015 («Esperando a la PC principal del local (…)») y cada ~6 s vuelve a buscar: si el mismo nodo
     contesta desde otra IP, la toma y vuelve sola; además, «Buscar otra PC principal». El origen del producto pasa a ser
     `http://<IP>:<puerto>`: el clasificador lo trata como el producto (no va al navegador del sistema) y `localhost` en
     un puesto ya no lo es; F11 se habilita con una capacidad remota por origen (al arrancar y al elegir otra PC); la
     sesión se guarda y se repone con el host de la PC principal (si cambia de IP, la misma sesión). **No ofrece
     «Vincular»** (el comando también lo rechaza) ni lo intenta por `--vincular`.
  6. **El instalador de puesto**, por producto, sólo con la app (sin nodo, PostgreSQL ni Python), con nombre fijo en el
     Release: **`libraedge-puesto-<producto>.exe`** y **`libraedge-puesto-<producto>_amd64.deb`** (y los `.deb` con versión).
     El nombre sigue al de `libraedge-nodo-<producto>` (se reconocen como pareja en el Release y en `apt`) y «puesto» es la
     palabra de ADR-013 y de la bandeja («Otros puestos»); para el usuario la app se llama igual que en la PC principal
     («ContaLibra»), y en «Agregar o quitar programas» «ContaLibra (puesto)».
     - **Windows: un `.iss` chico** (`instalador/puesto.iss`), no el bundler de Tauri: Inno ya está en el CI, con la misma
       tabla nombre/AppId (AppId propio por producto, comparado con las definiciones), los íconos de ADR-013 y los textos
       en castellano; el bundler pedía instalar la CLI de Tauri (o WiX) y armar una configuración por producto para un
       solo archivo. Instala `C:\Program Files\<Producto> (puesto)\<Producto>.exe` con «<Producto>» en el escritorio
       público y el menú Inicio, se desinstala desde «Agregar o quitar programas» (cierra la app de esa carpeta). Avisa
       (sin frenar en silencio) si la PC ya tiene el nodo del producto o si falta WebView2.
     - **Ubuntu: `instalador/linux/puesto/armar_deb.sh`** → `/opt/libraedge-puesto/<producto>/libraedge-puesto-<producto>` (el
       nombre del `.desktop`: WM_CLASS/app_id para el dock), `libraedge-puesto-<producto>.desktop`, `Depends:
       libwebkit2gtk-4.1-0, libgtk-3-0`. El favorito del dock con `dock.sh` **sin Python** (el valor por defecto con
       `gsettings` en un entorno vacío, como ADR-013, más los `.desktop` de puesto y, si estuviera, el del nodo, en
       `91_libraedge-puesto.gschema.override`); el `postrm` lo rehace o lo borra con la última puesto.
  7. **La pantalla del central** (libra-ui `SucursalSinInternet`, ADR-086 del kit): la tarjeta del instalador suma «Para
     las otras PC del local» con las descargas de puesto, armadas al lado del `.exe` del nodo como las otras (el backend no
     manda nada nuevo).
  8. **CI** (`publicar-instalador.yml`): el job `deb` arma también el `.deb` de puesto (shellcheck de sus scripts) y
     `compilar` el `.exe` (en `salida-puesto`); `publicar` los sube y los suma a `SHA256SUMS.txt`. En Ubuntu
     22.04/24.04/26.04, con el nodo instalado y configurado en el mismo runner: el servicio activo con un usuario
     efímero, `descubrimiento.json` 0644 sin el secreto, un pedido corto o de otro producto sin respuesta; el puesto
     instalada con `--no-install-recommends` (bibliotecas resueltas, `--version` en modo puesto, `.desktop` válido, dock con
     el nodo y el puesto), `--buscar` encuentra el nodo **por broadcast con la IP del runner**, y bajo `xvfb-run` la app
     busca, encuentra y carga `http://<IP del runner>:<puerto>/` (el `GET /` desde esa IP en el journal, nunca
     `localhost`, nunca «vincular»); con una IP guardada que no responde espera y vuelve a encontrar el mismo nodo; sin
     nadie que conteste pide la IP y, cuando el servicio vuelve, lo encuentra solo; `remove` saca la app, el `.desktop` y
     el override. En Windows: el servicio como LocalService, la regla UDP, el `.json` sin secretos; el puesto instalado en
     silencio con sus accesos directos, `--version` en modo puesto, `--buscar` (si el broadcast no vuelve en el runner, se
     avisa y se prueba la IP directa), la app carga el producto por la IP del runner con el `GET /` en el log del
     producto, y desinstalar la saca. `verificar_nodo` (los dos) pregunta como un puesto a `127.0.0.1`.
- Descartadas: mDNS (punto 1); que la respuesta traiga la IP (la decide quien contesta); un binario por modo (punto 4);
  el bundler de Tauri para el puesto de Windows (punto 6); que el puesto busque también en `127.0.0.1` (en la PC principal
  se entra por el acceso directo del nodo, no por el puesto); ofrecer «Vincular» en el puesto.
- Consecuencias: lo que el CI no ve (una red de verdad con dos PC, DHCP que cambia la IP, el firewall de Windows con la
  red en «Pública», la lista con dos PC principales, cómo se ve) se prueba en las VMs (`PROBAR_EN_VM.md`, V20 y V21).
  El responder llega a las PC principales que ya tienen el nodo cuando se actualicen con el instalador nuevo (un nodo
  anterior no contesta: el puesto pide la IP). Si la red del local filtra broadcast o el puesto está en otra subred, se
  escribe la IP; el puesto la recuerda y, si cambia, vuelve a pedirla.

## ADR-018 — La salud de los nodos para uptime-kuma: `GET /sync/v1/salud` con un token de monitoreo por instancia

- Estado: aceptada (Fase 2 de LibraEdge, decidida por el humano; sale en v0.17.0)
- Fecha: 2026-10-21
- Contexto: el monitoreo de nodos existía en dos formas y ninguna le sirve a uptime-kuma: `libraedge-central nodo vigilar`
  (un comando para el cron del VPS, que sale 1 si un nodo se calló) y el estado `al_dia`/`sin_noticias`/`nunca`/`baja` de la
  pantalla «Sucursal sin internet» (`GET /api/nodos-offline`, detrás de la sesión de administrador). uptime-kuma ya vigila
  el resto de la suite y sólo habla HTTP: su monitor «HTTP + keyword» da caído con un status fuera de 2xx o si la palabra
  clave no está en el cuerpo. Un nodo callado hace horas se veía igual que uno sano en cualquier lugar donde nadie corriera
  `vigilar` a mano.
- Qué ya existe (se miró antes de diseñar): `NodeRepository.nodos_para_vigilar` (los datos crudos: `activo`,
  `ultima_vez`, `minutos_sin_verse`, que alimenta el `last_seen_at` que anota la autenticación de push y pull);
  `pantalla_del_central._estado_del_nodo` (activo + minutos + umbral → `baja`/`nunca`/`al_dia`/`sin_noticias`) y su
  `UMBRAL_MINUTOS = 15`, el mismo default que `nodo vigilar --umbral`; y la lógica de `aprovisionamiento.vigilar` (se
  vigilan sólo los activos; sin nodos activos no hay nada que vigilar y sale 0). No se escribió ninguna lógica de estado
  nueva: las rutas llaman a `_estado_del_nodo` sobre lo que devuelve `nodos_para_vigilar`.
- Decisión (en `create_sync_router`, `libraedge/sync/api.py`):
  1. **Dos rutas**: `GET /sync/v1/salud` (resumen de los nodos **activos**) y `GET /sync/v1/salud/{node_id}`.
     - Un nodo: **200** si `al_dia`, **503** si `sin_noticias` o `nunca`, **404** si está dado de baja o no existe (el 404 no
       distingue los dos casos: no hay por qué decir cuál).
     - El resumen es el del peor nodo (`sin_noticias` > `nunca` > `al_dia`) y trae la lista de nodos. Sin nodos activos es
       `sin_nodos` con **200**, como `vigilar` (que sale 0): no hay nada que vigilar. Un monitor de «ningún nodo» no
       es esta ruta.
  2. **Cuerpo**: `{estado, palabra_clave, minutos_sin_verse, ultima_sincronizacion, umbral_minutos}` (más `node_id` y
     `sucursal_id` en el de un nodo, y `nodos_vigilados` y `nodos` en el resumen). `minutos_sin_verse` y
     `ultima_sincronizacion` son `null` para `nunca`: no se inventan. **Palabra clave estable**: `LIBRAEDGE_OK` sólo en las
     200 y `LIBRAEDGE_ALERTA` sólo en las 503. Son un contrato con el monitor y no se renombran; el monitor puede buscar
     una o que falte la otra. `Cache-Control: no-store`.
  3. **Autenticación por token de monitoreo de la instancia**, `LIBRAEDGE_TOKEN_MONITOREO`, por `Authorization: Bearer` o
     `?token=`, comparado con `hmac.compare_digest` en bytes (con `str` un carácter no ASCII levanta `TypeError`). No se
     usa el secreto de un nodo: ése es de la PC del local y abriría la salud de *todos* los demás. Un token por instancia
     y no por nodo: el monitor vigila la instancia.
     - **Sin la variable (o vacía) las dos rutas contestan 404** con el mismo cuerpo que da FastAPI a una ruta inexistente:
       están apagadas, no protegidas. Un 401 diría que hay un monitoreo y cuál es la puerta. Se lee del entorno **en cada
       pedido** (rotar o apagar es cambiar la variable y reiniciar). El 404 va antes que cualquier validación: un `?umbral=`
       inválido sobre una ruta apagada también da 404, no 422.
     - Token ausente o equivocado: 401 con `WWW-Authenticate: Bearer`. La cabecera manda sobre la query. El token nunca
       se loguea ni se devuelve.
     - `?token=` queda en los access logs del proxy y de uvicorn: está para el monitor que no pueda mandar cabeceras;
       uptime-kuma sí puede (cabeceras del monitor), y es lo que hay que preferir. Si se usa la query, el token es de sólo
       lectura de salud: lo peor que filtra es cuántos minutos hace que habló cada nodo.
  4. **Umbral por `?umbral=`** en minutos enteros de 1 a 10080 (una semana), con el default de `vigilar`
     (`UMBRAL_MINUTOS`, 15). Se parsea a mano después de autenticar; fuera de rango o no entero, 422. Depende del intervalo
     de cada instalación (el nodo cicla cada 60 s por defecto) y por eso lo elige quien configura el monitor.
  5. **Nunca expone secretos del nodo**: nada de `secret_hash`, de prefijo ni de la identidad más allá del `node_id` y la
     sucursal (los mismos que ya muestra la pantalla del central). Tampoco **cuenta como contacto**: mirar la salud no
     mueve `last_seen_at`, o el monitor mantendría vivo a un nodo muerto.
- Descartadas: reusar `GET /api/nodos-offline` (está detrás de la sesión y devuelve el código de vinculación pendiente
  y más de lo que un monitor necesita); un token por nodo (el monitor vigila la instancia, y N tokens para N monitores no
  agregan nada); devolver siempre 200 con el estado en el cuerpo (obliga al monitor a depender de la palabra clave y
  pierde la alerta por status); 401 sin la variable (ver 3); un endpoint de salud del *central* (eso lo hace el `/health`
  del producto; esto mira si los nodos hablan).
- Consecuencias: uptime-kuma se configura con un monitor «HTTP(s) - Keyword» por instancia sobre
  `https://<central>/sync/v1/salud`, cabecera `Authorization: Bearer <token>` y palabra clave `LIBRAEDGE_OK` (opcional: uno por
  nodo sobre `/salud/<node_id>` para alertar por sucursal). El token se genera por instancia
  (`openssl rand -hex 32`) y va al `.env` del servicio del producto como `LIBRAEDGE_TOKEN_MONITOREO`; hay que reiniciar para
  que lo tome. La ruta sólo existe si el producto monta `create_sync_router`; un producto con un `libraedge` anterior a
  v0.17 contesta 404 (el monitor sale caído, que es lo correcto). Un nodo recién registrado que todavía no se instaló está
  en `nunca` y da 503 en el resumen: o se instala o se da de baja.

## ADR-019 — La guarda del espejo: en el nodo, las tablas que vienen del central no las escribe el producto

- Estado: aceptada (Fase 2 de LibraEdge, decidida por el humano; sale en v0.17.0). Opt-in por tabla.
- Fecha: 2026-10-21
- Contexto: el reparto de autoridad tiene dos mitades. Una la hace cumplir `MirrorApplier`: sólo aplica las tablas de
  `LIBRAEDGE_TABLAS_ESPEJO`, así que el central no puede escribir una tabla de autoridad del nodo. La otra, que **el
  producto del nodo no escribe las tablas que espeja**, era una convención. Una escritura del POS sobre `productos` no falla
  ni se sube (el outbox sólo sube lo que el producto encola): vive en la base del nodo hasta que el próximo cambio del
  central la pisa, o para siempre si el central nunca la toca, y la diferencia aparece en un arqueo. El diseño pedía que fuera
  imposible, no desaconsejado.
- Qué ya existe (se miró antes de diseñar): el trigger del changelog (`db/changelog.py`, `instalar_trigger`/
  `desinstalar_trigger`/`tablas_publicadas`: PL/pgSQL, solo PostgreSQL, idempotente, converge con la lista); el patrón de
  declarar tablas en `Aprovisionamiento.tablas_de_referencia` y viajar por `registrar` → carga → `nodo.env`
  (`LIBRAEDGE_TABLAS_ESPEJO`) → `libraedge-nodo`; y `MirrorApplier`, el único escritor legítimo. La guarda sigue el mismo
  molde en cada uno.
- Decisión:
  1. **`instalar_guarda_de_espejo(conn, tablas)`** (`libraedge/db/guarda.py`) pone en cada tabla un trigger `BEFORE INSERT OR
     UPDATE OR DELETE ... FOR EACH ROW` (`<tabla>_libraedge_guarda`, función `libraedge_guarda_de_espejo()`) que levanta
     `solo_en_el_central: la tabla X se espeja del central y no se escribe en el nodo (INSERT rechazado)` con **SQLSTATE
     `LE001`**, salvo que `current_setting('libraedge.aplicando', true) = '1'`. Devuelve las tablas que quedaron guardadas.
     - **Idempotente y converge**: se corre en cada arranque; una tabla que sale de la lista pierde su guarda (si no, una
       tabla que el producto resuelve seguiría bloqueada hasta que alguien tocara el trigger a mano). Una tabla de la lista
       que no está en la base (una migración todavía no la creó) se saltea y no figura en el resultado: el nodo arranca y lo
       avisa por stderr.
     - **El error no es un `IntegrityError`.** La clase `LE` no es estándar y psycopg la levanta como `DatabaseError`
       pelado; un `except IntegrityError` del producto (para un duplicado, por ejemplo) no se la traga. Los productos no
       atrapan `DatabaseError`, así que sube como un 500 con un mensaje que dice qué pasó.
  2. **`MirrorApplier` abre la guarda con `SET LOCAL libraedge.aplicando = '1'`** (`marcar_aplicando`) al empezar **cada**
     `aplicar`, dentro de su transacción. En cada uno y no una vez: `PullWorker` commitea después de cada cambio (el cursor
     avanza cambio por cambio, `pull.py`) y el `SET LOCAL` muere con el commit; el test del ciclo completo cubre justo eso.
     Es un statement más por cambio, sin costo apreciable contra un PostgreSQL local. Sobre una tabla sin guarda no cambia
     nada.
  3. **Es opt-in por tabla**, a propósito: la lista de sólo lectura la pasan el producto o el motor y **no son todas las
     de referencia**. Las altas de clientes en el nodo todavía no están resueltas (`clientes` se espeja y el POS crea
     clientes en el nodo); guardarla rompería el POS hoy. Se guarda lo que ya está decidido que el nodo no toca (catálogo,
     precios, usuarios, según cada producto) y la lista crece tabla por tabla a medida que se resuelven. Sin declarar nada,
     v0.17 se comporta exactamente como v0.16.
  4. **La costura es `Aprovisionamiento.tablas_solo_lectura`** (nombres, vacío por defecto; tienen que estar en
     `tablas_de_referencia`, si no `ValueError`: una tabla que nadie espeja quedaría bloqueada para siempre). `registrar` la
     intersecta con lo que de verdad se espeja y la devuelve en `Alta.solo_lectura`; viaja en la carga como la variable
     **opcional** `LIBRAEDGE_TABLAS_SOLO_LECTURA` (`instalador.VARIABLES_OPCIONALES_DE_LA_CARGA`: se valida contra
     `LIBRAEDGE_TABLAS_ESPEJO`, y sólo viaja si no está vacía); y `libraedge-nodo` (`construir_nodo`) la instala **al
     arrancar**, después de las migraciones. Se instala en el arranque del nodo y no en `publicar` porque `publicar` corre en el
     **central**, cuya base es la misma del producto y donde el producto sí escribe: una guarda ahí lo dejaría sin trabajar.
     Arrancar sin la variable desinstala las guardas de una carga anterior.
  5. **SQLite: no-op.** El nodo real corre PostgreSQL; SQLite queda para tests y para LibraCommerce. No tiene variable de
     sesión que consultar, y emularla pide una tabla bandera que el aplicador prenda y apague en cada cambio, más código en
     el camino caliente del espejo para proteger un motor en el que el nodo no corre. Las tres funciones devuelven `()`/no
     hacen nada sobre SQLite y lo dicen en su docstring en vez de fingir que protegen; los tests de la guarda se saltean en
     `[sqlite]` diciendo por qué, y un test propio fija que es un no-op.
- Descartadas: guardar todas las tablas de referencia (rompe el POS, ver 3); `SECURITY DEFINER`/roles de PostgreSQL distintos
  para el espejo y el producto (el nodo corre con un solo usuario `postgres` embebido, y separar roles es una migración del
  instalador entera); validar en el producto (el punto es no depender de que cada camino de escritura se acuerde); `TRUNCATE`
  y `DDL` (no se guardan: las migraciones de esquema tienen que poder tocar la tabla).
- Consecuencias: **no es una frontera de seguridad**: quien tenga acceso a la base puede hacer `SET libraedge.aplicando = '1'`.
  Atrapa el error del programador, no a un atacante. **Una migración de datos sobre una tabla guardada falla en el nodo**: tiene
  que llamar a `libraedge.db.guarda.marcar_aplicando(conn)` antes de escribir (las de esquema no). El central que emite
  una carga con `LIBRAEDGE_TABLAS_SOLO_LECTURA` a un instalador anterior a v0.17 la ve rechazada («trae variables que no le
  corresponden»): hay que actualizar el instalador antes de que un producto declare la lista (sin declararla, nada cambia).
  Lo que el CI no ve: el arranque del servicio del nodo real con el producto encima (se prueba en las VMs con un producto
  que declare una tabla).

## ADR-020 — El outbox sube en orden causal: ante un fallo de transporte se corta la tanda

- Estado: aceptada (Fase 2 de LibraEdge, decidida por el humano; sale en v0.17.0)
- Fecha: 2026-10-21
- Contexto: `OutboxWorker.run_once` hacía `continue` cuando el `push` de una operación levantaba una excepción: la dejaba
  `retryable_error` y seguía con la siguiente. Una venta depende de lo que la precede en el nodo (el `cliente.alta` que crea
  al cliente, el `turno.abierto` que abre la caja); con la red intermitente la operación de la que depende falla, la venta sube
  antes que ella y el central la rechaza por una FK que se habría resuelto sola esperando, o peor, la acepta sin su
  dependencia. `list_pending_operations` ya ordena por `(node_id, sequence)`: el orden se perdía en el worker.
- Decisión:
  1. **Un fallo de transporte o un error reintentable corta la tanda** (`break`): cualquier excepción del `push` (timeout,
     5xx, el 409 de esquema, `EsquemaIncompatible`). La operación queda `retryable_error` y las que siguen no se tocan (ni
     gastan un intento): salen en el ciclo siguiente, después de ella. Cortar además ahorra un timeout por cada operación
     que quedaba en la tanda cuando la red está caída.
  2. **Un rechazo definitivo (`rejected`, que el nodo guarda como `manual_review`) no traba la cola**: la tanda sigue.
     Es una *respuesta* del central, no una falla: esperarla no la arregla, y detener la cola detrás de ella dejaría **todas**
     las ventas posteriores del local sin subir hasta que alguien la resuelva a mano, con el nodo diciendo que está en
     línea. Nada se pierde: la operación queda entera en el nodo y en `sync_inbox` del central para revisarla
     (`libraedge-central revision`, `libraedge-nodo revision`).
     - **El 422 del central también es un rechazo.** `HttpSyncTransport` levantaba cualquier `HTTPError` como excepción, o
       sea reintentable; el 422 de `/sync/v1/push` (operación mal formada: faltan campos, `sequence` que no es un entero)
       es una respuesta definitiva y reintentarla no la arregla. Con el `break` habría trabado la cola para siempre, así que
       ahora el transporte lo devuelve como `PushResult("rejected", ...)` y va a revisión. 401, 404, 409, 5xx y los errores
       de red siguen siendo reintentables: valen para todas las operaciones, y esperar sí las arregla (credenciales,
       esquema, el central que vuelve).
  3. Lo que dependía de una rechazada, el central lo rechaza a su vez (el manejador no encuentra lo que necesita) y va a
     revisión también. Al resolverlas, `reenviar_operacion` las devuelve a `pending` y salen por secuencia: **hay que reenviar
     en el orden en que se crearon**; si se reenvía la dependiente sola, vuelve a rechazarse.
- Descartadas: cortar también ante un rechazo (ver 2); saltear sólo las operaciones del mismo agregado que la fallida
  (el vínculo `venta → cliente` no es del mismo agregado, y el worker no conoce los payloads); reordenar con un grafo de
  dependencias (el nodo no sabe qué depende de qué: sólo la secuencia lo dice).
- Consecuencias: con la red caída, cada ciclo intenta **una** operación en vez de todas, así que `fallidas` de un
  `ResultadoOutbox` vale como máximo 1 (nadie lo usa como cuenta; `hubo_falla_de_transporte` sigue igual). Una operación
  que el transporte rechaza siempre con una excepción **traba la cola detrás de ella** (un payload que revienta el `push`
  antes de salir, por ejemplo; el 422 ya no es de éstos): el nodo queda fuera de línea con `ultimo_error` diciendo cuál, en
  vez de subir lo de atrás desordenado. **Lo que no cubre:** si el central acepta una venta cuyo manejador no verifica
  que exista el cliente, un `cliente.alta` rechazado antes no la frena; eso es un contrato de cada manejador del producto,
  que debe rechazar lo que le falta.

## ADR-021 — Las altas de referencia en el nodo: id provisorio negativo, mapeo del central y reapuntado por el espejo

- Estado: aceptada (Fase 2 de LibraEdge, decidida por el humano; sale en v0.18.0). Opt-in por tabla; esta versión la usa **sólo
  para clientes** (productos va después, con el mismo mecanismo).
- Fecha: 2026-10-21
- Contexto: ADR-002 dice que el central manda sobre la referencia y el nodo la espeja, y ADR-019 dejó abierto qué hacer con las
  **altas de clientes en el nodo**: el POS crea clientes en el nodo, y `clients` se espeja. Un cliente creado en el nodo tomaba el
  siguiente id de la secuencia **de la base del nodo**, que no tiene nada que ver con la del central: chocaba por id con un cliente
  del central (o lo pisaba el espejo, que escribe con el id del central) y la venta subía con un `cliente_id` que en el central es
  de **otro cliente**, sin ningún error. La decisión del humano (2026-10-21): **las altas de referencia en el nodo se permiten y
  suben como operación; el central asigna su id y reescribe las referencias.** Esta decisión es la mitad de LibraEdge: el id
  provisorio, el mapeo y el reapuntado. El alta como operación (`cliente.alta`) y el aplicador del central son de `libracommerce`
  (ADR-070) y el gancho del alta de `libracore` (ADR-077): LibraEdge no sabe qué es un cliente (ADR-001).
- Qué ya existe (se miró antes de diseñar): `local_sequences` y `NodeRepository.next_sequence(commit=False)` (la secuencia del
  outbox, en la transacción del llamador); el espejo con clave compuesta (ADR-007), que ya sabe mover una tabla de clave
  `(node_id, tabla, id_nodo)`; la guarda (ADR-019) y su patrón de declarar por `Aprovisionamiento` → `registrar` → carga →
  `nodo.env` → `libraedge-nodo`; el catálogo de PostgreSQL como fuente de las claves (`clave_primaria`, `_dependencias`); y la regla
  del `SyncReceiver` de revisión manual para `ValueError` (ADR-007) sin trabar la cola (ADR-020).
- Decisión:
  1. **Id provisorio negativo** (`libraedge.mapeo.siguiente_id_provisorio(conn)`): -1, -2, ... de `local_sequences`, **en la
     transacción del alta** (no commitea; si el alta se deshace, el número también) y **atómico** entre procesos (un `UPDATE ...
     RETURNING` toma la fila hasta el commit). Un id del central es siempre positivo, así que **no se cruza nunca** con uno que
     llegue por el espejo ni con uno que el nodo todavía no conoce, y `id < 0` dice a simple vista «todavía no existe en el
     central». No se reusa jamás: el mapeo es por `(nodo, tabla, id_nodo)` y un id reusado apuntaría a otra fila.
  2. **`libraedge_mapeo(node_id, tabla, id_nodo, id_central, creado_at)`**, clave `(node_id, tabla, id_nodo)`, la crea
     `init_schema()` (en el central y en el nodo, sin migración del producto). El central la llena al aplicar el alta
     (`anotar_mapeo`: idempotente; un mapeo distinto para el mismo id es `MapeoEnConflicto`, o sea que el nodo reusó un id) y la usa
     para traducir las referencias de lo que sube después (`traducir_id(conn, node_id, tabla, id_nodo)`: `None` y un id no negativo
     pasan sin tocar; uno negativo se busca y, **sin mapeo, `SinMapeo`, un `ValueError`**: revisión manual con un mensaje que dice qué
     alta falta, y la cola sigue).
  3. **El mapeo baja por el espejo y es lo que dispara el reapuntado.** `libraedge_mapeo` es una tabla más del espejo (la publica
     el central con su trigger y su clave compuesta; declarar `altas_en_nodo` la suma sola a las tablas de referencia y a las de
     solo lectura). `MirrorApplier(conn, tablas, node_id=, altas=)` se queda con las filas **de su nodo** (las de otros nodos y
     las de tablas sin alta se ignoran) y, al guardar una, reapunta (`sync/reconciliacion.py`), todo en la transacción del `aplicar`.
  4. **El reapuntado** (`reapuntar(conn, tabla, provisorio, definitivo)`), cuando ya están **las dos filas** (la provisoria y la
     definitiva, que bajó con su contenido): (a) cada **clave foránea de una columna** hacia la tabla, leída del catálogo de la
     base (`pg_constraint` / `PRAGMA foreign_key_list`) y no de una lista, pasa del provisorio al definitivo; (b) las
     **referencias sin FK** que el producto declaró (`Referencia(tabla, columna, donde)`: el `tercero_id` de un libro de
     cuentas, que sirve a clientes y proveedores y por eso no tiene FK; el `donde` es sólo igualdades) hacen lo mismo; (c) lo que
     cuelga de la provisoria desde una **tabla que el nodo espeja** se borra y no se reapunta (la autoridad es el central, que
     nunca creó esa fila); (d) se borra la fila provisoria. Es la única operación del espejo que borra una fila que el central no
     borró, y por eso es estrecha: sólo con un id negativo y sólo cuando la definitiva ya está. Idempotente (`YA_ESTABA`).
  5. **Un reapuntado que falla no frena el espejo.** Va en un `SAVEPOINT`: se deshace entero (también en PostgreSQL, donde un error
     aborta la transacción del cursor), queda en `MirrorApplier.sin_reconciliar` y en stderr (una vez por motivo) y la bajada
     sigue. `PullWorker` llama `reconciliar_pendientes()` al final de cada ciclo: reintenta lo que quedó (la definitiva todavía
     no había bajado, o el choque se arregló) con una consulta barata por tabla. Lo peor que pasa es que la fila provisoria
     convive un rato con la definitiva.
  6. **Se declara por tabla y viaja por la carga**: `Aprovisionamiento(..., altas_en_nodo=[("clients", [("cc_asientos",
     "tercero_id", {"rol": "cliente"})]), "parties"])` (tienen que ser de referencia; `AltaEnNodo`/`Referencia` en
     `libraedge.altas`). `registrar` manda la variable **opcional** `LIBRAEDGE_ALTAS_EN_NODO` (`clients:cc_asientos.tercero_id@rol=
     cliente,parties`: sin comillas ni caracteres de shell, porque termina en un archivo de entorno), `instalador` la valida con
     un parser propio —ese módulo sólo usa la biblioteca estándar— y un test fija que coincide con el del nodo, y `libraedge-nodo`
     la lee al arrancar y arma el aplicador. **`registrar` no registra el nodo** (y no escribe nada) si el central no puede
     publicar la tabla o el mapeo: un nodo así daría de alta filas que nunca se reapuntan.
  7. **La guarda (ADR-019) deja pasar lo provisorio.** Una tabla con alta puede estar además en `tablas_solo_lectura`: su trigger
     recibe la columna clave y acepta las filas con id negativo (alta, edición y baja de lo provisorio; un `UPDATE` exige que el id
     anterior **y** el nuevo sean provisorios) y sigue rechazando todo lo que toque una fila del central. Sin la clave
     (`instalar_guarda_de_espejo(conn, tablas)`) es la de v0.17.
- Desvío del diseño propuesto, y por qué: **no hay columna `uid`.** La propuesta era un UUID en la fila para que el nodo
  reconociera su provisoria al bajar la del central. No alcanza: cuando el central **deduplica** —el CUIT ya existía, o dos
  nodos dieron de alta al mismo cliente— la fila que baja es otra y su `uid` no es el del nodo, así que la provisoria quedaría
  para siempre y la venta local a su nombre también. El mapeo dice exactamente lo que el central decidió, sea cual sea el
  motivo, y además resuelve en un solo mecanismo la traducción de las referencias en el central. Y evita lo que el `uid` pedía:
  una columna nueva en `clients` y en `parties` (la fila entera viaja por el espejo, así que **todos** los nodos tendrían que
  migrar antes de seguir sincronizando: 409 de esquema), un backfill de las filas existentes y el cambio de la fixture congelada
  del esquema de libracore. Con el mapeo no se toca ninguna tabla del producto.
- Descartadas: el `uid` (ver arriba); un trigger de PostgreSQL que asigne el id negativo en el nodo (no cubre SQLite, que es lo que
  prueba el resto de la familia; y el `lastrowid` de la capa de LibraCore no lo vería: el id lo asigna mejor quien hace el
  alta, que es `libracore.db.clients`); reapuntar con `UPDATE ... ON UPDATE CASCADE` (las FK del motor no lo declaran y cambiar
  las FK de las tablas del producto es una migración por producto); devolver el id definitivo en la respuesta del `push` (cambia el
  protocolo y se pierde si la respuesta se pierde: el mapeo espejado es idempotente y sobrevive a un corte); filtrar el mapeo por
  nodo en el changelog (el changelog es global y filtrar es un cambio de protocolo; cada nodo recibe unas filas de más y ignora las
  que no son suyas).
- Consecuencias: **MINOR (v0.18.0)**: suma API y una variable opcional de la carga, y `init_schema()` crea `libraedge_mapeo`; sin
  declarar `altas_en_nodo` el comportamiento es el de v0.17. Un **instalador anterior a v0.18 rechaza** una carga con
  `LIBRAEDGE_ALTAS_EN_NODO` («trae variables que no le corresponden»): actualizar el instalador antes de que un producto declare
  las altas. En el central, `libraedge-central nodo publicar` suma el trigger de `libraedge_mapeo` y siembra lo que haya.
  El id provisorio **no es seguro de reusar**: una PC reemplazada con el mismo `node_id` que arrancara `local_sequences` de cero
  reusaría el -1 (el aplicador del central lo detecta si el cliente es otro y lo manda a revisión; el reemplazo de una PC
  —`dar-de-baja` y `registrar`— es un nodo nuevo, y con el mismo `node_id` ya se repetirían también los `operation_id`). Mientras el mapeo no baja, el nodo ve al cliente dos veces (el provisorio y el del central): se resuelve en el
  mismo ciclo. **Lo que no cubre:** la *edición* o baja de un cliente creado en el nodo no sube (la decisión es de altas), así que
  lo que se corrija en el nodo antes de que baje el cliente del central se pierde cuando se borra la provisoria; las tablas de
  clave compuesta no admiten alta; una referencia sin FK que el producto no declare queda colgada del id provisorio (la
  declaración de `libracore` está fijada contra su esquema, pero el esquema de un producto es suyo); y el nodo no publica
  `sin_reconciliar` en su estado (hoy sólo stderr).

## ADR-022 — Conservar los nodos al recrear la base del central: `nodo conservar` y `nodo restaurar`

- Estado: aceptada (pedido del humano del 2026-10-21; sale en v0.19.0)
- Fecha: 2026-10-21
- Contexto: el reset nocturno de las demos (`scripts/reset_demo.sh` de cada producto) hace `DROP SCHEMA`, el arranque reconstruye
  el esquema y se siembra con `scripts/seed_demo.py`. Se pierden dos cosas de LibraEdge. **`node_identity`**: la PC vinculada a la
  demo queda huérfana (401) todas las mañanas. **`sync_changelog`**: es un `BIGSERIAL` (la capa de LibraCore traduce
  `INTEGER PRIMARY KEY AUTOINCREMENT`), así que la base nueva numera desde 1, y el nodo guarda su cursor (`last_server_cursor`) y
  pide `GET /sync/v1/pull?cursor=<el suyo>`: con cursor 500 y un changelog que llega a 120 no baja nada, **para siempre y sin ningún
  error**. Registrar el nodo de nuevo no arregla lo segundo, y obligaría a reinstalar la PC.
- Decisión:
  1. **`libraedge-central nodo conservar --a <archivo>`** escribe un JSON con las filas **enteras** de `node_identity` (todas las
     columnas, `secret_hash` incluido: el central nunca tuvo el secreto en claro) y el **cursor máximo** del changelog. Permisos 600
     desde la creación (`O_EXCL`), no pisa un archivo que exista, no imprime nada de su contenido. Sin nodos escribe igual un
     archivo válido con la lista vacía, para que quien lo usa no confunda «no había nodos» con «falló». El cursor máximo es el mayor
     entre la última fila **y la secuencia** (un changelog podado deja la secuencia por encima de su última fila, y el nodo pudo
     haber llegado hasta ahí).
  2. **`libraedge-central nodo restaurar --de <archivo>`**, en la base nueva y con la instancia ya arrancada: (a) reinserta los nodos,
     idempotente por `node_id` (el que ya está no se toca); sólo las columnas que la tabla tiene; (b) lleva la secuencia de
     `sync_changelog.cursor_id` a **>= el máximo viejo** (`setval` sobre la secuencia que dice `pg_get_serial_sequence`, sólo si
     está por debajo) y **renumera** lo que la base nueva ya tuviera con cursor <= ese máximo (en el mismo orden): esas filas
     quedarían por debajo del cursor del nodo y nadie las bajaría; (c) corre `publicar`, que instala los triggers y siembra la
     referencia **con cursores nuevos, todos mayores que el del nodo**, así que el nodo baja el estado nuevo completo. Con la lista
     vacía no hace nada salvo avisar; con un archivo que no existe, o que no es de `conservar`, sale 1 con el motivo.
  3. El archivo lleva `formato: 1`; `restaurar` rechaza uno que no conoce.
- Descartadas: guardar la **secuencia** con `pg_dump` o dejar `node_identity` y `sync_changelog` fuera del `DROP SCHEMA` (el reset
  borra el esquema entero y la cadena de migraciones espera una base vacía; dos tablas sobrevivientes son un estado que el arranque
  no espera); **resetear el cursor del nodo** a 0 desde el central (el central no lleva el cursor de nadie, ADR-002: el nodo es el
  único que sabe qué aplicó); un **épsilon** fijo para la secuencia (`+1.000.000`): oculta el problema hasta que el changelog crece;
  registrar el nodo de nuevo (emite un secreto nuevo: hay que tocar la PC).
- Consecuencias: **MINOR (v0.19.0)**: suma dos subcomandos y API (`conservar`, `restaurar`, `cursor_maximo`), sin tocar el esquema.
  Los productos que la usan detectan el subcomando (`libraedge-central nodo conservar --help`) para seguir andando con un pin viejo.
  **Lo que no cubre, y para una demo es aceptable:** (1) una fila que estaba en la base vieja y no está en la nueva siembra **no baja
  como borrado** al nodo --el changelog nuevo no tiene el `DELETE`--: el nodo la conserva hasta que se lo reinstale; la siembra son
  `upsert`, y lo que coincide por clave se pisa con el estado nuevo. (2) `libraedge_mapeo` y `libraedge_vinculacion` **no se
  conservan**: apuntan a ids de la demo vieja (el mapeo de los clientes dados de alta en el nodo, los códigos de vinculación
  emitidos). Un código de vinculación sin canjear se pierde; una PC ya vinculada no lo necesita. (3) `sync_inbox`
  (lo que el central recibió) tampoco: la demo nueva no tiene las ventas que subió el nodo ayer. (4) Si entre `conservar` y
  `restaurar` alguien registró otro nodo con el mismo prefijo de numeración, `restaurar` falla con un error claro y no deja nada a
  medias. (5) El archivo es un secreto (hashes): quien lo usa lo guarda con permisos 600 y lo borra al terminar.

## ADR-023 — La carga inicial de existencias: al vincular, el nodo trae las de su sucursal; desde ahí manda el nodo

- Estado: aceptada (decisión del humano del 2026-10-21; sale en v0.20.0)
- Fecha: 2026-10-21
- Contexto: el espejo (ADR-002) trae el catálogo, los precios y los depósitos, pero **las existencias son de autoridad del
  nodo**: las mueven sus ventas y sus recepciones, y no bajan por el changelog. Un nodo recién vinculado arranca con el
  stock en cero aunque la sucursal tenga mercadería. El humano decidió que **al vincular el nodo trae las existencias de
  SU sucursal del central, y desde ahí manda el nodo**: no hay una segunda bajada ni un vaivén de stock. La aritmética
  (qué es una existencia, cómo se arma el movimiento de apertura) es de LibraCommerce
  (`libracommerce.erp.offline_stock`); LibraEdge sólo pone las costuras genéricas, y sigue sin importar a LibraCommerce.
- Decisión:
  1. **Central: `GET /sync/v1/carga-inicial?node_id=`**, en `create_sync_router(..., carga_inicial=None)`, con el molde de
     `prestar_ticket`. Mismo gate que push y pull (`_autenticar` con el latido, `_verificar_esquema`). **La sucursal sale de
     `node_identity.branch_id` del nodo autenticado, no de un parámetro**: con el secreto de un nodo no se piden las
     existencias de otra sucursal (ni de otro nodo: el `node_id` tiene que ser el del secreto, si no 401). La costura es
     `carga_inicial(conexion, node_id, branch_id) -> dict`, **sincrónica** (recibe la conexión del request; una `async` se
     rechaza al construir el router). `branch_id` llega como está guardado en `node_identity`, **texto**. El `dict` viaja
     con `default=str`, igual que la bajada: los `Decimal` son texto y no se degradan a `float`. Sin la costura contesta
     **501** (después de autenticar: a quien no es un nodo no se le cuenta qué tiene este central) con
     `detail.codigo = carga_inicial_no_disponible`. Una costura que no devuelve un `dict` es 502.
  2. **Nodo: `CargaInicialWorker`** (`libraedge/sync/carga_inicial.py`), un paso de `Nodo.sincronizar` **después del pull**,
     mientras falte la marca **`carga_inicial:<node_id>`** en `local_sequences` (`NodeRepository.tiene_marca/poner_marca/
     quitar_marca`: la tabla ya existe en los dos motores y en todos los nodos instalados, así que no hay migración). Llama
     a la función del producto de `LIBRAEDGE_CARGA_INICIAL=modulo:funcion` con **`(conexion, traer)`**; `traer()` hace el
     GET con el mismo patrón de transporte que la bajada (`HttpCargaInicialTransport`, secreto del nodo, cabecera de
     esquema, timeout de 30 s). La función devuelve un `dict`: con **`"hecha": False`** (y un `"motivo"`) dice «todavía no
     puedo» y el worker **no marca** y reintenta el ciclo siguiente; cualquier otra cosa es «lo hice» y marca.
  3. **La función y la marca entran en una sola transacción.** La función no necesita commitear: si no lo hace, el worker
     commitea lo que escribió **junto con la marca**. No puede quedar aplicada y sin marca (se aplicaría de nuevo y pisaría
     el stock que el nodo ya movió) ni marcada y sin aplicar (el nodo no tendría nunca sus existencias). Ante una
     excepción o un «espero», `rollback`.
  4. **Solo corre cuando hay con qué.** Si el ciclo ya tuvo un error (sin central, subida caída) no se intenta. Si la
     bajada de este ciclo llenó su tanda (`bajados >= limite`, puede quedar espejo por bajar) tampoco: la función lee del
     espejo (depósitos, sucursal) y llamarla con un espejo a medias la haría esperar o, peor, cargar de menos.
  5. **Errores: se registran en el estado y el nodo sigue.** `EstadoNodo.carga_inicial` (campo nuevo, `None` por defecto) vale
     `None` si está hecha o el producto no tiene, `esperando: <motivo>` o `error: <Tipo>: <mensaje>`. **No toca `en_linea`
     ni `ultimo_error`**: que la carga inicial espere o falle no es estar fuera de línea, y no puede frenar la subida de
     las ventas (la bandeja diría «sin conexión» por un dato que se arregla actualizando). `run_once` no levanta. La
     función se importa **al intentar, no al arrancar** (`LIBRAEDGE_CARGA_INICIAL` es una variable blanda, a diferencia de
     `LIBRAEDGE_TABLAS_ESPEJO`): un nodo cuyo libracommerce todavía no trae `offline_stock` no se cae; queda
     `error: ... no se pudo importar el modulo ... actualizar el nodo`, no se marca y se reintenta solo cuando se actualice.
  6. **501 = el producto no tiene carga inicial**: se marca como hecha (nada que traer) y se loguea **una vez** con
     `warning` (sin `logging` configurado sólo salen los warnings). **Un 404 no es un 501**: un central anterior a v0.20 no
     tiene la ruta, y eso se arregla actualizándolo; darlo por hecho dejaría al nodo sin sus existencias para siempre. 401,
     409 de esquema, red caída y los demás: error y reintento.
  7. **CLI de soporte: `libraedge-nodo carga-inicial [--forzar]`**. Hace un intento y imprime el resultado en JSON; sale 0
     si quedó terminada (hecha, ya estaba, o el central no tiene nada que traer) y 1 si espera o falla. Sin `--forzar`, si la
     marca existe no hace nada y lo dice. `--forzar` borra la marca y corre ya: **pisa las existencias del nodo con las del
     central**, así que es de soporte (nodo recién vinculado que quedó en cero, o reinstalado a propósito), no de rutina.
  8. **Instalador: `LIBRAEDGE_CARGA_INICIAL` viaja como `variables` del producto** (`instalador/productos/*.json`, que ya
     existía y estaba vacío): `libracommerce.erp.offline_stock:aplicar_carga_inicial` en ContaLibra, RestoLibra y VentaLibra
     (los tres corren las migraciones de libracommerce). Con la carga, `armar_entorno` la escribe en el `nodo.env` (pisa,
     como toda `variables`). **Para que la reciban los nodos ya instalados al actualizar**, `completar-entorno` aprende
     `--definicion`: agrega las `variables` del producto que **falten** (respeta la que ya esté, con cualquier valor: es lo
     que soporte pudo haber tocado a mano), y lo usan `preparar_nodo.sh` (apt) e `instalar.ps1` (`/SINVINCULAR` sobre un
     nodo vinculado). Con sólo declararla en el json, una actualización **no** la habría traído: sin carga el `nodo.env` no
     se rearma. La definición valida la forma `modulo:funcion`. No es obligatoria para `revisar-entorno`: un nodo sin ella
     sigue siendo un nodo completo.
  9. **Windows: el entorno de los servicios se vuelve a cargar al actualizar.** Los servicios de NSSM guardan su entorno en
     el registro (`AppEnvironmentExtra`, escrito por `preparar_nodo.ps1`) y **no releen el `nodo.env`**. Hasta hoy
     `Actualizar-NodoVinculado` completaba el archivo y arrancaba los servicios con el entorno viejo, así que una variable
     agregada por `completar-entorno` no llegaba a un nodo de Windows. `Actualizar-NodoVinculado` ahora hace el mismo
     `nssm set ... AppEnvironmentExtra` que la instalación, antes de arrancar los servicios. En Linux no hace falta
     (`EnvironmentFile=` se lee en cada arranque).
  10. **El registro de un nodo pasa por un chequeo del producto** (decisión del humano: si la instancia tiene depósitos sin
     sucursal, vincular se niega). LibraEdge no conoce depósitos: `Aprovisionamiento(..., validar_registro=)` es opcional,
     `validar_registro(conn) -> list[str]` de problemas escritos para quien instala (`None` o vacía: se puede). Lo consulta
     `registrar` —que es el embudo de todas las altas: el CLI, el alta por código, el alta por archivo, la carga nueva y el
     canje—, después de las guardas de siempre (sucursal, segundo nodo, adicional) y **antes de escribir nada**. Con
     problemas levanta `RegistroConProblemas` (un `ErrorDeAprovisionamiento`, con `.problemas`): las pantallas del central
     contestan **409** `registro_con_problemas` con el texto, y el CLI sale 1. El canje contesta 409 y **no gasta el
     código**. `pedir_otro_codigo` lo consulta también, para no emitir un código que el canje va a rechazar. Corre en cada
     registro, también el de una PC que se reemplaza: si se niega, el secreto del nodo que ya andaba no se invalida.
- Descartadas:
  - **Que la carga inicial baje por el changelog**: las existencias son de autoridad del nodo (ADR-002); publicarlas haría
    que el central se las devolviera a cada cambio y el nodo se pisara su stock.
  - **Que el nodo elija la sucursal** (`?branch_id=`): cualquier nodo con su secreto podría pedir las existencias de otra
    sucursal.
  - **Marcar «hecha» ante cualquier 4xx/5xx del central**, o ante un 404: un central sin actualizar dejaría al nodo en cero
    para siempre y en silencio.
  - **Un paso del instalador, o un comando que corra una sola vez a mano**: el nodo ya sincroniza cada minuto, ya tiene la
    conexión y el espejo, y reintenta solo cuando falta algo; un paso aparte necesitaría su propio manejo de «todavía no».
  - **Exigir que la función commitee**, o commitear la marca aparte: abre la ventana de aplicada-sin-marca descrita arriba.
  - **Fallar al arrancar** si `LIBRAEDGE_CARGA_INICIAL` no se entiende o no se importa: dejaría una caja sin sistema por un
    dato que se arregla actualizando.
  - **Poner el error en `ultimo_error`**: lo lee `en_linea`, la bandeja y los scripts de verificación como «sin conexión».
- Consecuencias: **MINOR (v0.20.0)**: suma una ruta, un paso del ciclo, una variable, un subcomando, un campo del estado y
  dos costuras opcionales; sin tocar el esquema ni lo que había. **Los productos tienen que sumar la fila de
  `GET /sync/v1/carga-inicial` en su matriz de roles**, como «PÚBLICA» autenticada por el secreto del nodo, igual que push,
  pull y ticket-arca (`libraedge.sync.api.RUTAS_AUTENTICADAS_POR_NODO` es la lista); y pasar `carga_inicial=` al router y
  `validar_registro=` al `Aprovisionamiento` (en la familia, `carga_inicial_del_central` y
  `depositos_sin_sucursal` de `libracommerce.erp.offline_stock`). **Lo que la función del producto tiene que cumplir**:
  idempotencia razonable (el worker la protege con la transacción única, pero `--forzar` la vuelve a correr), esperar con
  `{"hecha": False, "motivo": ...}` si el espejo no trajo todavía lo que necesita, y no commitear si quiere la atomicidad
  con la marca. **Lo que no cubre**: (1) un nodo que ya vendió antes de recibir la carga inicial: la función decide qué hace
  con esas existencias (el worker no sabe de stock); (2) la bandeja no muestra `carga_inicial`: sólo el estado, `libraedge-
  nodo estado` y la línea de `correr`; (3) la marca vive en la base del nodo: un nodo cuya base se recrea vuelve a cargar
  (correcto: arranca en cero), y uno que conserva la base no (sí con `--forzar`).

## ADR-024 — Descartar una operación en revisión del nodo: `libraedge-nodo revision descartar`

- Estado: aceptada (hueco encontrado en la prueba real del 2026-10-22; sale en v0.21.0)
- Fecha: 2026-10-22
- Contexto: una operación del outbox que el central rechaza (`rejected`) queda en `manual_review` del nodo (ADR-020) y de ahí
  sólo salía por `libraedge-nodo revision reenviar`, es decir, si el central la aceptaba después de resolverla. Si el
  rechazo es definitivo (la operación es de una prueba, apunta a algo que no existe en el central, el dato ya no importa),
  quedaba en `manual_review` **para siempre**. No es sólo ruido: en LibraCommerce, mientras haya una operación de stock en
  `manual_review`, la carga inicial de existencias (ADR-023) espera (`stock_en_revision`) y no corre nunca. En la prueba
  real hubo que tocar la base de la VM a mano.
- Decisión:
  1. **Estado terminal nuevo `discarded`** (`SyncOperationStatus.DISCARDED`; en inglés como `manual_review` y los demás, y
     igual que el `discarded` del `sync_inbox` del central). **No suma columnas ni migración**: `sync_outbox.status` es
     `TEXT` sin `CHECK`, así que el estado nuevo entra sin tocar el esquema, y `LIBRAEDGE_VERSION_ESQUEMA`
     (`version_de_alembic`) no cambia: un nodo v0.21 y un central v0.20 siguen coincidiendo (nada de 409). Los productos que
     crean `sync_outbox` en su cadena de Alembic tampoco necesitan una migración.
  2. **`NodeRepository.descartar_operacion(operation_id, motivo)`** y el CLI **`libraedge-nodo revision descartar
     <operation_id> --motivo "<texto>"`**. Sólo descarta una operación en `manual_review`. Motivo obligatorio y no vacío
     (`--motivo` es requerido; en blanco, sale 1). Una operación `pending`, `sending`, `retryable_error` o `acknowledged`
     no se descarta (error claro y sale 1): las primeras todavía pueden salir solas, y una `acknowledged` el central ya la
     tiene. Una inexistente, también 1.
  3. **El motivo y el momento van en `last_error`**, que pasa de `<rechazo del central>` a
     `descartada el <ISO-8601 UTC>: <motivo> | rechazo del central: <rechazo>`. Se prefirió a una columna nueva por lo de
     arriba (sin esquema), y como se **antepone** y se conserva el rechazo original no se pierde la evidencia de por qué
     se rechazó. El costo: el momento no es una columna consultable por SQL sino texto; para soporte alcanza. Si algún día
     hace falta ordenar o filtrar por fecha de descarte, ahí sí corresponde una columna con su `ALTER` en `init_schema`.
  4. **Idempotente frente a reintentos: descartar dos veces avisa por `stderr` ("ya estaba descartada"), sale 0 y no
     pisa el motivo original.** Es el caso de un soporte que repite el comando o de un script reintentado; no es un error,
     y el resultado deseado (la operación cerrada) ya se cumple. Cualquier otro estado distinto de `manual_review` sigue
     siendo error (1). El `UPDATE` repite el estado en el `WHERE`, así que dos descartes simultáneos no se pisan.
  5. **`revision listar` sigue mostrando sólo lo que espera a alguien (`manual_review`); `--todas` suma las descartadas**,
     igual que `libraedge-central revision listar --todas`. Cada línea trae ahora `estado` además de `motivo`; para una
     descartada, `motivo` es el `last_error` completo (motivo, momento y rechazo original). No se mostraron siempre porque
     la lista es la bandeja de trabajo de soporte y las descartadas son historia.
  6. **El worker no la vuelve a mandar y no es pendiente**: `list_pending_operations` sólo mira `pending` y
     `retryable_error`, `reclamar_operaciones_colgadas` sólo `sending`, y `reenviar_operacion` sólo `manual_review` (una
     descartada es `LookupError`, no se reabre). Como `Nodo.pendientes()` sale de `list_pending_operations`, ni
     `EstadoNodo.pendientes`, ni `libraedge-nodo estado`, ni la bandeja la cuentan. Tampoco la contaba ya `manual_review`:
     lo que cambia es que ahora hay forma de sacarla de la cola de revisión.
- Procedimiento de soporte:
  1. Mirar qué hay: `libraedge-nodo revision listar` (en el nodo) y, del lado del central, `libraedge-central revision
     listar --nodo <node_id>`.
  2. **Descartar en el nodo sólo cuando el central la rechazó con una razón y no se va a corregir.** Si el rechazo se arregla
     (se despliega el manejador que faltaba, se carga el dato que chocaba), lo que corresponde es `libraedge-central
     revision reintentar` y después `libraedge-nodo revision reenviar`, no descartar.
  3. `libraedge-nodo revision descartar <operation_id> --motivo "<qué pasó y quién lo decidió>"`.
  4. **Si era un movimiento de stock** (la carga inicial esperaba por `stock_en_revision`): después de descartar,
     `libraedge-nodo carga-inicial --forzar` realinea el stock del nodo con el central. La fila local del movimiento
     descartado **queda** en la base del nodo (descartar no deshace nada de lo que la operación hizo en el nodo), y el
     delta de la carga inicial la compensa (ese cálculo es del producto, ADR-023, no de LibraEdge). Recordar que
     `--forzar` pisa las existencias del nodo con las del central: antes de forzar, revisar con el producto qué pasa con
     lo que el nodo vendió o movió desde que la carga quedó esperando.
  5. **En el central** la operación sigue en `manual_review` de su `sync_inbox` (el nodo no se lo avisa: no hay ruta para
     eso, y el nodo no la va a mandar nunca más). No se verificó que ahí frene algo (lo que frena la carga inicial es la
     operación en el outbox **del nodo**), pero queda en `libraedge-central revision listar` para siempre; conviene
     cerrarla también, con
     `libraedge-central revision descartar <operation_id> --por <quién> --motivo "<lo mismo>"`. El orden no importa.
- Descartadas:
  - **Una columna nueva (`discarded_at`, `discard_reason`)**: cambia el esquema del nodo y de los productos que crean la
     tabla por Alembic, y exige coordinar versiones de nodo y central sin que haga falta (ver 3).
  - **Pisar `last_error` con sólo el motivo**: se perdería por qué rechazó el central.
  - **Que descartar una ya descartada sea un error (sale 1)**: obliga a un script a distinguir «falló» de «ya estaba»
    leyendo el texto; el aviso con salida 0 es seguro de reintentar.
  - **Descartar también `pending` / `retryable_error`**: una operación que todavía puede salir se perdería sin que el central
    la haya visto nunca. Para eso está el outbox: se espera, o se la lleva a revisión con un rechazo real.
  - **Borrar la fila**: el outbox es la constancia de qué hizo el nodo; el central tiene su `resolucion` para lo mismo.
  - **Avisarle al central al descartar** (una ruta nueva): es otra superficie autenticada para algo de housekeeping; el
    comando del central ya existe.
- Consecuencias: **MINOR (v0.21.0)**: suma un estado, dos métodos del repositorio, una acción del CLI y un flag, y un campo
  (`estado`) en cada línea de `revision listar`; sin tocar el esquema, la versión de esquema que el nodo declara ni lo que
  había. Quien lea `revision listar` como JSON línea a línea no se rompe (campo agregado). **Lo que no cubre**: (1) descartar
  no revierte lo que la operación hizo en el nodo (la venta, el movimiento de stock): sólo la saca de la cola de envío; (2)
  el momento del descarte es texto en `last_error`, no una columna; (3) no hay "des-descartar": si fue un error, la
  operación se vuelve a encolar con un `UPDATE` manual o se rehace la operación.

## ADR-025 — El ticket de ARCA por servicio (`wsfe` y `wscpe`), y LibraCargo y LibraClub en el instalador y la app de escritorio

- Estado: propuesta
- Fecha: 2026-10-23
- Contexto: es el PR 6 del plan «LibraCargo y LibraClub en la versión de escritorio»
  (`wiki/analyses/libraedge-libracargo-libraclub-2026-10-22.md`). Decisión del humano (2026-10-22): LibraCargo emite sus CPE
  y sus facturas desde el nodo, y sin internet **quedan en cola y se emiten solas al volver**. libracore v1.191.0 (ADR-079)
  puso del lado del motor la cola de comprobantes (`libracore.cola_arca`) y le enseñó a `ta_para_el_nodo` a prestar el
  ticket de dos servicios: `wsfe` (la facturación, con el par de `arca_config`) y `wscpe` (la carta de porte, con el par de
  `arca_credenciales_servicio`). Faltaba el transporte: `POST /sync/v1/ticket-arca` (ADR-009) no sabía de servicios y la
  caché del cliente era por emisor. ARCA da **un ticket por certificado y por servicio**, así que servirle a quien pide
  `wscpe` el de `wsfe` que ya estaba guardado lo hace llamar a ARCA con un ticket que no es el suyo (y ARCA contesta con un
  error de autenticación que no explica nada). Además LibraCargo y LibraClub son de **una sola base** desde libracore
  v1.190.1 (ADR-078, `_UNA_SOLA_BASE`): ya no existe `libraclub_core`.

### Parte A — El ticket por servicio

- Decisión:
  1. **`POST /sync/v1/ticket-arca` recibe `servicio`** (opcional; `null` o ausente es `wsfe`, que es lo que mandan los nodos
     de v0.21 y anteriores). Se normaliza a minúsculas y tiene que ser un nombre de servicio (`[a-z][a-z0-9_]{0,31}`):
     cualquier otra cosa es 422 `invalid servicio` **antes** de tocar el prestador. LibraEdge no sabe qué servicios existen;
     el que decide cuáles se prestan es el producto (el motor tiene `SERVICIOS_PRESTABLES = ("wsfe", "wscpe")`).
  2. **El prestador recibe `servicio=`** si su firma lo acepta (un parámetro `servicio` o `**kwargs`, que se mira con
     `inspect.signature` al montar el router). El producto central lo cablea como
     `lambda emisor_id, servicio="wsfe": ta_para_el_nodo(emisor_id, servicio=servicio, empresa=EMPRESA_ARCA)`
     (`EMPRESA_ARCA = "agencia"` en LibraCargo; en un producto sin CPE alcanza `prestar_ticket=ta_para_el_nodo`, y pedirle
     `wscpe` es un `ValueError` porque falta la `empresa`). **Compatible hacia atrás**: un prestador de antes,
     `prestar_ticket(emisor_id)`, se llama exactamente como antes cuando el servicio es `wsfe`; con cualquier otro servicio
     la ruta contesta **501 `ticket_no_prestado`** sin llamarlo (pasárselo daría el ticket de `wsfe` con otro nombre).
  3. **Las respuestas.** `ValueError` del prestador pidiendo un servicio que no es `wsfe` («no se presta»): **501
     `ticket_no_prestado`**. `NodoSinArca` (o cualquier excepción con `detalle()`): **422**, como hoy. `ValueError` o
     `LookupError` pidiendo `wsfe` (un emisor que no existe): 422 `ticket_no_disponible`, como en ADR-009 (el motor no levanta
     `ValueError` por `wsfe`, y cambiarle el mapeo a quien ya lo usaba no aporta nada). Cualquier otra: 502. Nunca se usa el
     texto de la excepción ni se loguea el ticket; el log dice el servicio.
  4. **El ticket dice de qué servicio es.** La respuesta lleva `servicio` (los de `wsfe` de un prestador de antes lo
     reciben del router) y, si el prestador lo trae, `ambiente`: el emisor de CPE llama a ARCA con **ese** ambiente. Si el
     prestador devuelve el ticket de **otro** servicio, o uno de `wscpe` sin decir su servicio, es **502
     `ticket_de_otro_servicio`**: no se presta.
  5. **La caché de `ClienteDeTicket` es por `(emisor_id, servicio)`**, y `olvidar(emisor_id=None, servicio="wsfe")` también;
     cada servicio vence por su cuenta. Sin `servicio`, `pedir_ticket`, `pedir_ticket_async` y `olvidar` son los de siempre
     (`wsfe`). El pedido del nodo lleva `servicio` en el cuerpo. **El cliente comprueba qué ticket volvió**: sin `servicio`
     es de `wsfe` (un central anterior a v0.22 ignora el pedido y no lo dice); si se pidió otro, `TicketRechazado`
     `ticket_de_otro_servicio` y **no se guarda** (guardarlo serviría el equivocado hasta que venza). El motor repite la
     comprobación en `ticket_prestado`: dos cinturones.
  6. **`ClienteDeTicket.para_servicio(sin_conexion=)`** es la `obtener_ta(arca, servicio="wsfe")` que esperan
     `libracore.nodo_offline.ticket_prestado` y `libracore.cola_arca.ColaDeArca`: pide el ticket de `arca["id"]` y del servicio
     que se le diga (`ticket_prestado` la llama `obtener_ta(arca)` para `wsfe` y `obtener_ta(arca, servicio="wscpe")` para los
     demás). `para_numerador` se conserva y es lo mismo (sirve con un solo parámetro).
  7. **Los errores se traducen como hoy.** `SinConexionConElCentral` y el 409 de esquema → la `SinConexion` del producto
     (corte: se reintenta al volver); `TicketRechazado` → rechazo (`NodoSinArca` / `RechazoDelCentral`). `TicketRechazado`
     ahora lleva `rechazo_del_central = True`, la marca con la que el motor (`es_rechazo`) distingue un rechazo de un corte:
     aunque el producto no la traduzca a mano (como hacía `obtener_ta_del_central` de VentaLibra), `ticket_prestado` la
     clasifica bien. Pero `ticket_prestado` reemplaza el mensaje de un rechazo que no es `NodoSinArca` por uno genérico
     («nodo revocado o sin credencial»), y se perdería el motivo del central («no tiene el certificado de wscpe», «actualizá
     LibraEdge»): por eso `para_servicio(sin_conexion=, rechazo=)` acepta la excepción del producto para el rechazo
     (`RechazoDelCentral`) y la levanta con el mensaje del central. No cambia nada de lo que ya andaba.
  8. **La subida del estado de la cola: la operación `cola.pedido`, y su aplicador es del producto.**
     `ColaDeArca.al_cambiar(payload)` (con `payload = libracore.cola_arca.payload_de_pedido(pedido)`) encola una operación del
     outbox del nodo; el central la aplica con `libracore.cola_arca.aplicar_pedido_del_nodo(payload, conn=conn)`. No va en
     LibraEdge: el aplicador necesita `libracore` y LibraEdge no lo importa en runtime (el pin es sólo para tests), y los que
     tienen el `Despachador` **y** el motor juntos son los productos (en la familia, `libracommerce.erp.offline.registrar_en`
     hace eso con las operaciones de venta, pero LibraCargo no usa LibraCommerce). LibraEdge no cambia: una operación es un
     `operation_type` más y el `Despachador` ya registra por `(tipo, versión)`. **El contrato**, para LibraCargo (PR 9) y
     LibraClub (PR 11):
     - *Nodo, al encolar:* `OutboxOperation(operation_id=f"{node_id}:{seq}", node_id=node_id, sequence=seq,
       operation_type="cola.pedido", aggregate_type="cola_arca",
       aggregate_id=f"{node_id}:cola_arca:{payload['id_en_el_nodo']}", occurred_at=<UTC ISO>, schema_version=1,
       payload=payload)`, con `seq = repo.next_sequence()` y `NodeRepository(conn).enqueue_operation(op)`. `al_cambiar` corre
       **después de confirmar** el cambio de la cola (ADR-079, punto 8) y no tiene transacción propia: abre la suya, y si
       falla se registra y no deshace el comprobante (ya está emitido en ARCA). El outbox sube en orden causal (ADR-020), así
       que la `revision` N llega antes que la N+1; si llegara desordenada, `aplicar_pedido_del_nodo` sólo pisa con una
       `revision` mayor. Un `payload` tiene que ser JSON estricto (`json.dumps` lo serializa: el outbox usa
       `sort_keys=True`).
     - *Central, al aplicar:* `despachador.registrar("cola.pedido", 1, lambda conn, op: aplicar_pedido_del_nodo(op.payload,
       conn=conn))`. La `conn` del manejador es la del router (`libracore.db.core.get_connection`): en LibraCargo y LibraClub
       dominio y motor son la misma base, así que la copia de consulta entra en la misma transacción que el `sync_inbox`.
       Un `ValueError` (versión de payload que no entiende, o `origen_instancia` vacío) deja la operación en revisión
       manual, que es lo que tiene que pasar. No suma ninguna ruta ni fila a la matriz de roles: va por `POST /sync/v1/push`.
- Descartadas (parte A):
  - **Llamar siempre al prestador con `servicio=` y atrapar el `TypeError` de los viejos**: un `TypeError` de adentro del
    prestador se leería como «prestador viejo». Se mira la firma.
  - **Pasarle `servicio` a un prestador que no lo acepta y confiar en el ticket que devuelva**: sin la costura no hay forma de
    que ese prestador dé el de `wscpe`; contestarle 501 es lo honesto.
  - **Que la lista de servicios se valide en LibraEdge** (`wsfe`, `wscpe`): cada servicio nuevo obligaría a publicar una versión
    de LibraEdge; el motor ya tiene su lista cerrada (`SERVICIOS_PRESTABLES`) y es quien tiene que decidir qué se presta.
  - **`olvidar(emisor_id)` que borre todos los servicios del emisor**: cambia lo que hacía; quien quiere olvidar `wscpe` lo dice.
  - **Registrar el aplicador de `cola.pedido` en LibraEdge**: lo ataría a `libracore` (ver 8).
- Consecuencias (parte A): **MINOR (v0.22.0)**. Sin cambio de esquema del nodo ni del central, y sin ruta nueva (la matriz de
  roles de los productos no cambia: `POST /sync/v1/ticket-arca` ya es «PÚBLICA, secreto del nodo»). Nodo v0.21 con central
  v0.22: el nodo no manda `servicio`, es `wsfe`, todo igual. Nodo v0.22 con central v0.21: `wsfe` sigue andando (el central
  ignora el campo); `wscpe` es un `TicketRechazado` `ticket_de_otro_servicio` con «actualizá LibraEdge en el central» (el motor
  lo deja `esperando` con ese motivo si el producto pasó `rechazo=`, ADR-079): **se despliega primero el central**, como en todo el plan (motores → central →
  nodos). El ticket sigue siendo una credencial: no se loguea, no va en ningún mensaje, `Cache-Control: no-store`, sólo en memoria.

### Parte B — LibraCargo y LibraClub en el instalador y en la app de escritorio

- Decisión:
  1. **Una definición por producto**, como ADR-010: `instalador/productos/libracargo.json` y `libraclub.json` (formato
     `libraedge-producto/1`), con `app_id` nuevos y fijos (los del nodo, y los de puesto en `puesto.iss`), `asgi`
     `app.asgi:app`, puerto 8000, salud `/health` (los dos la sirven en `main`), `base_auth: dominio` y los mismos `extras`. Las
     filas de `nodo.iss` y `puesto.iss`, el `PRODUCTOS` de `instalar-nodo.sh` y las cinco matrices del workflow
     `publicar-instalador.yml` (más la compilación de la app) suman los dos productos. **La app de escritorio (Tauri) no
     cambia**: `build.rs` toma nombre, puerto, salud, iconos y marca de la definición, así que alcanza con la definición y los
     archivos de marca.
  2. **Una sola base, y sólo una.** Cada definición tiene **una** entrada en `bases`, la del nodo (`nodo: true`), con las dos
     variables que escribe el alta de libracore, `<P>_DATABASE_URL` y `<P>_LIBRACORE_DATABASE_URL`, **con la misma URL**: es lo
     que ya hace VentaLibra y lo que aceptan los dos productos (LibraCargo exige que, si la variable está, apunte a la del
     dominio; LibraClub la trata como la del dominio si es la misma). No hay `libraclub_core` ni `libracargo_core`
     (`libracore-unir-bases`, ADR-078): un test lo ata y el que declaraba `libraclub_core` pasó a usar un producto de juguete,
     porque lo que prueba es que el formato admite varias bases, no que LibraClub las tenga.
  3. **Las migraciones son las del `scripts/panel_admin.py` de cada producto, en su orden**, y el nodo corre las mismas que el
     central. LibraCargo: `libracore-migrar`, `libraauth-migrar --base dominio` y `alembic` (no usa LibraCommerce). LibraClub:
     `libracore-migrar`, **`libracommerce-migrar` antes de `libraauth-migrar`** (el buffet es de LibraCommerce; es el orden que su
     `panel_admin.py` midió sobre una base vacía) y `alembic` (tabla `alembic_version_libraclub`). Por eso
     `test_cada_definicion_del_repo_es_valida` ya no supone las cuatro cadenas de siempre: fija la de cada producto.
  4. **La marca** de cada uno son los dos SVG de `marcas/` de libra-ui tal cual (`main`, `9af3938`); los `.ico` y los `.png` salen
     de `armar_icono.py` (Chrome headless), sólo para los dos productos nuevos. Las marcas de los otros tres son de la versión
     anterior de libra-ui (planas, sin degradé) y no se regeneraron: queda para quien toque esos productos.
  5. **Sin `LIBRAEDGE_CARGA_INICIAL`** (la de existencias de LibraCommerce, ADR-023): ninguno de los dos trae stock por sucursal. Si
     el producto necesita una carga inicial propia (las órdenes y la caja de LibraCargo; el buffet de LibraClub), la declara en
     `variables` de su definición. `test_carga_inicial` distingue los productos con y sin existencias.
- Lo que tiene que exponer cada producto (**PR 9 de LibraCargo, PR 11 de LibraClub**) para que el nodo sincronice: LibraEdge no
  depende de nada más, pero **hoy ninguno de los dos tiene** `app/offline.py`, `Aprovisionamiento` ni la dependencia de
  `libraedge` (ni LibraCargo ni LibraClub la tienen en su `pyproject.toml`; en `develop`, LibraCargo pinea libracore v1.191.0 y
  LibraClub v1.190.1). El modelo es VentaLibra (`app/offline.py` y `app/routers/sincronizacion_offline.py`):
  1. **`pyproject.toml`/`uv.lock`:** `libraedge @ git+...@v0.22.0` y libracore v1.191.0 (`cola_arca`; LibraClub todavía está en
     v1.190.1). El instalador empaqueta lo que dice el `uv.lock` del producto.
  2. **`app/offline.py`** con `nodo()` (`libraedge.identidad.nodo_actual("LIBRACARGO")`), `en_linea()` (`libracore.nodo_offline.
     esta_en_linea`), `CONEXION`, y **`aprovisionamiento = Aprovisionamiento(prefijo=..., tablas_de_referencia=..., listar_
     sucursales=..., verificar_alta=sin_guarda, conectar=...)`** (`LIBRAEDGE_APROVISIONAMIENTO=app.offline:aprovisionamiento`).
     `conectar` abre `url_de_instancia("<producto>", requerida=True)` (una sola base). `tablas_de_referencia` es el reparto de
     autoridad: lo que el **nodo** genera (en LibraCargo, órdenes, caja, asientos, facturas y CPE; la cola `cola_arca` es suya y
     no se espeja) **no** se publica. Nunca se espeja `arca_credenciales_servicio` ni el almacén de claves: el ticket lo presta el
     central.
  3. **`app/routers/sincronizacion_offline.py`** con el `despachador` (`LIBRAEDGE_DESPACHADOR=app.routers.sincronizacion_
     offline:despachador`; las operaciones `cargo.*` / `club.*` de cada producto **y `cola.pedido` v1**) y `router =
     create_sync_router(get_connection, despachador=..., version_esquema=version_de_alembic, prestar_ticket=...)`. En LibraCargo,
     `prestar_ticket=lambda emisor_id, servicio="wsfe": ta_para_el_nodo(emisor_id, servicio=servicio, empresa="agencia")`; en
     LibraClub, `ta_para_el_nodo` a secas (no hay `wscpe`: pedirlo es 501).
  4. **El montaje en `app/main.py`:** ese router, `create_nodos_router(aprovisionamiento, get_connection)` (la pantalla «Sucursal
     sin internet») y `create_canje_router(...)` (el código de vinculación), más las filas de la matriz de roles: «PÚBLICA, secreto
     del nodo» para `libraedge.sync.api.RUTAS_AUTENTICADAS_POR_NODO` y ADMIN para `/api/nodos-offline*`. **Sin esto el instalador
     instala, pero no hay dónde obtener el código para vincular**: por eso publicar el instalador de estos dos productos antes de
     sus PR 9 y 11 no expone nada.
  5. **El cliente del ticket:** `cliente_desde_entorno(version_esquema=...)` y `obtener_ta = cliente.para_servicio(
     sin_conexion=SinConexion, rechazo=RechazoDelCentral)` para `ColaDeArca` y `numerador_con_ta`. **No** el envoltorio de un solo
     parámetro de `obtener_ta_del_central` de VentaLibra: la cola llama `obtener_ta(arca, servicio="wscpe")`.
  6. **La carga inicial,** si hace falta: `LIBRAEDGE_CARGA_INICIAL=modulo:funcion` en `variables` de la definición (un cambio de
     una línea en este repo) y `create_sync_router(..., carga_inicial=)`, que es síncrona y devuelve un `dict` de una sola
     respuesta: con ≈4.100 órdenes y movimientos (Suitrans) puede hacer falta paginarla (abierto, ver el plan).
- Descartadas (parte B):
  - **Dejar a los dos productos fuera de las matrices hasta sus PR 9 y 11**: el CI del instalador arma el paquete desde `main` del
    producto, instala, migra, arranca, abre la app y vincula contra un central de prueba (`tests/central_de_prueba.py`, un
    producto de juguete), nada de lo cual necesita `app/offline.py`. Sumarlos ahora da la primera medición real de las migraciones
    de LibraClub y de LibraCargo sobre PostgreSQL embebido en Windows, que no se puede hacer a mano acá.
  - **`continue-on-error` para los nuevos**: un instalador que no pasa no se publica, y eso tiene que verse en rojo.
  - **Una sola variable de base** (sin `<P>_LIBRACORE_DATABASE_URL`): hoy andaría con los dos productos en `main`, pero el alta de
    libracore sigue escribiendo las dos y un `main` anterior a libracore v1.190.1 exige la del core. Se puede sacar cuando los
    productos dejen de leerla (abierto).
  - **Regenerar los `.ico` de los otros tres productos** con las marcas nuevas: cambia archivos de tres productos que no se
    tocan en este PR.
- Consecuencias (parte B): el Release de v0.22.0 trae `libraedge-nodo-libracargo` y `-libraclub` (`.exe`, `.deb`) y los de puesto,
  y `instalar-nodo.sh` los acepta. **No se probó acá** (Windows, Inno Setup, runners de Ubuntu, Rust): lo que se corrió fueron los
  tests de las definiciones, de las filas del `.iss` y del workflow y de los iconos; la primera corrida de `Publicar instalador` en
  el PR es la medición. Mientras LibraCargo y LibraClub no cumplan el contrato de arriba, un nodo instalado es sólo el producto web
  sobre una PC local, sin nada que sincronizar.

## ADR-026 — Ids provisorios de texto: `"-N"` y `libraedge_mapeo_texto`

- Estado: propuesta
- Fecha: 2026-10-25
- Contexto: es el PR 7 del plan «GestioLibra, MedLibra y LibraDesk en la versión de escritorio»
  (`wiki/analyses/libraedge-gestiolibra-medlibra-libradesk-2026-10-24.md`). Las altas en el nodo (ADR-021) usan un id provisorio
  **entero negativo** y el central lo traduce al definitivo con `libraedge_mapeo`. libragenda (GestioLibra y MedLibra) tiene ids
  de **texto**: un uuid en `clients`, y `p-042` en los pacientes de MedLibra (que además es a la vez clave primaria y clave
  foránea de `clients.id`). El mismo mecanismo tiene que servir para texto sin tocar a los cinco productos que ya usan el de
  enteros (ContaLibra, RestoLibra, VentaLibra, LibraCargo y LibraClub). La guarda del espejo (ADR-019) ya dejaba pasar un texto
  `-N` (compara el texto con `^-[0-9]+$`).
- Qué ya existe (se miró antes de diseñar): `libraedge/mapeo.py` (`es_provisorio`, `traducir_id`, `anotar_mapeo`),
  `sync/reconciliacion.py` (que hacía `int()` sobre los dos ids), `MirrorApplier` y su barrido, la declaración por
  `Aprovisionamiento` → `registrar` → carga, y **cómo usan el mapeo los productos**: LibraCargo y LibraClub lo importan
  (`anotar_mapeo`, `traducir_id`, `es_provisorio`, `siguiente_id_provisorio`) **y además lo leen con SQL propio**
  (`libracargo:app/servicios/emisores.py:136`, `SELECT id_central FROM libraedge_mapeo WHERE ... AND id_nodo = :id` con un
  entero; `libraclub:app/servicios/caja.py:483`, `... WHERE id_central IN (?, ?, ...)` con enteros, dentro de un
  `except Exception` que devuelve «sin equivalentes»).
- Decisión:
  1. **El id provisorio de texto es `"-N"`** (un `-` y un número sin ceros a la izquierda, hasta 18 cifras). `es_provisorio`
     lo acepta además del entero negativo; `es_provisorio_de_texto` y `tabla_del_mapeo` dicen cuál es. Los enteros siguen
     exactamente igual (el test existente `test_es_provisorio` cambió una sola línea: `"-1"` ya no es «no provisorio»).
     `siguiente_id_provisorio(conn, texto=True)` da `"-1"`, `"-2"`, ... de **la misma secuencia** `local_sequences` (una sola
     por base: un número no se reusa aunque cambie la forma).
  2. **El mapeo de texto es otra tabla, `libraedge_mapeo_texto(node_id, tabla, id_nodo TEXT, id_central TEXT, creado_at)`**,
     misma clave `(node_id, tabla, id_nodo)`, que crea `init_schema()` (central y nodo, sin migración del producto). Qué mapeo
     se usa lo dice **el tipo del id que se pide**: `traducir_id(conn, nodo, tabla, -3)` va a `libraedge_mapeo` y devuelve un
     `int`; `traducir_id(conn, nodo, tabla, "-3")` va a `libraedge_mapeo_texto` y devuelve un `str`. Un texto que no es `"-N"`
     (un uuid, un `p-042`) es un id del central y vuelve igual. `anotar_mapeo` hace lo mismo (idempotente; un mapeo distinto
     para el mismo id es `MapeoEnConflicto`); el `id_central` de un mapeo de texto es un texto no vacío y no provisorio.
  3. **El reapuntado deja de convertir.** `reapuntar(conn, tabla, provisorio, definitivo)` recibe los ids del tipo de la clave
     (enteros o textos) y los pasa tal cual: PostgreSQL no compara un entero con una columna de texto, y el `int()` de antes
     rompía las tablas de texto. Valida con `es_provisorio` el primero y que el segundo no lo sea. Las claves foráneas (incluida
     la que es a la vez clave primaria, `patients.id`) y las referencias sin FK se reapuntan igual. `clave_es_entera(conn,
     tabla, clave)` lee el tipo de la columna del catálogo.
  4. **El espejo baja los dos mapeos.** `MirrorApplier` reapunta con enteros las filas que bajan por `libraedge_mapeo` y con
     textos las que bajan por `libraedge_mapeo_texto` (la tabla de la que viene la fila dice el tipo). El barrido de
     `reconciliar_pendientes` elige el mapeo por el tipo de la clave de cada tabla con alta; si el nodo no espeja el mapeo de
     texto, salta las tablas de texto.
  5. **Se declara por alta:** `AltaEnNodo("clients", referencias, id_de_texto=True)`. `Aprovisionamiento` suma entonces
     `libraedge_mapeo_texto` al espejo y a las de solo lectura (y `libraedge_mapeo`, como siempre), y `registrar` se niega a
     registrar el nodo si el central no puede publicarla. El indicador **no viaja** en `LIBRAEDGE_ALTAS_EN_NODO`: el nodo ve el
     tipo en su base. Sin `id_de_texto`, el espejo y la carga son los de v0.22.
- Desvío del diseño propuesto, y por qué: **`libraedge_mapeo` no pasa a `TEXT`.** El plan proponía guardar el id como texto en la
  misma tabla con una migración aditiva. No es aditiva para quien la lee con SQL propio: con `id_nodo` e `id_central` en `TEXT`,
  PostgreSQL contesta `operator does not exist: text = smallint` a las dos consultas de arriba (medido contra PostgreSQL 16.15 con
  la capa de LibraCore; con la tabla de enteros pasada a `TEXT` fallan además los tests existentes de `test_mapeo.py` en PostgreSQL), y la de
  LibraClub lo hace **en silencio** (según su código, el `except Exception` devuelve un solo id y los cobros de la reserva con
  id provisorio dejan de verse como suyos). Agregar una columna de texto nulable a la misma tabla evita eso pero cambia la forma de **cada** fila
  espejada (`to_jsonb(NEW)` incluye la columna nueva): un nodo de v0.22 la rechazaría con `column does not exist` en el
  primer mapeo que baje, y el central se actualiza antes que las PC. Una tabla aparte no cambia un byte de lo que existe, no
  cambia el payload de nadie, y se desarma con un `DROP`.
- Descartadas: pasar `libraedge_mapeo` a `TEXT` con `ALTER ... USING id_nodo::text` (rompe a LibraCargo y LibraClub, ver arriba;
  exigiría cambiar sus consultas en el mismo tag de pin); una columna `id_central_texto` en la misma tabla (el payload nuevo
  rompe los nodos de antes); convertir los ids por la forma (`"57"` → `57`) para decidir el tipo (un id de texto puede ser
  numérico; el catálogo y la tabla de origen lo dicen sin adivinar); un indicador en `LIBRAEDGE_ALTAS_EN_NODO` (cambia el
  formato de la carga, que el instalador valida con un parser propio, para algo que el nodo ya ve en su base).
- Consecuencias: **MINOR (v0.23.0)**: suma API (`es_provisorio_de_texto`, `tabla_del_mapeo`, `TABLA_TEXTO`, `clave_es_entera`,
  `siguiente_id_provisorio(texto=)`, `AltaEnNodo.id_de_texto`) y una tabla; **sin declarar `id_de_texto`, el comportamiento es el
  de v0.22** y los cinco productos no cambian. Una diferencia deliberada: `traducir_id` con un texto que **no** es `"-N"` lo
  devuelve tal cual (antes lo pasaba por `int()`, así que `"57"` daba `57`); ningún producto pasa textos de enteros, pero
  quien lo hiciera tiene que pasar el entero. Volver a v0.22 deja `libraedge_mapeo_texto` sin leer (`DROP TABLE` la saca; los
  mapeos de texto se pierden, pero esos productos no existen en v0.22). **Lo que no cubre:** que el central deduplique, asigne
  el id definitivo (uuid, el próximo `p-NNN`) y reapunte `patients.id` cuando la ficha definitiva **ya bajó por el espejo** es del
  producto (libragenda, PR 8 a 10): `reapuntar` mueve la fila hija con la clave primaria igual a la FK (probado), pero si el nodo
  ya tiene una fila `patients` con el id definitivo, la clave primaria debería chocar y el reapuntado quedar en
  `sin_reconciliar` con su aviso (inferido, **no probado**: a verificar con la ficha real de MedLibra).

## ADR-027 — GestioLibra y MedLibra en el instalador y la app de escritorio

- Estado: propuesta
- Fecha: 2026-10-25
- Contexto: es el PR 12 del plan «GestioLibra, MedLibra y LibraDesk en la versión de escritorio»
  (`wiki/analyses/libraedge-gestiolibra-medlibra-libradesk-2026-10-24.md`). Los dos productos adoptan el nodo en sus ramas
  `feat/modo-sin-conexion` (ADR-036 de GestioLibra y ADR-045 de MedLibra, PRs 9 y 10) sobre `libraedge` v0.23.0 (ADR-026, los ids
  de texto). Esta es la parte que les toca a ellos acá: la definición del instalador, la app de escritorio, los íconos y, para
  MedLibra, el aviso de que la PC guarda la historia clínica.
- Qué ya existe (se miró antes): el molde de LibraCargo y LibraClub (ADR-025, parte B; libraedge#164, #167 a #169): una definición
  en `instalador/productos/`, una fila con `AppId` nuevo en `nodo.iss` y en `puesto.iss`, `PRODUCTOS` de `instalar-nodo.sh`, las
  marcas de libra-ui y sus íconos, y la app de escritorio, que sale de la definición y no cambia.
- Decisión:
  1. **`instalador/productos/gestiolibra.json` y `medlibra.json`**, de una sola base: `<P>_DATABASE_URL` y `DATABASE_URL` con la
     misma URL (la segunda la leen `libragenda-migrar` y `migrations/env.py` en `alembic upgrade head`). **Sin**
     `<P>_LIBRACORE_DATABASE_URL`: ya no existe (`_UNA_SOLA_BASE` desde libracore v1.194.0, ADR-083). Sin `LIBRAEDGE_CARGA_INICIAL`
     (no tienen existencias). `app.asgi:app`, puerto 8000, `/health`, `version_esquema: alembic`, extras como los otros.
  2. **Cuatro cadenas, las de `scripts/panel_admin.py` de cada producto, en ese orden:** `libragenda-migrar upgrade` (primero: las
     revisiones propias tienen claves foráneas a las tablas de LibraGenda), `libraauth-migrar upgrade --prefijo <p> --base dominio`,
     `libracore-migrar upgrade --prefijo <p>` y `alembic upgrade head`. `base_auth` es **`dominio`**: el validador de la
     definición exige que diga lo mismo que el `--base` del comando, y desde gestiolibra ADR-037 y medlibra ADR-046 (2026-10-25)
     los dos productos migran libraauth con `--base dominio` en su `panel_admin.py`. Antes se había puesto `core` (con `--base core`
     la instancia que todavía tiene la variable del core migra la suya y, sin ella, cae a la del dominio); con la base única no
     hay variable del core y el destino es el mismo, pero la definición dice lo que dicen los productos. Sólo VentaLibra queda
     con `core`.
  3. **`app_id` nuevos** (uno por producto, para siempre) y `AppIdPuesto` nuevos en `puesto.iss`. Marcas e íconos desde libra-ui
     v0.181.0 (`marcas/gestiolibra*.svg` y `medlibra*.svg`), armados con `armar_icono.py`.
  4. **Todavía no se publican.** Las dos apps no están en `main` (su `main` no trae `libraedge`): entran a las definiciones y a los
     tests, **no** a las matrices de `publicar-instalador.yml` ni a la lista de `armar.py` del workflow, porque sin `libraedge`
     el Python del nodo no puede importarlo y el instalador no pasa la prueba (el mismo motivo de libraedge#164). `SIN_PUBLICAR`
     de los tests de Linux y de escritorio los nombra, y un test fija que sus ids no aparecen en el workflow. Al publicarlos se
     sacan de `SIN_PUBLICAR` y se suman a las cinco matrices y a la lista de `armar.py`.
  5. **MedLibra: la historia clínica queda en la PC** (decisión del humano, 2026-10-21 y 2026-10-25; ADR-045 de MedLibra: disco
     cifrado y clave de Windows). El instalador **no implementa cifrado ni lo comprueba**. Lo que sí hace es decirlo: una página
     del asistente de Windows antes de instalar (`InfoBeforeFile=avisos\medlibra.txt` en la fila de MedLibra de `nodo.iss`), en
     lenguaje simple: cifrar el disco (BitLocker), un usuario de Windows con clave por persona, la PC dentro del consultorio,
     respaldos en discos cifrados, y que desinstalar **no** borra los datos (quedan en `C:\ProgramData\LibraEdge`: hay que borrarlos
     antes de entregar la PC). Está documentado en `instalador/README.md`. En una instalación desatendida la página no se muestra.
- Descartadas: comprobar BitLocker desde `instalar.ps1` y negarse a instalar sin él (cambia el comportamiento de un instalador que
  corre desatendido en VMs y CI, y el humano pidió el aviso, no la obligación); cifrar la base o los respaldos del nodo (otra
  decisión de arquitectura, no de este PR); un aviso por producto para los demás (no guardan historia clínica).
- Consecuencias: **MINOR (v0.24.0, a decidir al taggear)**: suma dos productos y una página del asistente; no toca el esquema ni
  ninguna ruta. **No se probó acá** (Windows, Inno Setup, runners de Ubuntu, Rust): lo que se corrió fueron los tests de las
  definiciones, de las filas de los `.iss`, del workflow, de los íconos y del aviso; la compilación de `nodo.iss` con
  `InfoBeforeFile` y la prueba en VM del instalador de MedLibra (PR 12 del plan) siguen abiertas. Para publicarlos falta que
  GestioLibra y MedLibra lleguen a su `main` (con libraedge v0.23.0 y su router de nodos); **no** verificado: que `dar-de-baja`
  borre la base local de la PC.
