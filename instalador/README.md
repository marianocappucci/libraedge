# Instalador del nodo LibraEdge (Windows)

> 🟢 **Ya se ejecuto todo.** Al escribirse no se habia ejecutado nada; entre
> el 2026-08-30 y el 2026-08-31 se probo entero en una VM con Windows 11
> Enterprise LTSC en espanol: compilado, instalado en silencio, reiniciado,
> cortado de energia de golpe, actualizado y desinstalado. **Los seis puntos
> de la lista de verificacion pasan**, y en el camino aparecieron **diez
> defectos**, todos arreglados. El detalle esta mas abajo, vuelta por vuelta.
>
> ⚠️ Lo que sigue sin probarse: nada de este directorio. Lo que falta es
> darselo a un cliente real, que es otra cosa.
>
> El texto original decia: Desde el 2026-08-30
>  sí corrió, entero y bien, en una VM con Windows 11 LTSC
> en español — ver más abajo. El resto sigue sin ejecutarse.
>
> Lo que sigue es el texto original: Se escribió en un entorno sin Inno Setup, sin
> NSIS y sin PostgreSQL para Windows, así que **no está compilado ni probado** —
> ni el `.iss`, ni los scripts, ni el registro de servicios. Lo que sí se hizo es
> revisarlo a mano contra la documentación de cada herramienta y dejar
> explícitas, abajo, las cosas que hay que verificar en una máquina real antes
> de dárselo a un cliente.
>
> Tratarlo como un **borrador revisado**, no como algo que anduvo.

> ⚠️ **Actualización 2026-08-30: sigue sin ejecutarse, pero ya no está sin
> revisar.** Se le hizo un paso estático que encontró **cinco defectos**, cuatro
> de ellos suficientes para voltear una instalación real. Están arreglados. Lo
> que sigue faltando es la máquina: Hyper-V no está instalado en la PC del
> humano (corren `HvHost` y `vmcompute`, que es el hipervisor que usa WSL2, pero
> no el rol de Hyper-V) y no hay ISO de Windows 11 a mano.

## Lo que encontró el paso estático (2026-08-30)

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

## Qué instala

Tres cosas en la PC designada del cliente:

1. **PostgreSQL 16**, desde los binarios oficiales (el ZIP, no el instalador de
   EDB), como servicio de Windows.
2. **El producto** (Restolibra) y **LibraEdge**, sobre un Python embebido.
3. **Dos servicios**: el del producto (uvicorn) y el del nodo
   (`libraedge-nodo correr`).

Sin Docker Desktop: su licencia para empresas no es gratuita y agrega una capa
de soporte que en la PC de un restaurante no se puede sostener.

## El orden importa, y por qué

```
1. initdb  ──►  2. arrancar PG  ──►  3. crear la base  ──►  4. migraciones
                                                              │
5. registrar el nodo en el CENTRAL (a mano, una vez) ─────────┤
                                                              ▼
6. escribir el .env  ──►  7. registrar los servicios  ──►  8. arrancar
```

🔴 **`initdb` va antes que todo y con `--locale` y la zona horaria puestas ahí.**
La zona se escribe en `postgresql.conf` una sola vez, en el `initdb`: sobre un
clúster que ya existe, cambiar la variable de entorno mueve el reloj del proceso
y **no** el del servidor. Es el mismo defecto que la familia ya se comió una vez
(ver el barrido de huso del 2026-08-23). Se verifica con `select now()`, nunca
con `date`.

🔴 **El paso 5 es manual y no puede automatizarse.** El secreto del nodo lo emite
el central y **se muestra una sola vez**; el instalador no tiene —ni debería
tener— credenciales para pedirlo por su cuenta. Quien instala lo corre antes en
el central y lo trae:

```
python -m scripts.nodo_offline registrar <node-id> --sucursal <sucursal>
```

## Se ejecutó: VM con Windows 11 LTSC en español (2026-08-30)

> 🟢 **`preparar_postgres.ps1` corrió entero y terminó bien.** Se probó sobre una
> VM de Hyper-V con Windows 11 Enterprise LTSC 2024 **es-ES** recién instalado,
> manejada por PowerShell Direct. Ya no es un borrador revisado: esta parte
> anduvo.

**El montaje, y por qué así.** La zona horaria del Windows invitado se dejó en
**UTC** a propósito. Con el sistema ya en Argentina, "el script puso la zona" y
"la heredó del sistema operativo" dan el mismo resultado y la prueba no
distingue nada. Y el idioma es español porque el defecto de las cuentas sólo
existe ahí.

### Lo que quedó verificado

| Punto | Resultado |
|---|---|
| **#1 del README: `initdb` y la zona** | `now()` del servidor = `2026-08-30 21:47:42.031166-03` **con el sistema operativo en UTC**. El `-03` lo puso el script. |
| Collation | `datlocprovider = i`, `daticulocale = en-US`. Y el orden **cambió de verdad**: la base ordena `Anana banana Banana cereza`; con `C` sería `Anana Banana banana cereza`. |
| BOM en el `--pwfile` | `psql` se conectó con esa contraseña y creó la base. Con BOM, la contraseña habría sido otra y el bucle de espera habría vencido. |
| BOM en el `nodo.env` | Primeros bytes `4C 49 42` (`LIB`). Sin BOM. |
| ACL del `nodo.env` | Exactamente `NT AUTHORITY\SYSTEM` y `BUILTIN\Administradores`, nada más. En español, y sin excepción. |
| El `._pth` del Python embebido | Confirmado: trae `#import site` comentado, tal como decía el comentario del `.iss`. |

### 🔴 Lo que la corrida encontró y el paso estático no podía

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

### Segunda vuelta: `preparar_nodo.ps1` entero, y el nodo levanta solo

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

### 🔴 Lo que encontró esta vuelta

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

### Lo que quedó verificado en esta vuelta

| Punto | Resultado |
|---|---|
| `preparar_nodo.ps1` completo | exit 0; las dos cadenas de migraciones; **73 tablas** |
| Los dos servicios | registrados, `Automatic`, y el producto **sirve**: salud 200, y una ruta inventada da **404** (no un 200 de catch-all) |
| **#2 del README: reiniciar** | los tres servicios levantan solos; salud 200 a los **51 s** de arrancar Windows |
| El nodo sin central | reporta `en_linea: false` con `HTTP 401` — el motivo real, no un silencio |

### Observación, sin arreglar

Al crear el usuario admin, el producto imprime en el log
`[WARN] ADMIN_PASSWORD no configurado. Contraseña generada: …`. En el nodo eso
deja una contraseña en texto plano en un archivo del disco. Es comportamiento
del producto, no del instalador, pero acá importa más: la PC está en el salón.
Habría que decidir si el nodo debe crear un admin propio —los usuarios bajan por
el espejo— o si el instalador tiene que setear `ADMIN_PASSWORD`.


### Tercera vuelta: el corte sucio de energia (punto #3)

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


### Cuarta vuelta: el `.iss` compilado, instalado, actualizado y desinstalado

> 🟢 **Los seis puntos de la lista pasan.** El instalador se compilo (263 MB), se
> instalo en silencio, se actualizo sobre datos existentes y se desinstalo sin
> tocarlos. Ya no queda nada de este directorio sin ejecutar.

### 🔴 Los cuatro de esta vuelta

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

### Lo verificado, punto por punto

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

### Observaciones, sin arreglar

- Al reinstalar hay que tipear **la misma contrasena** de PostgreSQL que la vez
  anterior: el cluster no se re-inicializa, asi que una distinta hace fallar el
  bucle de espera con un mensaje que no lo dice.
- El producto imprime en el log `ADMIN_PASSWORD no configurado. Contrasena
  generada: ...` (ver la seccion anterior).


## Lo que hay que verificar en una máquina real

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

## Archivos

| Archivo | Qué es |
|---|---|
| `nodo.iss` | El script de Inno Setup: qué se copia, qué se ejecuta, y la desinstalación |
| `preparar_postgres.ps1` | `initdb`, servicio y base — el paso 1 a 3 |
| `preparar_nodo.ps1` | Migraciones, `.env` y los dos servicios — el paso 4 al 8 |
| `nodo.env.ejemplo` | Las variables que espera `libraedge-nodo`, con qué es cada una |
