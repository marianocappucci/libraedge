# shellcheck shell=bash
# Las variables las usan los scripts que cargan este archivo:
# shellcheck disable=SC2034
# Lo que comparten los scripts del nodo en Linux (ADR-012). Es el comun.ps1 de
# Windows: rutas fijas, el Python propio, el ayudante (libraedge/instalador.py
# tal cual) y la lectura de la definicion del producto.
#
#     . "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"
#
# Regla de esta carpeta, la misma que en Windows: NINGUN secreto en la linea de
# comandos ni en la salida. Se pasan RUTAS de archivos (nodo.env, la clave de
# PostgreSQL) y los mensajes nombran variables, nunca valores. La linea de
# comandos de un proceso la lee cualquier usuario en /proc/<pid>/cmdline.

RAIZ_NODO=/opt/libraedge-nodo
DATOS=/var/lib/libraedge-nodo
CONFIG=/etc/libraedge-nodo
LOGS=/var/log/libraedge-nodo

PYTHON="$RAIZ_NODO/python/bin/python3"
AYUDANTE="$RAIZ_NODO/instalador/libraedge_instalador.py"
DEFINICION="$RAIZ_NODO/instalador/producto.json"
PRODUCTO_DIR="$RAIZ_NODO/producto"

ARCHIVO_ENV="$CONFIG/nodo.env"
CLAVE_PG="$DATOS/postgres.clave"
DATA_DIR="$DATOS/app"
ESTADO_DIR="$DATOS/estado"
ESTADO="$ESTADO_DIR/estado.json"
RESPALDOS="$DATOS/respaldos"
MARCA="$DATOS/producto.txt"
# Lo que apt remove guarda del nodo.env al borrarlo (SECRET_KEY y la clave del admin local, no el secreto del
# nodo): sin SECRET_KEY no se lee lo que libraauth cifro en la base que queda (ADR-012).
CONSERVADO="$DATOS/conservado.env"

# El usuario de los servicios del producto y del nodo. No es root: lo que corre
# expuesto a la red del local no tiene por que poder tocar el sistema.
USUARIO=libraedge

# El PostgreSQL del nodo: un cluster PROPIO (no el "main" que la PC pueda tener),
# solo en 127.0.0.1 y no en el 5432, igual que en Windows.
PG_VERSION=16
PG_CLUSTER=libraedge
PG_PUERTO=55432
PG_SERVICIO="postgresql@$PG_VERSION-$PG_CLUSTER"
# Los binarios de la 16 sin pasar por pg_wrapper: la PC puede tener otra version instalada.
PG_BIN="/usr/lib/postgresql/$PG_VERSION/bin"
ZONA="America/Argentina/Buenos_Aires"

SERVICIOS_NODO=(libraedge-producto.service libraedge-nodo.service)

# -I: aislado (sin PYTHONPATH ni el site del usuario: corre como root).
# -B: sin __pycache__ en /opt (quedaria al desinstalar).
py() { "$PYTHON" -I -B "$@"; }
ayudante() { py "$AYUDANTE" "$@"; }

# Un campo del plan (la definicion validada por el ayudante), como texto.
#     plan_campo 'p["puerto"]'      plan_campo '",".join(b["nombre"] for b in p["bases"])'
plan_campo() {
    ayudante plan --definicion "$DEFINICION" | py -c "import json, sys; p = json.load(sys.stdin); print($1)"
}

# Como se entra al producto en el local, con la IP de esta PC (ADR-013): en esta
# PC se entra SIEMPRE por la copia local, aunque haya internet. Nunca falla.
mostrar_acceso() {
    echo
    ayudante acceso --definicion "$DEFINICION" 2> /dev/null \
        || echo "En esta PC se entra siempre por el acceso directo del producto (http://localhost:8000/)."
    echo
}

# psql como el usuario postgres por el socket local (autenticacion peer): sin
# clave, que es lo que deja crear bases y verificar sin tener la clave a mano.
psql_pg() {
    (cd / && runuser -u postgres -- "$PG_BIN/psql" -X -q -p "$PG_PUERTO" "$@")
}

registrar() { echo "$(date '+%d-%m-%Y %H:%M:%S') $*"; }
aviso() { echo "AVISO: $*" >&2; }
morir() { echo "ERROR: $*" >&2; exit 1; }
