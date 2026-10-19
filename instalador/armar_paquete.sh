#!/usr/bin/env bash
# Arma instalador/paquete/ para compilar el instalador del nodo de un producto.
#
#     instalador/armar_paquete.sh <producto> <repo del producto> [--frontend-dist RUTA | --sin-frontend]
#     instalador/armar_paquete.sh ventalibra ~/proyectos/ventalibra
#
# Corre en Linux (el WSL): baja el Python embebido, PostgreSQL 16 (ZIP), NSSM y
# el runtime de Visual C++, e instala el producto y sus dependencias PARA
# WINDOWS con `uv pip install --python-platform` (las ruedas de Windows; los
# motores de la familia se construyen desde git, son Python puro). Despues se
# compila en Windows con ISCC (ver README.md y PROBAR_EN_VM.md).
#
# Del repo del producto se usa lo que tenga en HEAD (`git archive`): elegir la
# version con un checkout antes. Las dependencias salen de su uv.lock (`uv
# export --frozen`), asi que el nodo corre exactamente lo mismo que el central.
#
# Hace falta: uv, git con acceso a los repos privados de la familia, unzip,
# curl; y npm (con acceso a libra-ui) si se construye el frontend.
#
# Las versiones se pueden cambiar por entorno:
#     PYTHON_VERSION   3.12.10  (la ultima 3.12 con instalables de Windows)
#     POSTGRES_VERSION 16.10    (binarios de EDB: postgresql-<v>-1-windows-x64-binaries.zip)
#     NSSM_VERSION     2.24
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.12.10}"
POSTGRES_VERSION="${POSTGRES_VERSION:-16.10}"
NSSM_VERSION="${NSSM_VERSION:-2.24}"

uso() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 2 ] || uso
producto="$1"; repo="$(cd "$2" && pwd)"; shift 2
frontend_dist=""; sin_frontend=0
while [ $# -gt 0 ]; do
    case "$1" in
        --frontend-dist) frontend_dist="$(cd "$2" && pwd)"; shift 2 ;;
        --sin-frontend) sin_frontend=1; shift ;;
        *) uso ;;
    esac
done

aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
definicion="$aqui/productos/$producto.json"
[ -f "$definicion" ] || { echo "No hay definicion para '$producto' ($definicion)." >&2; exit 2; }
[ -f "$repo/uv.lock" ] || { echo "$repo no parece el repo del producto (falta uv.lock)." >&2; exit 2; }

paquete="$aqui/paquete"
descargas="$paquete/.descargas"
mkdir -p "$descargas" "$paquete/herramientas"

bajar() {  # bajar URL ARCHIVO: una sola vez, a la carpeta de descargas
    local destino="$descargas/$2"
    if [ ! -s "$destino" ]; then
        echo "Bajando $1" >&2
        curl -fL --retry 3 -o "$destino.parcial" "$1"
        mv "$destino.parcial" "$destino"
    fi
    echo "$destino"
}

# --- Lo comun a todos los productos -----------------------------------------
if [ ! -x "$paquete/postgres/bin/initdb.exe" ] && [ ! -f "$paquete/postgres/bin/initdb.exe" ]; then
    zip_pg="$(bajar "https://get.enterprisedb.com/postgresql/postgresql-$POSTGRES_VERSION-1-windows-x64-binaries.zip" \
        "postgresql-$POSTGRES_VERSION.zip")"
    tmp="$(mktemp -d "$paquete/.pg-XXXXXX")"
    unzip -q "$zip_pg" -d "$tmp"
    # El ZIP trae pgAdmin, StackBuilder, la documentacion y los simbolos: el nodo no los usa.
    rm -rf "$tmp/pgsql/pgAdmin 4" "$tmp/pgsql/StackBuilder" "$tmp/pgsql/doc" "$tmp/pgsql/symbols"
    rm -rf "$paquete/postgres"
    mv "$tmp/pgsql" "$paquete/postgres"
    rm -rf "$tmp"
fi
if [ ! -f "$paquete/herramientas/nssm.exe" ]; then
    zip_nssm="$(bajar "https://nssm.cc/release/nssm-$NSSM_VERSION.zip" "nssm-$NSSM_VERSION.zip")"
    unzip -q -j -o "$zip_nssm" "nssm-$NSSM_VERSION/win64/nssm.exe" -d "$paquete/herramientas"
fi
if [ ! -f "$paquete/herramientas/vc_redist.x64.exe" ]; then
    cp "$(bajar "https://aka.ms/vs/17/release/vc_redist.x64.exe" "vc_redist.x64.exe")" "$paquete/herramientas/"
fi

# --- El producto -------------------------------------------------------------
destino="$paquete/$producto"
rm -rf "$destino"
mkdir -p "$destino/python" "$destino/producto"

zip_py="$(bajar "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip" \
    "python-$PYTHON_VERSION-embed-amd64.zip")"
unzip -q "$zip_py" -d "$destino/python"
pth="$(ls "$destino/python"/python3*._pth)"
# `import site` viene comentado: sin el, site-packages no entra al path (medido
# 2026-08-30). Y `..` es la carpeta de la instalacion, donde esta `app/`: con un
# ._pth presente, Python no agrega el directorio actual por su cuenta.
sed -i 's/\r$//' "$pth"  # viene con CRLF: sin esto `grep -x` no ve las lineas
sed -i 's/^#\s*import site/import site/' "$pth"
grep -qx '\.\.' "$pth" || sed -i '1i ..' "$pth"
grep -qx 'import site' "$pth" || { echo "No se pudo activar 'import site' en $pth" >&2; exit 1; }

requisitos="$destino/requisitos.txt"
( cd "$repo" && uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt ) > "$requisitos"
mapfile -t extras < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("extras", [])))' "$definicion")
echo "Instalando $(grep -c '^[a-zA-Z]' "$requisitos") dependencias de $producto para Windows (+ ${extras[*]:-nada})"
uv pip install --quiet \
    --target "$destino/python/Lib/site-packages" \
    --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
    -r "$requisitos" "${extras[@]}"

# app/, migrations/, alembic.ini y los modulos sueltos de la raiz (plans.py: lo importa
# app/db.py; sin el, la migracion del producto cae en Windows, medido 2026-10-19), tal
# como estan en HEAD (sin .venv ni basura local).
mapfile -t sueltos < <(git -C "$repo" ls-tree --name-only HEAD | grep '\.py$' || true)
git -C "$repo" archive --format=tar HEAD app migrations alembic.ini "${sueltos[@]}" | tar -x -C "$destino/producto"
git -C "$repo" rev-parse --short HEAD > "$destino/producto/REVISION"

if [ "$sin_frontend" = 1 ]; then
    echo "AVISO: sin frontend. La API responde, pero la caja no se puede usar desde el navegador."
elif [ -n "$frontend_dist" ]; then
    mkdir -p "$destino/producto/frontend"
    cp -r "$frontend_dist" "$destino/producto/frontend/dist"
else
    tmp="$(mktemp -d "$paquete/.front-XXXXXX")"
    git -C "$repo" archive --format=tar HEAD frontend | tar -x -C "$tmp"
    ( cd "$tmp/frontend" && npm ci --no-audit --no-fund && npm run build )
    mkdir -p "$destino/producto/frontend"
    cp -r "$tmp/frontend/dist" "$destino/producto/frontend/dist"
    rm -rf "$tmp"
fi

echo
echo "Listo: $destino ($(du -sh "$destino" | cut -f1)), revision $(cat "$destino/producto/REVISION")."
echo "Compilar en Windows:  ISCC.exe /DProducto=$producto nodo.iss"
