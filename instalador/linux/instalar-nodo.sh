#!/usr/bin/env bash
# Instala el nodo LibraEdge de un producto en Ubuntu 22.04 o 24.04 (amd64), en un paso (ADR-012):
#
#     curl -fsSL https://github.com/marianocappucci/libraedge/releases/latest/download/instalar-nodo.sh \
#         | sudo bash -s <producto> /ruta/carga-nodo.json
#
# <producto> es restolibra, ventalibra o contalibra. La carga es una RUTA: el
# secreto del nodo no pasa nunca por la linea de comandos.
#
# Hace, en orden: controla el sistema (Ubuntu 22.04/24.04, amd64, root) y la
# carga; agrega el repositorio de PostgreSQL de PGDG de la forma oficial (el
# script de postgresql-common: llave en un archivo y `signed-by`, sin apt-key);
# baja el .deb del producto del Release y lo verifica contra SHA256SUMS.txt del
# MISMO Release; y lo instala con las recomendadas (la bandeja) y
# LIBRAEDGE_CARGA, que lo deja configurado. Sale con un codigo distinto de 0
# ante cualquier falla.
#
# Variables (con sudo van despues de sudo: `sudo LIBRAEDGE_VERSION=0.12.0 bash -s ...`):
#     LIBRAEDGE_VERSION   una version (0.12.0) en vez de la ultima publicada
#     LIBRAEDGE_DEB_DIR   un directorio local con el .deb y SHA256SUMS.txt en vez del Release (el CI)
set -euo pipefail

REPO="https://github.com/marianocappucci/libraedge"
PRODUCTOS="restolibra ventalibra contalibra"

morir() { echo "ERROR: $*" >&2; exit 1; }
paso() { echo; echo "== $*"; }

# Todo adentro de una funcion: con `curl ... | bash` el script llega por la entrada
# estandar, y bash lo lee de a poco. Asi lo lee entero antes de correr, y ningun
# programa (apt) se come el resto del script leyendo su entrada.
principal() {
    exec < /dev/null
    uso="uso: curl -fsSL $REPO/releases/latest/download/instalar-nodo.sh | sudo bash -s <producto> /ruta/carga-nodo.json"
    [ $# -eq 2 ] || { echo "$uso" >&2; exit 2; }
    producto="$1"; carga="$2"

    # --- El sistema y los argumentos -------------------------------------------------
    case " $PRODUCTOS " in *" $producto "*) ;; *) morir "Producto desconocido: '$producto' (es uno de: $PRODUCTOS)." ;; esac
    [ "$(id -u)" = 0 ] || morir "Correr como root: ... | sudo bash -s $producto $carga"
    [ -r /etc/os-release ] || morir "No se puede leer /etc/os-release: este script es para Ubuntu 22.04 o 24.04."
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] || morir "Este sistema es '${ID:-?}': el nodo se instala en Ubuntu 22.04 o 24.04."
    case "${VERSION_ID:-}" in 22.04 | 24.04) ;; *) morir "Ubuntu ${VERSION_ID:-?} no esta soportado: 22.04 o 24.04." ;; esac
    [ "$(dpkg --print-architecture)" = amd64 ] || morir "La arquitectura es $(dpkg --print-architecture): el nodo es amd64."
    if [ ! -f "$carga" ] || [ ! -r "$carga" ]; then morir "No esta la carga del nodo (o no se puede leer): $carga"; fi
    carga="$(readlink -f "$carga")"
    if [ -n "${LIBRAEDGE_VERSION:-}" ]; then
        [[ "$LIBRAEDGE_VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || morir "LIBRAEDGE_VERSION no valida: $LIBRAEDGE_VERSION"
        origen="$REPO/releases/download/v${LIBRAEDGE_VERSION#v}"
    else
        origen="$REPO/releases/latest/download"
    fi
    deb="libraedge-nodo-${producto}_amd64.deb"
    echo "Nodo LibraEdge de $producto en Ubuntu $VERSION_ID, con la carga $carga."

    export DEBIAN_FRONTEND=noninteractive
    trabajo="$(mktemp -d /tmp/libraedge-nodo-XXXXXX)"
    trap 'rm -rf "$trabajo"' EXIT
    # apt baja con el usuario _apt: tiene que poder leer el .deb.
    chmod 755 "$trabajo"

    # --- PostgreSQL 16 de PGDG -------------------------------------------------------------
    paso "Repositorio de PostgreSQL (apt.postgresql.org)"
    apt-get update -q
    apt-get install -y -q ca-certificates curl postgresql-common
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
    politica="$(apt-cache policy postgresql-16)"
    case "$politica" in *apt.postgresql.org*) ;; *) morir "postgresql-16 no sale del repositorio de PGDG." ;; esac

    # --- El .deb y su suma -----------------------------------------------------------------
    if [ -n "${LIBRAEDGE_DEB_DIR:-}" ]; then
        paso "El paquete, de $LIBRAEDGE_DEB_DIR"
        cp "$LIBRAEDGE_DEB_DIR/$deb" "$LIBRAEDGE_DEB_DIR/SHA256SUMS.txt" "$trabajo/" || morir "No estan $deb o SHA256SUMS.txt en $LIBRAEDGE_DEB_DIR."
    else
        paso "El paquete, de $origen"
        for archivo in SHA256SUMS.txt "$deb"; do
            curl -fsSL --retry 3 -o "$trabajo/$archivo" "$origen/$archivo" || morir "No se pudo bajar $origen/$archivo."
        done
    fi
    (
        cd "$trabajo"
        grep -E "^[0-9a-f]{64}  $deb\$" SHA256SUMS.txt > suma.txt || morir "SHA256SUMS.txt no tiene la suma de $deb."
        sha256sum --check --strict suma.txt || morir "El $deb bajado no coincide con SHA256SUMS.txt: no se instala."
    )

    # --- Instalar y configurar -----------------------------------------------------------------
    paso "Instalando y configurando el nodo"
    LIBRAEDGE_CARGA="$carga" apt-get install -y -q "$trabajo/$deb" || morir "La instalacion del nodo fallo (arriba el motivo)."
    [ -s /etc/libraedge-nodo/nodo.env ] || morir "El paquete se instalo pero el nodo no quedo configurado."

    echo
    echo "Listo. Verificar con: sudo libraedge-nodo-verificar"
    echo "La carga ($carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
}

principal "$@"
