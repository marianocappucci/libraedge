# Instalador del nodo LibraEdge (Windows)

> 🔴 **NADA DE ESTO SE EJECUTÓ.** Se escribió en un entorno sin Inno Setup, sin
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
