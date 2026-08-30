# Instalador del nodo LibraEdge (Windows)

> 🔴 **NADA DE ESTO SE EJECUTÓ.** Se escribió en un entorno sin Inno Setup, sin
> NSIS y sin PostgreSQL para Windows, así que **no está compilado ni probado** —
> ni el `.iss`, ni los scripts, ni el registro de servicios. Lo que sí se hizo es
> revisarlo a mano contra la documentación de cada herramienta y dejar
> explícitas, abajo, las cosas que hay que verificar en una máquina real antes
> de dárselo a un cliente.
>
> Tratarlo como un **borrador revisado**, no como algo que anduvo.

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
