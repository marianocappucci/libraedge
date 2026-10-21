#!/usr/bin/env bash
# Configura el nodo LibraEdge en Linux (ADR-012). Es el instalar.ps1 de Windows:
# lo corre el postinst del .deb y se puede correr a mano (todo es idempotente).
#
#     sudo libraedge-nodo-instalar --codigo K7Q4-M2XP --direccion sistema.contalibra.com.ar
#                                                           primera vez o reinstalar, con el codigo (ADR-014)
#     sudo libraedge-nodo-instalar /ruta/carga-nodo.json    lo mismo con el archivo de carga (soporte)
#     sudo libraedge-nodo-instalar                          actualizar/reparar con el nodo.env que ya esta
#     ... --direccion D --codigo-entrada                    el codigo por la entrada estandar (una linea): asi lo
#                                                           pasa la app de escritorio por pkexec (ADR-015)
#
# La direccion y el codigo los muestra el central (pantalla «Sucursal sin
# internet», al dar de alta el nodo): el codigo vale 15 minutos y una sola vez, y
# el ayudante lo canjea por la carga del nodo por https. Tambien se pueden pasar
# al instalar el paquete, con LIBRAEDGE_CODIGO y LIBRAEDGE_DIRECCION (o la carga
# con LIBRAEDGE_CARGA):
#
#     sudo LIBRAEDGE_CODIGO=K7Q4-M2XP LIBRAEDGE_DIRECCION=sistema.contalibra.com.ar \
#         apt install ./libraedge-nodo-<producto>_<version>_amd64.deb
#
# El codigo NO se registra en el log. Pasado por argumento queda un momento en la
# linea de comandos de este proceso (/proc/<pid>/cmdline): sirve una sola vez y
# vence, y para no dejarlo ni ahi esta LIBRAEDGE_CODIGO.
#
# Sale con 0 si el nodo quedo configurado y con otro codigo si no (2: falta la
# carga o el codigo, o no sirven). El detalle queda en
# /var/log/libraedge-nodo/instalacion.log, sin secretos: solo pasan rutas de archivos.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

[ "$(id -u)" = 0 ] || { echo "ERROR: correr como root (sudo libraedge-nodo-instalar ...)." >&2; exit 2; }
uso="uso: libraedge-nodo-instalar [--codigo CODIGO | --codigo-entrada] --direccion DIRECCION | /ruta/carga-nodo.json"
carga="${LIBRAEDGE_CARGA:-}"
codigo_vinc="${LIBRAEDGE_CODIGO:-}"
direccion="${LIBRAEDGE_DIRECCION:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --codigo) [ $# -ge 2 ] || { echo "$uso" >&2; exit 2; }; codigo_vinc="$2"; shift 2 ;;
        --codigo=*) codigo_vinc="${1#--codigo=}"; shift ;;
        # La app de escritorio lo manda por la entrada (pkexec limpia el entorno y la linea de comandos se ve en /proc).
        --codigo-entrada) IFS= read -r codigo_vinc || true; shift ;;
        --direccion) [ $# -ge 2 ] || { echo "$uso" >&2; exit 2; }; direccion="$2"; shift 2 ;;
        --direccion=*) direccion="${1#--direccion=}"; shift ;;
        -h | --help) echo "$uso"; exit 0 ;;
        -*) echo "$uso" >&2; exit 2 ;;
        *) carga="$1"; shift ;;
    esac
done
unset LIBRAEDGE_CODIGO
if [ -n "$codigo_vinc" ] || [ -n "$direccion" ]; then
    if [ -z "$codigo_vinc" ] || [ -z "$direccion" ]; then
        echo "ERROR: el codigo va con la direccion del sistema. $uso" >&2
        exit 2
    fi
    [ -z "$carga" ] || { echo "ERROR: o el codigo o el archivo de carga, no los dos. $uso" >&2; exit 2; }
fi

install -d -m 750 "$LOGS"
exec > >(tee -a "$LOGS/instalacion.log") 2>&1
echo
# Ni el codigo ni el secreto en el log: la direccion y la ruta de la carga, si.
registrar "== libraedge-nodo-instalar ${carga:+con la carga $carga}${direccion:+con el codigo de vinculacion de $direccion}"
# pkexec deja el usuario que lo pidio: la app de escritorio de ese usuario (ADR-015).
if [ -n "${PKEXEC_UID:-}" ]; then registrar "Pedido desde la app de escritorio (usuario $PKEXEC_UID)."; fi

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
        if [ -n "${canjeada:-}" ] && [ -s "$canjeada" ]; then
            echo "El codigo ya se uso: la carga quedo en $canjeada (root, 600). Reintentar con:"
            echo "    sudo libraedge-nodo-instalar $canjeada"
        fi
    fi
}
trap al_salir EXIT

nombre="$(plan_campo 'p["nombre"]')" || morir "La definicion del producto ($DEFINICION) no es valida."
producto="$(plan_campo 'p["producto"]')"
registrar "Nodo LibraEdge de $nombre"

# --- 1. La carga, antes de tocar nada ----------------------------------------------
# Con el codigo (ADR-014), primero se canjea: el ayudante lo lee de LIBRAEDGE_CODIGO (nunca de un argumento) y
# escribe la carga en un archivo de root con permisos 600, que se borra al terminar bien. Si algo falla despues, el
# archivo queda (el codigo ya se uso) y se reintenta con: sudo libraedge-nodo-instalar <esa ruta>.
canjeada=""
if [ -n "$codigo_vinc" ]; then
    canjeada="$DATOS/carga-nodo.json"
    install -d -m 755 "$DATOS"
    rm -f "$canjeada"
    install -m 600 -o root -g root /dev/null "$canjeada"
    registrar "Canjeando el codigo de vinculacion en $direccion..."
    export LIBRAEDGE_CODIGO="$codigo_vinc"
    if ! ayudante canjear --definicion "$DEFINICION" --direccion "$direccion" --salida "$canjeada" > /dev/null; then
        unset LIBRAEDGE_CODIGO
        rm -f "$canjeada"
        echo "ERROR: no se pudo canjear el codigo (el motivo esta arriba). Pedir otro en «Sucursal sin internet»." >&2
        exit 2
    fi
    unset LIBRAEDGE_CODIGO
    codigo_vinc=""
    carga="$canjeada"
    registrar "Codigo canjeado: la carga del nodo quedo en $canjeada (root, 600) hasta terminar."
fi
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
    echo "ERROR: falta configurar el nodo. Correr: sudo libraedge-nodo-instalar --codigo <codigo> --direccion <direccion>" \
        "(los muestra «Sucursal sin internet» en el sistema) o con /ruta/carga-nodo.json." >&2
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

# --- 7. Como se entra: el acceso directo «<Producto>» y la direccion para los otros puestos (ADR-013) ---
mostrar_acceso

if [ -n "$canjeada" ]; then
    # El secreto ya esta en el nodo.env: la carga canjeada no tiene por que quedar.
    rm -f "$canjeada"
elif [ -n "$carga" ]; then
    echo "La carga ($carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
fi
registrar "== Listo."
