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

- Estado: aceptada
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
     **«Otras cajas: http://<IP>:<puerto> (copiar)»**, que al hacer clic copia la dirección (`clip.exe`; el portapapeles
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
  su configuración inicial la primera vez. En Windows, «Abrir» y «Otras cajas» de la bandeja llegan cuando los
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
