#!/usr/bin/env bash
# Instala el nodo LibraEdge de un producto en Ubuntu 22.04, 24.04 o 26.04 (amd64), en un paso (ADR-012):
#
#     curl -fsSL https://github.com/marianocappucci/libraedge/releases/latest/download/instalar-nodo.sh \
#         | sudo bash -s <producto> --codigo K7Q4-M2XP --direccion sistema.contalibra.com.ar
#
#     ... | sudo bash -s <producto> /ruta/carga-nodo.json        (con el archivo de carga: soporte)
#
# <producto> es restolibra, ventalibra, contalibra, libracargo o libraclub. La direccion y el codigo los
# muestra el sistema en «Sucursal sin internet» al dar de alta el nodo (ADR-014):
# el codigo vale 15 minutos y una sola vez, y se canjea por la carga del nodo por
# https. El secreto del nodo no pasa nunca por la linea de comandos; el codigo,
# un momento (sirve una vez y vence). La carga, si se usa, es una RUTA.
#
# Hace, en orden: controla el sistema (Ubuntu 22.04/24.04/26.04, amd64, root) y la
# carga o el codigo; agrega el repositorio de PostgreSQL de PGDG de la forma oficial
# (el script de postgresql-common: llave en un archivo y `signed-by`, sin apt-key);
# baja el .deb del producto del Release y lo verifica contra SHA256SUMS.txt del
# MISMO Release; y lo instala con las recomendadas (la bandeja) y
# LIBRAEDGE_CODIGO + LIBRAEDGE_DIRECCION (o LIBRAEDGE_CARGA), que lo dejan
# configurado. Sale con un codigo distinto de 0 ante cualquier falla.
#
# Variables (con sudo van despues de sudo: `sudo LIBRAEDGE_VERSION=0.12.0 bash -s ...`):
#     LIBRAEDGE_VERSION   una version (0.12.0) en vez de la ultima publicada
#     LIBRAEDGE_DEB_DIR   un directorio local con el .deb y SHA256SUMS.txt en vez del Release (el CI)
set -euo pipefail

REPO="https://github.com/marianocappucci/libraedge"
PRODUCTOS="restolibra ventalibra contalibra libracargo libraclub"

morir() { echo "ERROR: $*" >&2; exit 1; }
paso() { echo; echo "== $*"; }

# Todo adentro de una funcion: con `curl ... | bash` el script llega por la entrada
# estandar, y bash lo lee de a poco. Asi lo lee entero antes de correr, y ningun
# programa (apt) se come el resto del script leyendo su entrada.
principal() {
    exec < /dev/null
    uso="uso: curl -fsSL $REPO/releases/latest/download/instalar-nodo.sh | sudo bash -s <producto> --codigo <codigo> --direccion <direccion>  (o <producto> /ruta/carga-nodo.json)"
    [ $# -ge 2 ] || { echo "$uso" >&2; exit 2; }
    producto="$1"; shift
    carga=""; codigo=""; direccion=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --codigo) [ $# -ge 2 ] || { echo "$uso" >&2; exit 2; }; codigo="$2"; shift 2 ;;
            --codigo=*) codigo="${1#--codigo=}"; shift ;;
            --direccion) [ $# -ge 2 ] || { echo "$uso" >&2; exit 2; }; direccion="$2"; shift 2 ;;
            --direccion=*) direccion="${1#--direccion=}"; shift ;;
            -*) echo "$uso" >&2; exit 2 ;;
            *) [ -z "$carga" ] || { echo "$uso" >&2; exit 2; }; carga="$1"; shift ;;
        esac
    done
    if [ -n "$codigo" ] || [ -n "$direccion" ]; then
        if [ -z "$codigo" ] || [ -z "$direccion" ] || [ -n "$carga" ]; then echo "$uso" >&2; exit 2; fi
    elif [ -z "$carga" ]; then
        echo "$uso" >&2; exit 2
    fi

    # --- El sistema y los argumentos -------------------------------------------------
    case " $PRODUCTOS " in *" $producto "*) ;; *) morir "Producto desconocido: '$producto' (es uno de: $PRODUCTOS)." ;; esac
    [ "$(id -u)" = 0 ] || morir "Correr como root: ... | sudo bash -s $producto ..."
    [ -r /etc/os-release ] || morir "No se puede leer /etc/os-release: este script es para Ubuntu 22.04, 24.04 o 26.04."
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] || morir "Este sistema es '${ID:-?}': el nodo se instala en Ubuntu 22.04, 24.04 o 26.04."
    case "${VERSION_ID:-}" in 22.04 | 24.04 | 26.04) ;; *) morir "Ubuntu ${VERSION_ID:-?} no esta soportado: 22.04, 24.04 o 26.04." ;; esac
    [ "$(dpkg --print-architecture)" = amd64 ] || morir "La arquitectura es $(dpkg --print-architecture): el nodo es amd64."
    if [ -n "$carga" ]; then
        if [ ! -f "$carga" ] || [ ! -r "$carga" ]; then morir "No esta la carga del nodo (o no se puede leer): $carga"; fi
        carga="$(readlink -f "$carga")"
    else
        # Solo la forma: el codigo se canjea al configurar (despues de bajar e instalar el paquete).
        limpio="$(printf '%s' "$codigo" | tr -d ' -' | tr '[:lower:]' '[:upper:]')"
        [[ "$limpio" =~ ^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$ ]] || morir "El codigo no tiene el formato de la pantalla (8 letras y numeros, como K7Q4-M2XP)."
    fi
    if [ -n "${LIBRAEDGE_VERSION:-}" ]; then
        [[ "$LIBRAEDGE_VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || morir "LIBRAEDGE_VERSION no valida: $LIBRAEDGE_VERSION"
        origen="$REPO/releases/download/v${LIBRAEDGE_VERSION#v}"
    else
        origen="$REPO/releases/latest/download"
    fi
    deb="libraedge-nodo-${producto}_amd64.deb"
    if [ -n "$carga" ]; then
        echo "Nodo LibraEdge de $producto en Ubuntu $VERSION_ID, con la carga $carga."
    else
        echo "Nodo LibraEdge de $producto en Ubuntu $VERSION_ID, con el codigo de vinculacion de $direccion."
    fi

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
    if [ -n "$carga" ]; then
        LIBRAEDGE_CARGA="$carga" apt-get install -y -q "$trabajo/$deb" || morir "La instalacion del nodo fallo (arriba el motivo)."
    else
        # Por el entorno y no por argumento: el postinst se lo pasa a libraedge-nodo-instalar, que no lo loguea.
        LIBRAEDGE_CODIGO="$codigo" LIBRAEDGE_DIRECCION="$direccion" apt-get install -y -q "$trabajo/$deb" \
            || morir "La instalacion del nodo fallo (arriba el motivo). Si el codigo ya se uso, pedir otro."
    fi
    [ -s /etc/libraedge-nodo/nodo.env ] || morir "El paquete se instalo pero el nodo no quedo configurado."

    # Como se entra (ADR-013), con la IP de esta PC: el postinst ya lo dijo, pero arriba de mucha salida de apt.
    echo
    /opt/libraedge-nodo/python/bin/python3 -I -B /opt/libraedge-nodo/instalador/libraedge_instalador.py acceso \
        --definicion /opt/libraedge-nodo/instalador/producto.json || true
    echo
    echo "Listo. Verificar con: sudo libraedge-nodo-verificar"
    if [ -n "$carga" ]; then
        echo "La carga ($carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
    fi
}

principal "$@"
