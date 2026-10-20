# Instalador del nodo LibraEdge (Windows y Linux)

**Un solo instalador para todos los productos** (ADR-010, E7 de la fase 1): se compila por producto con
`ISCC /DProducto=<producto> nodo.iss` y lo propio de cada uno vive en `productos/<producto>.json`. Hoy hay
definiciones para **RestoLibra, VentaLibra y ContaLibra**.

> ⚠️ **Estado.** La versión anterior, atada a RestoLibra, se probó entera en una VM entre el 2026-08-30 y el
> 2026-08-31 (historia abajo). **Esta versión parametrizada todavía no corrió en Windows**: lo verificado es el
> parser de Windows PowerShell 5.1 sobre los seis `.ps1`, las funciones de `comun.ps1` corridas en 5.1 y los tests
> del ayudante (`tests/test_instalador.py`). Lo que falta, en orden, está en [`PROBAR_EN_VM.md`](PROBAR_EN_VM.md).

## Qué instala

En la PC designada del local (Windows 10/11 x64, una sola por sucursal; las cajas son navegadores en la red interna):

1. **PostgreSQL 16** desde el ZIP binario oficial (no el instalador de EDB), como servicio `LibraEdgePostgres`, sólo
   en `127.0.0.1:55432`, con `initdb` en ICU `en-US`, zona `America/Argentina/Buenos_Aires` y `--data-checksums`.
2. **El producto** sobre un **Python 3.12 embebido**, como servicio `LibraEdgeProducto` (`uvicorn <asgi> --port 8000`),
   y el **proceso del nodo** como `LibraEdgeNodo` (`python -m libraedge correr`). Los dos con NSSM.
3. La tarea programada `LibraEdgeRespaldo` (cada hora, con verificación), la regla de firewall del puerto del producto
   (perfiles Privado y Dominio) y el ícono de bandeja en el inicio común.

4. **El acceso directo «<Producto>»** en el escritorio público y en el menú Inicio (ADR-013), con la marca del producto:
   abre `http://localhost:<puerto>/` en **modo aplicación de Edge** (`msedge.exe --app=...`: una ventana sin barra de
   direcciones, con el título y el ícono de la página). Sin Edge, la URL en el navegador por defecto. Lo quita la
   desinstalación.

## En el local se entra siempre por la copia local (ADR-013)

En la PC con el nodo **se entra siempre por la copia local, aunque haya internet**: el nodo sincroniza por detrás. Quien
entra por la nube desde el local (`demo.contalibra.com.ar`) se queda sin sistema cuando se corta internet, y tiene que
abrir `localhost:8000` y volver a loguearse (VM, 2026-10-20). Por eso:

- **En esa PC**, el acceso directo «<Producto>» (Windows: escritorio y menú Inicio; Ubuntu: el menú de aplicaciones,
  desde donde se ancla al dock con «Agregar a favoritos»). También `http://localhost:8000`.
- **Desde las otras cajas del local**, `http://<IP de esa PC>:8000`. El final de la instalación lo muestra con la IP ya
  completa (Windows: la página final del asistente, `logs\instalacion.log` y el log de Inno; Linux: la salida de
  `libraedge-nodo-instalar` y de `instalar-nodo.sh`), y la bandeja lo repite en su menú («Otras cajas: http://… (copiar)»).
  La IP es la privada de la ruta por defecto (`libraedge_instalador.py acceso`).
- **La dirección de la nube** es para entrar desde afuera del local.
- «Abrir <Producto>» de la bandeja usa el mismo lanzamiento que el acceso directo.

**Abrirlo al iniciar sesión** (opcional, apagado por defecto). Windows: copiar el acceso directo del menú Inicio al
inicio común (`copy "%ProgramData%\Microsoft\Windows\Start Menu\Programs\<Producto>.lnk" "%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp\"`,
como administrador). Ubuntu: `sudo cp /usr/share/applications/libraedge-nodo-<producto>.desktop /etc/xdg/autostart/`.
Desinstalar borra la copia en los dos.

Los íconos: `instalador/iconos/<producto>.ico` (Windows) y `instalador/marca/<producto>.svg` (Linux) salen de las marcas
de libra-ui (`marcas/<producto>.svg` y `-favicon.svg`) con `python instalador/armar_icono.py <chrome>`, que arma también
`libraedge.ico`. Los `.ico` se commitean.

## La versión de escritorio: la app del producto (ADR-015)

El acceso directo «<Producto>» abre **la app de escritorio del producto** («ContaLibra», «VentaLibra», «RestoLibra»):
una ventana propia sobre `http://localhost:<puerto>/`, con el nombre y el ícono del producto. Es la «versión de
escritorio» de las webs. El código está en [`escritorio/`](../escritorio) (Tauri 2), uno solo para los tres productos;
cada uno se compila aparte con `python escritorio/armar.py <producto>`.

- **Ventana:** se abre maximizada la primera vez y después recuerda tamaño y posición. Abrir el acceso directo con la
  app abierta la trae al frente (una sola por usuario). Ctrl + y Ctrl − cambian el tamaño del texto.
- **Si el sistema no responde** (la PC está arrancando, se reinició el servicio): muestra «Conectando con
  <Producto>…» y vuelve sola, a la misma pantalla, cuando responde.
- **Vincular con el sistema:** si el nodo se instaló sin vincular, la app pide la dirección y el código de
  «Sucursal sin internet» y configura el nodo; Windows pide permiso de administrador (UAC) y Ubuntu la clave de un
  administrador (pkexec). Es el mismo configurador del instalador, y el código no queda en ningún log.
- **Pantalla completa** (cajas dedicadas): **F11** la prende y la apaga y se recuerda. Para dejarla fija desde el
  acceso directo: agregar `--pantalla-completa` al destino (Windows: Propiedades del acceso directo; Ubuntu: copiar el
  `.desktop` a `/etc/xdg/autostart/` o a `~/.local/share/applications/` con `Exec=... --pantalla-completa`);
  `--ventana` la apaga. Con «abrirlo al iniciar sesión» (arriba), la caja arranca directo en el sistema.
- **La sesión se conserva** al cerrar y abrir la app (hasta que vence por inactividad o se cierra sesión).
- **Los enlaces externos** (ARCA, Mercado Pago, las ayudas) se abren en el navegador del sistema; las descargas van a
  Descargas.
- **El navegador queda de respaldo:** si la app no puede abrir su ventana, abre la copia local en el navegador. En
  Windows, además, «<Producto> (navegador)» en el menú Inicio (Edge en modo aplicación), y si la PC no tiene el runtime
  de WebView2 el acceso directo principal es ése. En Ubuntu, clic derecho en el ícono del dock → «Abrir en el
  navegador».

Dónde guarda lo suyo (por usuario; desinstalar no lo borra, como el perfil de un navegador): Windows
`%LOCALAPPDATA%\ar.com.librasuite.<producto>\` (la sesión del webview) y `%APPDATA%\ar.com.librasuite.<producto>\`
(`preferencias.json`); Ubuntu `~/.local/share/ar.com.librasuite.<producto>/` y `~/.config/ar.com.librasuite.<producto>/`.

**Firma de código:** todavía no hay certificado, así que Windows muestra «Editor desconocido» en SmartScreen y en UAC.
Queda preparada: con un certificado, se carga el secreto `LIBRAEDGE_FIRMA_CMD` (por ejemplo
`signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f cert.pfx /p <clave> {archivo}`; `armar.py`
firma cada `.exe` de la app) y se compila el instalador con `ISCC /DFirmar "/Sfirma=signtool sign ... $f"` (firma el
instalador y el desinstalador).

Sin Docker Desktop: su licencia para empresas no es gratuita y es una capa más que soportar en la PC de un comercio.

**Un nodo por PC.** Servicios, puerto y carpeta de datos son unos solos: si la PC ya tiene el nodo de otro producto
(`C:\ProgramData\LibraEdge\producto.txt`), el instalador no arranca.

| Dónde | Qué | ¿Lo borra desinstalar? |
|---|---|---|
| `C:\Program Files\LibraEdge\` | Python, producto, PostgreSQL, scripts, la app (`escritorio\<Producto>.exe`), `nodo.env` (ACL: SYSTEM y Administradores), `estado.json`, `logs\` | sí |
| `C:\ProgramData\LibraEdge\datos\` | el clúster de PostgreSQL | **no** |
| `C:\ProgramData\LibraEdge\postgres.clave` | la clave del superusuario (generada, ACL restringida) | **no**: sin ella los datos no se abren |
| `C:\ProgramData\LibraEdge\app\` | `DATA_DIR` del producto: `config.json` del emisor, backups, errores (ACL restringida) | **no** |
| `C:\ProgramData\LibraEdge\respaldos\` | los volcados de `LibraEdgeRespaldo` (ACL restringida) | **no** |

## La definición del producto

`productos/<producto>.json` (formato `libraedge-producto/1`; las claves que empiezan con `_` son comentarios, una clave
desconocida es un error):

| Clave | Qué es |
|---|---|
| `producto`, `nombre`, `app_id` | id en minúsculas, nombre visible y el GUID de Inno Setup (**fijo para siempre**: es lo que hace que reinstalar actualice). El de RestoLibra es el de siempre. |
| `prefijo_variables` | `<PRODUCTO>` en mayúsculas: `<PRODUCTO>_DATABASE_URL`, `<PRODUCTO>_ADMIN_PASSWORD` |
| `bases` | una o varias: `{nombre, variables, nodo}`. Cada variable apunta a esa base; la que lleva `nodo: true` es también `LIBRAEDGE_DATABASE_URL` (donde vive el outbox). Hoy las tres son de una sola base con `<P>_DATABASE_URL` y `<P>_LIBRACORE_DATABASE_URL`, como escribe el alta de libracore. |
| `base_auth` | el `--base` de `libraauth-migrar` (`dominio` en RestoLibra y ContaLibra, `core` en VentaLibra); se valida contra `migraciones` |
| `migraciones` | los comandos, en orden: **los mismos** de `configure(migraciones=...)` en `scripts/panel_admin.py` del producto. Hoy cuatro cadenas: libracore, libraauth, libracommerce y la del producto |
| `asgi`, `puerto`, `salud` | `app.asgi:app`, `8000` y `/health` (la salud tiene que devolver un objeto JSON) |
| `extras` | requisitos de pip que el nodo suma a los del producto: `tzdata` (zoneinfo en Windows no trae la base de zonas) y `pystray`/`Pillow` (bandeja) |
| `variables` | variables fijas de más para el `nodo.env` (hoy ninguna) |
| `version_esquema` | `LIBRAEDGE_VERSION_ESQUEMA` (`alembic`) |

La tabla de `nodo.iss` (nombre y `AppId` por producto) tiene que coincidir con los `.json`: lo verifica un test.

## La carga del nodo

Lo que el central emite para **un** nodo es la **carga** (formato `libraedge-carga/1`). Desde v0.14 (ADR-014) llega al
instalador como **dirección + código de vinculación**: el administrador da de alta el nodo en «Sucursal sin internet»,
la pantalla muestra, por ejemplo, «Dirección: sistema.contalibra.com.ar · Código: K7Q4-M2XP», y el instalador los pide
y canjea el código por la carga (`libraedge_instalador.py canjear`, https con verificación de certificado, contra
`POST /sync/v1/vinculacion` del central). El código vale **15 minutos y una sola vez**; si vence o se usó, se pide otro
en la misma pantalla. El secreto del nodo no se muestra nunca: sale recién en la respuesta del canje, que el ayudante
escribe con permisos de secreto (como el archivo) y se borra al terminar bien.

**La alternativa para soporte es el archivo** `carga-nodo.json`: se baja de la misma pantalla («Archivo de carga») o lo
escribe el mismo comando que registra el nodo, adentro del contenedor del cliente:

```bash
export LIBRAEDGE_APROVISIONAMIENTO=app.offline:aprovisionamiento
libraedge-central nodo registrar <node_id> --sucursal <id o nombre> \
    --carga /app/data/carga-nodo.json --central-url https://<cliente>.<producto>.com.ar
```

Trae `LIBRAEDGE_CENTRAL_URL`, `LIBRAEDGE_NODE_ID`, `LIBRAEDGE_NODE_SECRET`, `LIBRAEDGE_PREFIJO` y
`LIBRAEDGE_TABLAS_ESPEJO`, la sucursal, y **los datos del emisor** copiados del `config.json` de la instancia. Con
`--carga`, `registrar` no imprime el secreto; el archivo se crea con permisos 600 y no pisa uno existente. Se lleva a la
PC del local (scp, pendrive) y **se borra de los dos lados después de instalar**. Ver `carga-de-prueba.json` (datos
ficticios, para la VM).

**El `config.json` del emisor viaja en la carga** y no se baja del central en la instalación: se toma en el mismo paso
que ya emite el secreto (sin una ruta nueva en el central ni credenciales en el instalador), la instalación no
necesita internet, y es una lista cerrada de claves (`empresa_*` y `ticket_*`): las credenciales de MercadoPago y SMTP y
el estado del servicio no viajan nunca. El instalador las **fusiona** en `C:\ProgramData\LibraEdge\app\config.json`.
Lo que no hace: seguir los cambios del central (si cambia la razón social, se reinstala con una carga nueva o se edita a
mano). Un CUIT con dígito verificador inválido se rechaza al registrar, antes de emitir el secreto.

## Compilar

Se arma en dos lugares:

1. **El paquete, en Linux (el WSL):** `armar_paquete.sh <producto> <repo del producto>` baja el Python embebido
   (3.12.10), PostgreSQL 16 (ZIP de EDB), NSSM 2.24 y el runtime de Visual C++, e instala el producto **para Windows**
   con `uv pip install --python-platform x86_64-pc-windows-msvc` desde su `uv.lock`. Necesita `uv`, `git` con acceso a
   los repos privados de la familia, `curl`, `unzip`, y `npm` con acceso a `libra-ui` para el frontend (o
   `--frontend-dist RUTA` / `--sin-frontend`). Usa el `HEAD` del repo del producto: hacer antes el checkout de la
   versión que se quiere (por ejemplo un worktree de `origin/develop`). Deja todo en `instalador/paquete/`
   (ignorado por git). Las versiones se cambian con `PYTHON_VERSION`, `POSTGRES_VERSION` y `NSSM_VERSION`.
2. **El `.exe`, en Windows:** **Inno Setup 6.3 o superior** (por `x64compatible`) y, en esta carpeta con `paquete\` al
   lado:

   ```powershell
   & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DProducto=ventalibra /DVersion=0.10.0 nodo.iss
   ```

   Sale en `salida\libraedge-nodo-ventalibra-0.10.0.exe`. `/DVersion` es la del tag de LibraEdge (por defecto la que
   dice el `.iss`). Hace falta la carpeta `libraedge\` del repo al lado de `instalador\`: el ayudante se copia de ahí.
3. **La app de escritorio (ADR-015)**, en el sistema donde va a correr, con Rust estable (`rustup`):
   `python escritorio/armar.py ventalibra` deja `escritorio/salida/ventalibra.exe` (Windows) o
   `escritorio/salida/libraedge-nodo-ventalibra` (Ubuntu; hacen falta `libwebkit2gtk-4.1-dev`, `libgtk-3-dev`,
   `libsoup-3.0-dev`, `build-essential` y `pkg-config`). Para Windows se copia a `paquete\escritorio\ventalibra.exe`
   antes de ISCC; para el `.deb`, `armar_deb.sh ... --escritorio escritorio/salida`. Sin la app, los dos instaladores
   salen igual y el acceso directo abre el navegador. El CI la compila en `windows-latest` y `ubuntu-22.04`.

## Instalar

- **Con el asistente:** pide la carpeta, y en «Vincular con el sistema» la **dirección** y el **código** que muestra
  «Sucursal sin internet». Si soporte dio un archivo de carga, se dejan vacíos y se elige el archivo en la página
  siguiente. Nada más: la clave de PostgreSQL ya no se tipea (se genera la primera vez y queda en `postgres.clave`).
- **Desatendida, con el código** (en la variable `LIBRAEDGE_CODIGO`, que no queda en ningún log):

  ```powershell
  $env:LIBRAEDGE_CODIGO = "K7Q4-M2XP"
  .\libraedge-nodo-ventalibra-0.14.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIRECCION=sistema.ventalibra.com.ar /LOG=C:\ruta\inno.log
  ```

  También anda `/CODIGO=K7Q4-M2XP`, pero **Inno Setup escribe su línea de comandos en el `/LOG`**: el código queda en
  ese archivo. Como sirve una sola vez, vence a los 15 minutos y el instalador lo canjea apenas termina de copiar, el
  riesgo es chico; con la variable no queda en ningún lado.
- **Desatendida, con el archivo (soporte):**

  ```powershell
  .\libraedge-nodo-ventalibra-0.14.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CARGA=C:\ruta\carga-nodo.json /LOG=C:\ruta\inno.log
  ```

  Sin código ni `/CARGA` (o con un archivo que no existe) aborta antes de tocar el disco.

**Ningún secreto pasa por una línea de comandos**: Inno Setup escribe la suya en `/LOG` y `Start-Transcript` la suya en
`logs\instalacion.log`. Viaja la **ruta** de la carga; el secreto lo lee `libraedge_instalador.py` y los mensajes nombran
variables, nunca valores. El código va del instalador a `instalar.ps1` y al ayudante en la variable `LIBRAEDGE_CODIGO`
del proceso; `instalar.ps1` lo canjea antes de tocar nada y deja la carga en `C:\ProgramData\LibraEdge\carga-nodo.json`
(sólo SYSTEM y Administradores) hasta terminar bien. Si algo falla después del canje, el código ya se usó: se reintenta
con `instalar.ps1 -Carga` sobre ese archivo, o con otro código.

**Códigos de salida:** `0` bien; `1` no arrancó (falta el código o `/CARGA`, u otro producto ya instalado); `20` se copiaron los
archivos pero la configuración falló (el motivo está en `C:\Program Files\LibraEdge\logs\instalacion.log`); el resto,
los de Inno Setup. Antes los pasos eran entradas de `[Run]`, cuyo código Inno no mira: un paso roto terminaba con 0.

**Reparar o reintentar** sin el instalador (todo es idempotente; conserva `SECRET_KEY` y la clave del admin local):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Program Files\LibraEdge\instalador\instalar.ps1" -RaizNodo "C:\Program Files\LibraEdge" -Carga C:\ruta\carga-nodo.json
```

### Qué hace `instalar.ps1`, en orden

1. Valida la carga contra el producto (otro producto, una variable de más, una URL sin https, un salto de línea en un
   valor: error **antes** de crear nada).
2. Un nodo por PC; el runtime de Visual C++ si falta.
3. `preparar_postgres.ps1`: clave (generada o leída), `initdb` sólo si no hay clúster, servicio, **las bases de la
   definición**, y verifica zona (`now()` en -03) y collation.
4. `preparar_nodo.ps1`: para los servicios si existían; escribe `nodo.env` (ACL primero, contenido después) y el
   `config.json` del emisor; corre **las migraciones de la definición**; hace **una primera sincronización antes de
   arrancar el producto** (si el espejo ya bajó los usuarios, el producto no crea un admin local; sin central sólo
   avisa); registra y arranca los servicios; firewall; respaldo; bandeja.

### El `nodo.env`

Lo arma `libraedge_instalador.py entorno` (probado en `tests/test_instalador.py`): las cinco variables de la carga,
`LIBRAEDGE_DATABASE_URL`, `LIBRAEDGE_ESTADO`, `LIBRAEDGE_VERSION_ESQUEMA`, las de cada base, `DATA_DIR`, `SECRET_KEY`,
`ADMIN_PASSWORD` y `<P>_ADMIN_PASSWORD` (aleatoria: sin ella unos productos no arrancan y otros la inventan y la
escriben en el log; nadie la usa porque los usuarios bajan del central), `TZ` y las fijas de la definición. En una
reinstalación conserva `SECRET_KEY` y la clave del admin: un `SECRET_KEY` nuevo deja ilegible lo que libraauth cifró.

## Verificar

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Program Files\LibraEdge\instalador\verificar_nodo.ps1" [-SinCentral]
```

Chequea los tres servicios (corriendo y automáticos), la ACL y las variables de `nodo.env` (sin mostrar valores),
PostgreSQL (`now()` en -03, `data_checksums`), las cadenas de migraciones contra la definición y `sync_outbox`,
`libraedge-nodo estado`, que el `estado.json` se escriba (el servicio cicla), que la salud del producto sea un objeto
JSON, el emisor en `config.json` y la tarea de respaldo. Sale con la cantidad de fallas. `-SinCentral` convierte «sin
conexión con el central» en aviso (carga de prueba o local sin internet).

## Agregar un producto

1. `productos/<producto>.json` con sus migraciones copiadas de `scripts/panel_admin.py` y sus variables de base.
2. Su fila en la tabla de `nodo.iss` (nombre y un GUID nuevo).
3. Que el producto haya adoptado el nodo (`app/offline.py`, ganchos, aprovisionamiento): sin eso el instalador instala
   un producto que no sincroniza.

## Archivos

| Archivo | Qué es |
|---|---|
| `nodo.iss` | Inno Setup: copia los archivos, corre `instalar.ps1`, devuelve su código; la desinstalación |
| `productos/*.json` | una definición por producto |
| `armar_paquete.sh` | arma `paquete/` desde Linux |
| `instalar.ps1` | lo único que ejecuta el `.iss`; orquesta lo de abajo con transcript |
| `preparar_postgres.ps1` | clave, `initdb`, servicio y bases |
| `preparar_nodo.ps1` | `nodo.env`, `config.json`, migraciones, primera bajada, servicios, firewall, respaldo, bandeja |
| `respaldo.ps1` | el respaldo de cada hora, verificado restaurándolo |
| `verificar_nodo.ps1` | la verificación post-instalación |
| `comun.ps1` | ACL por SID, programas nativos sin la trampa de stderr, leer el entorno |
| `../libraedge/instalador.py` | el ayudante (se instala como `libraedge_instalador.py`): definición, carga, entorno, `config.json`, correr un console script |
| `carga-de-prueba.json` | una carga ficticia para la VM |
| `nodo.env.ejemplo` | las variables del nodo, explicadas |
| `PROBAR_EN_VM.md` | los pasos para probarlo en una VM Windows 10 limpia |
| `linux/` | el `.deb` para Ubuntu 22.04/24.04 (ADR-012): ver la sección de abajo |

## Linux: Ubuntu 22.04 y 24.04 (ADR-012)

Un **`.deb` por producto**, `libraedge-nodo-<producto>_<version>_amd64.deb` (y el alias sin versión
`libraedge-nodo-<producto>_amd64.deb` para `releases/latest/download/`). Es el mismo instalador con otra capa de
sistema operativo: la definición del producto, la carga, el `nodo.env`, el emisor, las cuatro cadenas de migraciones,
la primera sincronización y la verificación son **las mismas**, por el mismo `libraedge/instalador.py`.

### Instalar

**En un paso**, con el script que se publica en el Release al lado de los `.deb` (y en `SHA256SUMS.txt`):

```bash
curl -fsSL https://github.com/marianocappucci/libraedge/releases/latest/download/instalar-nodo.sh \
    | sudo bash -s ventalibra --codigo K7Q4-M2XP --direccion sistema.ventalibra.com.ar
# o con el archivo (soporte): ... | sudo bash -s ventalibra /ruta/carga-nodo.json
sudo libraedge-nodo-verificar
```

`instalar-nodo.sh <producto> <carga>` controla que sea Ubuntu 22.04 o 24.04 amd64 y que corra como root, agrega el
repositorio de PGDG de la forma oficial (el script de `postgresql-common`: llave en un archivo y `signed-by`, sin
`apt-key`), baja `libraedge-nodo-<producto>_amd64.deb` del mismo Release, lo verifica contra su `SHA256SUMS.txt` y lo
instala **con las recomendadas** (la bandeja) y `LIBRAEDGE_CODIGO` + `LIBRAEDGE_DIRECCION` (o `LIBRAEDGE_CARGA`), que
el postinst le pasa a `libraedge-nodo-instalar` por el entorno. Ante cualquier falla sale con un código distinto de 0.
El secreto no pasa por los argumentos; el código sí, un momento (está en la línea de comandos de `bash -s` y, con
`sudo`, en el registro de `sudo` del sistema): sirve una sola vez y vence a los 15 minutos. Para no dejarlo ni ahí:
`sudo LIBRAEDGE_CODIGO=... libraedge-nodo-instalar --direccion ...` no lo saca del registro de `sudo`, pero
`export LIBRAEDGE_CODIGO=...; sudo --preserve-env=LIBRAEDGE_CODIGO libraedge-nodo-instalar --direccion ...` sí. Una versión fija: `sudo LIBRAEDGE_VERSION=0.12.0 bash -s
...`. En el CI se prueba con `LIBRAEDGE_DEB_DIR` (un directorio con el `.deb` y `SHA256SUMS.txt`) en vez de bajar.

**A mano**, en tres pasos:

```bash
# 1. PostgreSQL 16 de PGDG (en 22.04 es obligatorio: Ubuntu trae la 14).
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y

# 2. El paquete, con la carga del nodo (la emite el central: «Sucursal sin internet»).
sudo LIBRAEDGE_CARGA=/ruta/carga-nodo.json apt install ./libraedge-nodo-ventalibra_0.12.0_amd64.deb
#    o en dos pasos:
sudo apt install ./libraedge-nodo-ventalibra_0.12.0_amd64.deb      # instala SIN configurar (sale con 0 y lo explica)
sudo libraedge-nodo-instalar --codigo K7Q4-M2XP --direccion sistema.ventalibra.com.ar   # configura con el código
sudo libraedge-nodo-instalar /ruta/carga-nodo.json                  # o con el archivo (0 bien; 2 falta o no sirve)

# 3. Verificar.
sudo libraedge-nodo-verificar            # --sin-central con la carga de prueba o sin internet
```

- **Actualizar** es instalar el `.deb` nuevo encima: el `prerm` para el producto y el nodo, el `postinst` corre
  `libraedge-nodo-instalar` **sin carga** con el `nodo.env` que ya está (migra y rearranca). Se conservan `SECRET_KEY`,
  la clave del admin local y los datos. Con `LIBRAEDGE_CARGA` se reescribe el `nodo.env` (otra carga) conservando lo
  mismo.
- **Si la configuración falla antes de tocar la base** (carga de otro producto, incompleta), los servicios que estaban
  habilitados se vuelven a arrancar. Con `LIBRAEDGE_CARGA`, un fallo hace fallar el `postinst` (apt sale con error); se
  reintenta con `sudo libraedge-nodo-instalar <carga>` o `sudo dpkg --configure -a`.
- **Un nodo por PC:** el `preinst` corta antes de desempaquetar si la PC tiene el nodo de otro producto
  (`/var/lib/libraedge-nodo/producto.txt` o el paquete de otro producto instalado). No se usa `Conflicts:` a propósito:
  apt sacaría el otro nodo sin preguntar.
- **`apt remove`** para y deshabilita los servicios y el clúster y **borra `nodo.env`**, que tiene el secreto del nodo
  (como en Windows: no queda en el disco de una PC que se da de baja). Antes guarda `SECRET_KEY` y la clave del admin
  local en `/var/lib/libraedge-nodo/conservado.env` (root 600; no el secreto del nodo): sin `SECRET_KEY` no se lee lo
  que libraauth cifró en la base. **Reinstalar después pide la carga** y, con ella, configura sobre los datos que
  estaban (mismo clúster, mismo `SECRET_KEY`; `conservado.env` se borra). **`apt purge`** borra además el resto de
  `/etc/libraedge-nodo`, el estado, los logs y la regla de ufw. **Los datos quedan siempre**: el clúster
  `16/libraedge`, `postgres.clave`, `conservado.env`, `app/config.json`, los respaldos y `producto.txt`. Se borran a mano, después de confirmar que el outbox no tiene nada sin subir:
  `sudo pg_dropcluster --stop 16 libraedge && sudo rm -rf /var/lib/libraedge-nodo`.

### Qué instala

| Dónde | Qué |
|---|---|
| `/opt/libraedge-nodo/python/` | **Python 3.12 propio** (python-build-standalone, el de `uv python install`) con el producto y sus dependencias del `uv.lock`. Ubuntu 22.04 trae 3.10. |
| `/opt/libraedge-nodo/producto/` | `app/`, `migrations/`, `alembic.ini`, los módulos sueltos de la raíz (`plans.py`), `frontend/dist`; compilado a `.pyc` |
| `/opt/libraedge-nodo/instalador/` | los scripts, `producto.json` y `libraedge_instalador.py` |
| `/etc/libraedge-nodo/nodo.env` | el entorno (`root:root 600`; lo lee systemd, no el usuario del servicio) |
| `/var/lib/libraedge-nodo/` | `postgres.clave` (`root 600`), `app/` (`DATA_DIR`, del usuario `libraedge`, 700), `estado/estado.json` (lo lee la bandeja), `respaldos/` (de `postgres`, 700), `producto.txt` |
| `/var/lib/postgresql/16/libraedge/` | el clúster propio del nodo (`pg_createcluster`), en `127.0.0.1:55432`, zona `America/Argentina/Buenos_Aires` puesta en el initdb, `data_checksums`, ICU `en-US` como en Windows |
| `/var/log/libraedge-nodo/instalacion.log` | la salida de `libraedge-nodo-instalar` (sin secretos); los servicios, en `journalctl -u 'libraedge-*'` |

Servicios: `postgresql@16-libraedge`, `libraedge-producto` (uvicorn en el puerto de la definición) y `libraedge-nodo`
(`libraedge correr`), con arranque automático, como el usuario de sistema `libraedge`; `libraedge-respaldo.timer`
(cada hora, `respaldo.sh` como `postgres` por el socket con peer: verifica restaurando en una base única por corrida);
`libraedge-estado.path` (deja el `estado.json` legible para la bandeja con versiones de LibraEdge anteriores a ADR-012).
Si **ufw** está activo, abre el puerto del producto para 10/8, 172.16/12 y 192.168/16; si no, no toca nada. Una segunda
copia del respaldo en otro disco: `LIBRAEDGE_RESPALDO_SECUNDARIO=/media/pendrive` en `/etc/libraedge-nodo/respaldo.env`.

**Ningún secreto en una línea de comandos ni en un log:** la clave de PostgreSQL se genera en `postgres.clave` y se le
pone al rol por la **entrada** de `psql`; las migraciones y la primera sincronización corren con
`libraedge_instalador.py ejecutar --entorno nodo.env --como libraedge`, que carga el entorno **dentro** del proceso y
recién después baja de usuario (ni `env VAR=...`, que se ve en `/proc/<pid>/cmdline`, ni `source`, que ejecutaría un
`$(...)` de un valor como root).

### La bandeja

`pystray` necesita PyGObject (`gi`) en Linux, y PyGObject no se instala con pip en el Python propio (se compila contra
GTK y girepository del sistema). Por eso la bandeja de Linux (`linux/bandeja_linux.py`) corre con el **`python3` del
sistema y su `python3-gi`**, dibuja con AyatanaAppIndicator y **no importa libraedge**: cada 10 s le pregunta al Python
del nodo `libraedge-nodo bandeja --una-vez` (la decisión sigue siendo la de `bandeja.py`) y pinta el resultado. Es 3.10
y biblioteca estándar (un test lo compila con la gramática de 3.10). El paquete **recomienda** `python3-gi`,
`gir1.2-ayatanaappindicator3-0.1` y `gnome-shell-extension-appindicator` (Recommends y no Depends: un servidor sin
escritorio no los necesita). En Ubuntu Desktop la extensión `ubuntu-appindicators@ubuntu.com` viene instalada y la
sesión «ubuntu» la trae habilitada para todos (el CI lo comprueba en los paquetes). Arranca al iniciar sesión por
`/etc/xdg/autostart/libraedge-nodo-bandeja.desktop`, y al terminar de instalar se abre en las sesiones gráficas activas
(`systemd-run --user` en la sesión de cada usuario).

«Abrir <Producto>» corre `bandeja/abrir_producto.sh` (ADR-013), el mismo que el acceso directo del menú
(`/usr/share/applications/libraedge-nodo-<producto>.desktop`, ícono `/opt/libraedge-nodo/marca/<producto>.svg`): con
Google Chrome, Chromium o Edge, en modo aplicación (`--app=`); si no hay ninguno (Ubuntu trae Firefox, que no tiene
modo aplicación), `xdg-open` de la URL. La línea «Otras cajas: http://<IP>:8000 (copiar)» le pregunta la IP al
ayudante (`acceso --json`) cada minuto y al hacer clic la copia.

**No se deja un ícono en el escritorio de los usuarios:** en GNOME un `.desktop` en `~/Escritorio` aparece como «no
confiable» hasta que el usuario lo habilita, y `/etc/skel` sólo llega a los usuarios nuevos. El acceso directo está en
el menú de aplicaciones y **fijado en el dock de todos los usuarios**: `instalador/dock.sh` suma
`libraedge-nodo-<producto>.desktop` a los favoritos por defecto del sistema de cada versión (22.04, 24.04, 26.04) en
`/usr/share/glib-2.0/schemas/90_libraedge-nodo.gschema.override`, y desinstalar lo borra. Cambia el valor por defecto:
quien ya ordenó su dock a mano conserva lo suyo. El navegador: modo aplicación si ya hay Chrome, Chromium o Edge; si
no, una pestaña (se espera a Tauri, sin agregar Chromium).

### Armar

```bash
instalador/linux/armar_deb.sh ventalibra ~/proyectos/_wt/ventalibra-main [--frontend-dist DIR | --sin-frontend] --version 0.12.0
```

Corre en el WSL **sin sudo** (necesita `uv`, `git` con acceso a la familia y `dpkg-deb`): baja el Python con
`uv python install`, instala las dependencias del `uv.lock` del producto con `--python-platform x86_64-manylinux_2_35`
(la glibc de 22.04: el mismo paquete corre en las dos) y deja `instalador/salida/*.deb`. En el CI
(`publicar-instalador.yml`) se arma en `ubuntu-22.04` y se instala y verifica en `ubuntu-22.04` y `ubuntu-24.04`.

### Lo que el CI no puede probar

**La app de escritorio (ADR-015)** en un escritorio de verdad, en VMs con Ubuntu Desktop 22.04, 24.04 y 26.04 (Wayland,
la sesión por defecto) instalando el `.deb` **sin carga**: que el ícono del dock (el favorito) abra la app y la ventana
quede agrupada con él (no un segundo ícono genérico), con el ícono y el nombre del producto; «Vincular con el sistema»
con un código de un `dev.` del producto, con el pedido de clave de pkexec en castellano (y con un usuario que no es
administrador, que pide la clave de otro); que después cargue el producto y la bandeja aparezca; F11 y que se recuerde;
cerrar y abrir la app sin volver a loguearse; un enlace a ARCA o a una ayuda que se abra en el navegador; exportar un
reporte (Descargas); `sudo systemctl restart libraedge-producto` con la app abierta (espera y vuelta); reiniciar la PC
con la app en el inicio de sesión; y que la ventana no quede en blanco (si pasa en una VM: `WEBKIT_DISABLE_COMPOSITING_MODE=1`).

La bandeja en una sesión gráfica real (los runners no tienen escritorio). Para verla: una VM con Ubuntu Desktop 24.04
(y otra con 22.04), instalar el `.deb` con la carga de prueba **con las recomendadas**, y mirar que aparezca el círculo
al lado del reloj, que pase a ámbar al cortar la red (`sudo systemctl stop libraedge-nodo` lo pone rojo), que «Abrir
<Producto>» abra la ventana en modo aplicación (con Chrome o Chromium instalado) o el navegador, que «Otras cajas»
copie la dirección, que el acceso directo aparezca en el menú con la marca del producto y fijado en el dock (con un usuario nuevo y con
uno que ya tenía el dock ordenado), y que
vuelva al cerrar y abrir sesión. También: el producto desde otra PC de la red con ufw
activo, y un corte de energía con el nodo cobrando.

## Historia: la versión de RestoLibra en una VM (2026-08)

Lo que sigue es el registro de cómo se probó la primera versión, atada a RestoLibra, y los defectos que sólo aparecieron
ejecutándola. Las lecciones siguen en el código (ACL por SID, sin BOM, `Continue` alrededor de los programas nativos,
`--data-checksums`, `net stop` antes de `pg_ctl unregister`, borrar `nodo.env` al desinstalar).

### Lo que encontró el paso estático (2026-08-30)

Ninguno de estos se encontró leyendo: cada uno salió de **medir** algo, en la
PowerShell y el Windows reales donde esto va a correr.

| # | Qué | Cómo se midió |
|---|---|---|
| 1 | **`--locale=C` no es el collation del central.** Ordena por bytes: todas las mayúsculas antes que cualquier minúscula. La carta sale en otro orden en el mostrador que en el central. | `select datcollate` contra la base de `restolibra-demo`: **`en_US.utf8`** |
| 2 | **`Set-Content -Encoding utf8` escribe BOM en PowerShell 5.1**, la que trae Windows 11 de fábrica. El `--pwfile` de `initdb` lo lee un programa en C, que **se come el BOM como parte de la contraseña**: el superusuario queda con una clave que el propio script no sabe. | Los bytes del archivo: `EF BB BF 4C 49` en 5.1.26100 |
| 3 | **`"Administrators"` no existe en un Windows en español.** La ACL del `.env` —el archivo con el secreto del nodo— tiraba `IdentityNotMappedException`. | `NTAccount("Administrators").Translate(...)` en un Windows 11 Pro es-AR: falla. `Administradores` → `S-1-5-32-544` |
| 4 | **El bloque de `postgresql.conf` estaba dentro del `else`.** Con un clúster preexistente y sin servicio —una instalación interrumpida— el script registraba el servicio y después sondeaba treinta segundos un puerto donde la base no escuchaba. | Lectura del control de flujo |
| 5 | **El `.iss` declaraba `Version 0.4.1`** con el paquete en v0.6.0. | `git tag` |

> 🔑 **El #2 es también un caso de suposición corregida por la medición.** Mi
> primera lectura fue que el BOM rompía además el `nodo.env`, porque de ahí sale
> el entorno de los servicios. **Es falso**: ese archivo lo relee `Get-Content`,
> que saca el BOM. El que se rompe es el `--pwfile`, y sólo ése. Se escribe sin
> BOM igual, por si alguna vez lo lee algo que no sea PowerShell.

> 🔑 **Y el #3 sólo aparece en español.** Probado en un Windows en inglés, el
> instalador habría pasado; en las PCs de los clientes habría fallado el 100% de
> las veces. Las cuentas van por SID (`S-1-5-18`, `S-1-5-32-544`), que no
> dependen del idioma.

**Lo que se agregó además de arreglarlos**: el script ahora **verifica** la zona
horaria y el collation resultantes consultando `pg_database`, en vez de confiar
en los flags que le pasó a `initdb` — los dos son irreversibles sin rehacer el
clúster, así que si están mal tienen que gritar en la instalación y no seis
meses después. Y verifica la ACL del `.env` después de aplicarla, incluyendo que
no hayan quedado cuentas de más.


### Se ejecutó: VM con Windows 11 LTSC en español (2026-08-30)

> 🟢 **`preparar_postgres.ps1` corrió entero y terminó bien.** Se probó sobre una
> VM de Hyper-V con Windows 11 Enterprise LTSC 2024 **es-ES** recién instalado,
> manejada por PowerShell Direct. Ya no es un borrador revisado: esta parte
> anduvo.

**El montaje, y por qué así.** La zona horaria del Windows invitado se dejó en
**UTC** a propósito. Con el sistema ya en Argentina, "el script puso la zona" y
"la heredó del sistema operativo" dan el mismo resultado y la prueba no
distingue nada. Y el idioma es español porque el defecto de las cuentas sólo
existe ahí.

#### Lo que quedó verificado

| Punto | Resultado |
|---|---|
| **#1 del README: `initdb` y la zona** | `now()` del servidor = `2026-08-30 21:47:42.031166-03` **con el sistema operativo en UTC**. El `-03` lo puso el script. |
| Collation | `datlocprovider = i`, `daticulocale = en-US`. Y el orden **cambió de verdad**: la base ordena `Anana banana Banana cereza`; con `C` sería `Anana Banana banana cereza`. |
| BOM en el `--pwfile` | `psql` se conectó con esa contraseña y creó la base. Con BOM, la contraseña habría sido otra y el bucle de espera habría vencido. |
| BOM en el `nodo.env` | Primeros bytes `4C 49 42` (`LIB`). Sin BOM. |
| ACL del `nodo.env` | Exactamente `NT AUTHORITY\SYSTEM` y `BUILTIN\Administradores`, nada más. En español, y sin excepción. |
| El `._pth` del Python embebido | Confirmado: trae `#import site` comentado, tal como decía el comentario del `.iss`. |

#### 🔴 Lo que la corrida encontró y el paso estático no podía

**1. Los binarios de PostgreSQL no arrancan en un Windows limpio.** El ZIP no
trae el runtime de Visual C++ y Windows 11 LTSC tampoco: los cuatro ejecutables
—`initdb`, `psql`, `pg_ctl`, `postgres`— mueren con `0xC0000135`
(*STATUS_DLL_NOT_FOUND*) **sin escribir una sola línea**, ni siquiera para
`--version`. Ninguna de las DLLs del runtime estaba en `System32`, ninguna venía
en el ZIP. Instalando `vc_redist.x64.exe` los mismos binarios arrancaron sin
tocar nada más.

> Esto voltea la instalación entera y es exactamente lo que el instalador de EDB
> hace y el ZIP no. Ahora el `.iss` lo instala **antes** de todo, y
> `preparar_postgres.ps1` tiene una guarda que convierte ese código de salida
> negativo —que no lleva a ningún lado— en un mensaje que dice qué instalar.

**2. Los dos guards estaban escritos y no podían hablar.** Con
`$ErrorActionPreference = 'Stop'`, PowerShell convierte lo que un programa
nativo escribe en **stderr** en un error terminante, y `*> $null` **no** lo
evita: redirige la salida, no la conversión. El `python -c "import alembic"` que
falla lanzaba en esa misma línea, así que el instalador escupía un traceback de
Python crudo y el mensaje sobre el `import site` no se imprimía nunca.

> El mismo patrón estaba en el **bucle de espera de PostgreSQL**: el primer
> `psql` fallido —el caso normal mientras la base arranca— abortaba en vez de
> reintentar. Acá no se vio porque la VM aceptó conexiones al primer intento; en
> la PC de un restaurante, más lenta, el bucle es justamente lo que hace falta.

**3. Y la verificación de collation que se había agregado no podía fallar.** La
consulta usaba `datlocprovider || '/' || ...` y moría con *operator is not
unique: "char" || unknown*. Lo grave no fue el error: `psql` devolvió **vacío**,
el bloque imprimió la etiqueta sin valor, el `-match` sobre vacío dio falso y el
script siguió con código 0. El chequeo agregado para que el collation no pasara
desapercibido era mudo. Ahora se piden las dos columnas por separado y se grita
si vuelve vacío.

#### Segunda vuelta: `preparar_nodo.ps1` entero, y el nodo levanta solo

> 🟢 **El instalador completo corre y el nodo sobrevive a un reinicio.** Con el
> producto instalado en el Python embebido, `preparar_nodo.ps1` termina con
> código 0, deja los dos servicios andando, y **el punto #2 del README pasa**:
> 51 segundos después de arrancar Windows los tres servicios están arriba y el
> producto contesta 200, sin que nadie toque nada.

**El paso de empaquetado que el `.iss` despachaba en una línea.** *"El producto
ya instalado en el Python embebido"* esconde que **no se puede `pip install` en
un Python embebido**: el paquete no trae `venv` ni `ensurepip` ni `setuptools`,
así que el aislamiento de construcción de pip falla con
*"Cannot import 'hatchling.build'"*. Hay que preinstalar los backends
(`hatchling`, `hatch-vcs`, `setuptools`, `wheel`) y después instalar con
`--no-build-isolation`. Y hace falta **git** en la máquina que arma la carga,
porque las dependencias de la familia son `git+https`.

#### 🔴 Lo que encontró esta vuelta

**4. Faltaba la mitad de las migraciones.** Acá corría sólo
`alembic upgrade head` —la cadena **propia** del producto— y falta la del
**motor**, que es la que crea `facturas`, `cajas`, `usuarios` y el schema entero
de LibraCore. El central corre las dos: `scripts/panel_admin.py` y
`scripts/nuevo_cliente.py` del producto tienen exactamente esa tupla.

> Medido: con una sola cadena la migración muere en el `0001` con
> *relation "facturas" does not exist* y la base del nodo queda con **21**
> tablas, sin `cajas`, `usuarios` ni `sync_outbox`. Con las dos, **73**.

**5. El producto no levantaba, y el servicio decía `Running`.** El `.env` no
incluía `SECRET_KEY`, así que `libraauth` abortaba con *"SECRET_KEY no está
seteado"* en cada arranque. Windows mostraba el servicio **Running** —porque
NSSM estaba vivo— y no había nada escuchando en el puerto.

> 🔑 **Esto es exactamente por qué el chequeo no puede ser `Get-Service`.** Lo
> que lo delató fue pedirle la página de salud y mirar los logs de NSSM. Ahora
> el secreto se genera por nodo con el generador **criptográfico** del sistema
> —`Get-Random` no sirve para esto— y se escribe en el mismo archivo con la
> misma ACL.

**6. Y el bucle de reinicio giraba a dos por segundo.** NSSM reintenta a los
1500 ms por defecto: **24 archivos de log en dos minutos**, quemando disco y
tapando justamente el log que hacía falta leer. Ahora hay un `AppThrottle` de
15 s y la rotación es por tamaño, no por arranque.

#### Lo que quedó verificado en esta vuelta

| Punto | Resultado |
|---|---|
| `preparar_nodo.ps1` completo | exit 0; las dos cadenas de migraciones; **73 tablas** |
| Los dos servicios | registrados, `Automatic`, y el producto **sirve**: salud 200, y una ruta inventada da **404** (no un 200 de catch-all) |
| **#2 del README: reiniciar** | los tres servicios levantan solos; salud 200 a los **51 s** de arrancar Windows |
| El nodo sin central | reporta `en_linea: false` con `HTTP 401` — el motivo real, no un silencio |

> ⚠️ **Desde el 2026-09-17 son tres cadenas**, no dos: `preparar_nodo.ps1` corre
> también `libraauth-migrar upgrade --prefijo P --base $BaseAuth` entre la del motor y la
> del producto, porque libraauth v0.45.0 hace que el arranque **exija** la cadena de auth
> en vez de crear las tablas. Esa vuelta **no se probó en la VM**: la fila de arriba es
> de la segunda vuelta, con dos cadenas.

#### Observación, sin arreglar

Al crear el usuario admin, el producto imprime en el log
`[WARN] ADMIN_PASSWORD no configurado. Contraseña generada: …`. En el nodo eso
deja una contraseña en texto plano en un archivo del disco. Es comportamiento
del producto, no del instalador, pero acá importa más: la PC está en el salón.
Habría que decidir si el nodo debe crear un admin propio —los usuarios bajan por
el espejo— o si el instalador tiene que setear `ADMIN_PASSWORD`.


#### Tercera vuelta: el corte sucio de energia (punto #3)

> 🟢 **Pasa, y con margen.** Se escribieron ventas a ~1000 por segundo, cada una
> con su fila de outbox **en la misma transaccion**, y se apago la VM de golpe
> (`Stop-VM -TurnOff`, que es tirar del cable) en plena escritura.

Al volver:

| Medicion | Resultado |
|---|---|
| Ventas | 27.840 |
| Filas de outbox | 27.840 |
| **Ventas sin su outbox** | **0** |
| **Outbox sin su venta** | **0** |
| Ultima venta | `corte-027840` — sin huecos: el ultimo commit y todos los anteriores |
| Integridad fisica | `sum(total)` sobre la tabla entera, sin errores de lectura |
| PostgreSQL | se recupero solo y volvio a aceptar conexiones |

> 🔑 **La invariante no necesita un registro externo**, y eso es lo que la hace
> util: un archivo con "iba por la venta N" se habria perdido en el mismo corte.
> Lo que se verifica es que **toda venta tenga su outbox y todo outbox su
> venta**. Si el corte hubiera partido una transaccion al medio, aparece una sin
> la otra.

**Y lo que este resultado NO prueba.** Que el WAL se recupere bien no dice nada
sobre lo que el disco haya escrito mal. Por eso el `initdb` ahora lleva
**`--data-checksums`**: PostgreSQL 16 los deja apagados por defecto, y sin ellos
una pagina que el disco devuelve corrupta se lee como datos validos --el error
aparece meses despues, en un arqueo que no cierra--. Es una propiedad del
cluster que se decide en el `initdb` y no se puede cambiar despues.

> El cluster de esta VM se creo **antes** del cambio, asi que corre con
> `data_checksums = off`. La linea nueva se verifico aparte: `initdb` sale con 0
> y dice *"Las sumas de verificacion en paginas de datos han sido activadas"*,
> sin perder ICU.


#### Cuarta vuelta: el `.iss` compilado, instalado, actualizado y desinstalado

> 🟢 **Los seis puntos de la lista pasan.** El instalador se compilo (263 MB), se
> instalo en silencio, se actualizo sobre datos existentes y se desinstalo sin
> tocarlos. Ya no queda nada de este directorio sin ejecutar.

#### 🔴 Los cuatro de esta vuelta

**7. El instalador no se podia desatender.** Los `code:` de las secciones `Run`
leian `PaginaNodo.Values[0]` directo. En `/VERYSILENT` el asistente no se
muestra, los campos quedan vacios, y los dos scripts reciben cadenas vacias
**sin que nada avise**. No es solo un problema para probarlo: un cliente con
cinco sucursales necesita cinco instalaciones a mano, o sea cinco oportunidades
de tipear mal un secreto. Ahora los valores salen de la linea de comandos si
estan y del asistente si no, y en silencio **aborta antes de tocar el disco** si
falta alguno.

**8. Las llaves en los comentarios de Pascal Script.** El primer intento de
compilar murio con *"Error on line 108: Syntax error"*: un comentario que
mencionaba `code:` **entre llaves** cierra el comentario ahi mismo, porque en
Pascal Script la llave ES el comentario.

**9. El desinstalador dejaba PostgreSQL registrado.** `pg_ctl unregister` corria
con el servicio andando y fallaba con **codigo 1072**
(`ERROR_SERVICE_MARKED_FOR_DELETE`): Windows no borra un servicio en marcha,
solo lo marca. El desinstalador terminaba "bien" y el servicio quedaba
registrado --`Running` y `Disabled`-- apuntando a binarios que el mismo
desinstalador acababa de borrar. Ahora se detiene con `net stop` (que espera)
antes de desregistrar.

**10. Y dejaba el SECRETO DEL NODO en el disco.** `nodo.env` lo escribe
`preparar_nodo.ps1` en tiempo de instalacion, asi que el desinstalador no sabe
de el. Con `estado.json` y los `__pycache__` que genera Python al importar --96
archivos en siete directorios, que tampoco instalo nadie-- la carpeta entera
sobrevivia. Un secreto que autoriza a escribir en el central, en el disco de una
PC que se esta dando de baja.

#### Lo verificado, punto por punto

| | Que se midio |
|---|---|
| Compilacion | `Successful compile`, `libraedge-nodo-0.6.4.exe`, 263 MB |
| Instalacion silenciosa | los tres pasos con exit 0, en 2m20s |
| **#1 zona del `initdb`** | `now()` = `-03` con el sistema operativo en **UTC** |
| Collation | `i\|en-US` (ICU), no `C` |
| **Checksums** | `data_checksums = on` en un cluster creado por el instalador real |
| Schema | 73 tablas, con `cajas`, `usuarios`, `facturas` y `sync_outbox` |
| El producto | salud 200; los tres servicios `Running` y `Automatic` |
| **#4 el `.env`** | sin BOM, y solo `SYSTEM` y `BUILTIN\Administradores` |
| **#2 reiniciar** | los tres servicios solos, salud 200 a los 51 s |
| **#3 corte sucio** | 27.840 ventas, ninguna transaccion partida |
| **#6 actualizar** | instalado sobre datos existentes: la marca sobrevive con su valor (4242), **la operacion pendiente del outbox tambien**, y el bloque de `postgresql.conf` **no se duplica** |
| **#5 desinstalar** | los tres servicios quitados, `nodo.env` borrado, la carpeta borrada, y **los datos intactos** |

> 🔑 El #5 y el #6 se probaron **encadenados y con una marca**: se inserto una
> venta con un total conocido y una operacion en el outbox, se desinstalo, se
> reinstalo, y se verifico que la marca volviera con su valor exacto. Comprobar
> que "la carpeta sigue ahi" no habria distinguido datos intactos de datos
> vacios.

#### Observaciones, sin arreglar

- Al reinstalar hay que tipear **la misma contrasena** de PostgreSQL que la vez
  anterior: el cluster no se re-inicializa, asi que una distinta hace fallar el
  bucle de espera con un mensaje que no lo dice.
- El producto imprime en el log `ADMIN_PASSWORD no configurado. Contrasena
  generada: ...` (ver la seccion anterior).



### Lo que hay que verificar en una máquina real

Ninguna de estas está probada. En orden de "si esto falla, no sirve nada":

1. **Que `initdb` deje la zona horaria correcta.** `select now()` desde el
   cliente, comparado contra el reloj de Argentina. **No** `docker exec ... date`
   ni el equivalente en Windows: eso mide el proceso, no el servidor.
2. **Que los servicios arranquen solos después de reiniciar la PC.** Es el
   escenario real: alguien apaga la máquina a la noche. Reiniciar y comprobar que
   el local puede cobrar sin que nadie toque nada.
3. **Que el nodo sobreviva a un corte sucio.** Cortar la energía a mitad de un
   cobro, encender, y verificar que la base no quedó corrupta y que la venta
   quedó o no quedó **entera** (nunca a medias).
4. **Que el `.env` no quede legible por cualquier usuario de la máquina.**
   Contiene el secreto del nodo, que autoriza a escribir en el central.
5. **Que la desinstalación no borre los datos.** Un outbox con operaciones sin
   sincronizar no existe en ningún otro lado.
6. **Que la actualización del producto no rompa el nodo.** El nodo corre las
   mismas migraciones que el central; hay que ver qué pasa si el central va una
   versión adelante.
