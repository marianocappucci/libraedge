#!/usr/bin/env bash
# Configura el nodo LibraEdge en Linux (ADR-012). Es el instalar.ps1 de Windows:
# lo corre el postinst del .deb y se puede correr a mano (todo es idempotente).
#
#     sudo libraedge-nodo-instalar /ruta/carga-nodo.json    primera vez, o con una carga nueva
#     sudo libraedge-nodo-instalar                          actualizar/reparar con el nodo.env que ya esta
#
# La carga la emite el central (pantalla «Sucursal sin internet» o
# `libraedge-central nodo registrar --carga`). Tambien se puede pasar con la
# variable LIBRAEDGE_CARGA al instalar el paquete:
#
#     sudo LIBRAEDGE_CARGA=/ruta/carga-nodo.json apt install ./libraedge-nodo-<producto>_<version>_amd64.deb
#
# Sale con 0 si el nodo quedo configurado y con otro codigo si no (2: falta la
# carga o no sirve). El detalle queda en /var/log/libraedge-nodo/instalacion.log,
# sin secretos: solo pasan rutas de archivos.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

[ "$(id -u)" = 0 ] || { echo "ERROR: correr como root (sudo libraedge-nodo-instalar ...)." >&2; exit 2; }
carga="${1:-${LIBRAEDGE_CARGA:-}}"

install -d -m 750 "$LOGS"
exec > >(tee -a "$LOGS/instalacion.log") 2>&1
echo
registrar "== libraedge-nodo-instalar ${carga:+con la carga $carga}"

# Hasta tocar la base, un error deja la PC como estaba: los servicios que estaban
# habilitados se vuelven a arrancar (una carga equivocada no deja el local sin
# sistema; el prerm del paquete los paro para actualizar).
tocado=0
al_salir() {
    local codigo=$?
    if [ "$codigo" -ne 0 ] && [ "$tocado" = 0 ]; then
        for servicio in "$PG_SERVICIO" "${SERVICIOS_NODO[@]}"; do
            if systemctl is-enabled --quiet "$servicio" 2> /dev/null && ! systemctl is-active --quiet "$servicio"; then
                echo "Se vuelve a arrancar $servicio (no se toco nada)."
                systemctl start "$servicio" || true
            fi
        done
    fi
    if [ "$codigo" -ne 0 ]; then
        echo "La configuracion del nodo fallo (codigo $codigo). El detalle esta arriba y en $LOGS/instalacion.log."
    fi
}
trap al_salir EXIT

nombre="$(plan_campo 'p["nombre"]')" || morir "La definicion del producto ($DEFINICION) no es valida."
producto="$(plan_campo 'p["producto"]')"
registrar "Nodo LibraEdge de $nombre"

# --- 1. La carga, antes de tocar nada ----------------------------------------------
if [ -n "$carga" ]; then
    [ -f "$carga" ] || { echo "ERROR: no esta la carga: $carga" >&2; exit 2; }
    carga="$(readlink -f "$carga")"
    # El resumen no tiene el secreto.
    resumen="$(ayudante validar --definicion "$DEFINICION" --carga "$carga")" \
        || { echo "ERROR: la carga del nodo no sirve: el motivo esta arriba." >&2; exit 2; }
    echo "$resumen" | py -c '
import json, sys
r = json.load(sys.stdin)
print("Carga: nodo {node_id}, prefijo {prefijo}, central {central}, {tablas_espejo} tablas espejo.".format(**r))
for a in r["avisos"]:
    print("AVISO: " + a)'
elif [ ! -s "$ARCHIVO_ENV" ]; then
    echo "ERROR: falta la carga del nodo. Correr: sudo libraedge-nodo-instalar /ruta/carga-nodo.json" >&2
    exit 2
else
    registrar "Sin carga: se actualiza con el $ARCHIVO_ENV que ya esta."
fi

# --- 2. Un nodo por PC -------------------------------------------------------------------
if [ -s "$MARCA" ] && [ "$(cat "$MARCA")" != "$producto" ]; then
    morir "Esta PC ya tiene el nodo de $(cat "$MARCA"). Un nodo por PC."
fi

# --- 3. El usuario de los servicios ----------------------------------------------------
if ! id "$USUARIO" > /dev/null 2>&1; then
    adduser --system --group --home "$DATOS" --no-create-home --shell /usr/sbin/nologin "$USUARIO" > /dev/null
    registrar "Usuario $USUARIO creado."
fi

# Las migraciones no corren con el producto atendiendo.
for servicio in "${SERVICIOS_NODO[@]}"; do
    if systemctl is-active --quiet "$servicio"; then
        registrar "Se detiene $servicio para actualizar."
        systemctl stop "$servicio"
    fi
done

# --- 4. PostgreSQL ---------------------------------------------------------------------------
tocado=1
"$RAIZ_NODO/instalador/preparar_postgres.sh" "$(plan_campo '",".join(b["nombre"] for b in p["bases"])')"

# --- 5. El nodo --------------------------------------------------------------------------------
"$RAIZ_NODO/instalador/preparar_nodo.sh" ${carga:+"$carga"}

printf '%s\n' "$producto" > "$MARCA"

# --- 6. La bandeja, si hay alguien con sesion grafica ----------------------------------------
"$RAIZ_NODO/bandeja/abrir_bandeja.sh" || true

# --- 7. Como se entra: el acceso directo «<Producto>» y la direccion para las otras cajas (ADR-013) ---
mostrar_acceso

if [ -n "$carga" ]; then
    echo "La carga ($carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
fi
registrar "== Listo."
