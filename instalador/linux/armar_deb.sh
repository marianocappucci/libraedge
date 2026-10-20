#!/usr/bin/env bash
# Arma el .deb del nodo de un producto para Ubuntu 22.04/24.04 amd64 (ADR-012).
#
#     instalador/linux/armar_deb.sh <producto> <repo del producto> [opciones]
#     instalador/linux/armar_deb.sh ventalibra ~/proyectos/ventalibra --frontend-dist DIR --version 0.12.0
#
# Opciones: --frontend-dist DIR | --sin-frontend, --version X.Y.Z (0.0.0), --salida DIR (instalador/salida).
# Deja salida/libraedge-nodo-<producto>_<version>_amd64.deb.
#
# Es el armar_paquete.sh de Windows para Linux, y NO pide sudo: corre en el WSL o
# en el CI. Del repo del producto se usa lo que tenga en HEAD (git archive); las
# dependencias salen de su uv.lock (uv export --frozen) y se instalan con
# --python-platform x86_64-manylinux_2_35 (la glibc de Ubuntu 22.04), asi el
# mismo paquete corre en 22.04 y en 24.04.
#
# El Python es propio (python-build-standalone, el que instala `uv python`):
# 22.04 trae 3.10 y los productos piden 3.12.
#
# Hace falta: uv, git con acceso a los repos de la familia, dpkg-deb; y npm (con
# acceso a libra-ui) si se construye el frontend.
#     PYTHON_VERSION  3.12  (la ultima 3.12 que conozca uv)
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
uso() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 2 ] || uso
producto="$1"; repo="$(cd "$2" && pwd)"; shift 2
frontend_dist=""; sin_frontend=0; version="0.0.0"
linux="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
instalador="$(dirname "$linux")"
salida="$instalador/salida"
while [ $# -gt 0 ]; do
    case "$1" in
        --frontend-dist) frontend_dist="$(cd "$2" && pwd)"; shift 2 ;;
        --sin-frontend) sin_frontend=1; shift ;;
        --version) version="$2"; shift 2 ;;
        --salida) mkdir -p "$2"; salida="$(cd "$2" && pwd)"; shift 2 ;;
        *) uso ;;
    esac
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([~+.][0-9A-Za-z.~+-]*)?$ ]] || { echo "Version no valida: $version" >&2; exit 2; }

definicion="$instalador/productos/$producto.json"
[ -f "$definicion" ] || { echo "No hay definicion para '$producto' ($definicion)." >&2; exit 2; }
[ -f "$repo/uv.lock" ] || { echo "$repo no parece el repo del producto (falta uv.lock)." >&2; exit 2; }
ayudante="$instalador/../libraedge/instalador.py"

# Lo que hace falta de la definicion, validada por el mismo ayudante que la lee al instalar.
plan="$(python3 "$ayudante" plan --definicion "$definicion")"
campo() { python3 -c "import json, sys; p = json.loads(sys.argv[1]); print($1)" "$plan"; }
nombre="$(campo 'p["nombre"]')"
puerto="$(campo 'p["puerto"]')"
asgi="$(campo 'p["asgi"]')"
bases="$(campo '",".join(b["nombre"] for b in p["bases"])')"
base_nodo="$(campo 'p["base_del_nodo"]')"
mapfile -t extras < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("extras", [])))' "$definicion")
otros="$(for f in "$instalador"/productos/*.json; do b="$(basename "$f" .json)"; [ "$b" = "$producto" ] || printf '%s ' "$b"; done)"

trabajo="$instalador/paquete-linux/$producto"
raiz="$trabajo/raiz"
opt="$raiz/opt/libraedge-nodo"
rm -rf "$trabajo"
mkdir -p "$opt/instalador" "$opt/producto" "$opt/bandeja" "$raiz/DEBIAN" "$raiz/usr/lib/systemd/system" \
    "$raiz/usr/sbin" "$raiz/etc/xdg/autostart" "$salida"

# --- El Python propio ---------------------------------------------------------------
tmp_py="$(mktemp -d "$trabajo/.python-XXXXXX")"
uv python install --quiet --no-bin --install-dir "$tmp_py" "$PYTHON_VERSION"
origen="$(find "$tmp_py" -mindepth 1 -maxdepth 1 -type d -name 'cpython-3.12*-linux-x86_64-gnu' | head -n1)"
[ -n "$origen" ] || { echo "uv no dejo un cpython-3.12 en $tmp_py" >&2; exit 1; }
cp -a "$origen" "$opt/python"
rm -rf "$tmp_py"
py="$opt/python/bin/python3"
sitio="$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# Es el Python del nodo, no uno del sistema: se le instalan paquetes directo.
rm -f "$opt/python/lib/python3.12/EXTERNALLY-MANAGED"
# Lo que el nodo no usa: los tests de la biblioteca estandar, IDLE, tkinter.
rm -rf "$opt/python/lib/python3.12/test" "$opt/python/lib/python3.12/idlelib" \
    "$opt/python/lib/python3.12/turtledemo" "$opt/python/lib/python3.12/tkinter" "$opt/python/share"

# --- Las dependencias del producto, desde su uv.lock ------------------------------------
requisitos="$trabajo/requisitos.txt"
( cd "$repo" && uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt ) > "$requisitos"
echo "Instalando $(grep -c '^[a-zA-Z]' "$requisitos") dependencias de $producto para Linux (+ ${extras[*]:-nada})"
uv pip install --quiet --python "$py" --python-platform x86_64-manylinux_2_35 --python-version 3.12 \
    -r "$requisitos" "${extras[@]}"

# --- El producto -------------------------------------------------------------------------
# app/, migrations/, alembic.ini y los modulos sueltos de la raiz (plans.py lo
# importa app/db.py: sin el, la migracion del producto cae; VM 2026-10-19).
mapfile -t sueltos < <(git -C "$repo" ls-tree --name-only HEAD | grep '\.py$' || true)
git -C "$repo" archive --format=tar HEAD app migrations alembic.ini "${sueltos[@]}" | tar -x -C "$opt/producto"
git -C "$repo" rev-parse --short HEAD > "$opt/producto/REVISION"
# El producto se importa desde cualquier directorio (el ayudante, las migraciones).
echo "/opt/libraedge-nodo/producto" > "$sitio/libraedge-nodo-producto.pth"

if [ "$sin_frontend" = 1 ]; then
    echo "AVISO: sin frontend. La API responde, pero la caja no se puede usar desde el navegador."
elif [ -n "$frontend_dist" ]; then
    mkdir -p "$opt/producto/frontend"
    cp -r "$frontend_dist" "$opt/producto/frontend/dist"
else
    tmp="$(mktemp -d "$trabajo/.front-XXXXXX")"
    git -C "$repo" archive --format=tar HEAD frontend | tar -x -C "$tmp"
    ( cd "$tmp/frontend" && npm ci --no-audit --no-fund && npm run build )
    mkdir -p "$opt/producto/frontend"
    cp -r "$tmp/frontend/dist" "$opt/producto/frontend/dist"
    rm -rf "$tmp"
fi

# Compilado de antemano: los servicios corren como un usuario que no puede
# escribir __pycache__ en /opt, y sin esto cada arranque recompila todo.
"$py" -m compileall -q -j 0 -s "$raiz" -p / "$opt/python/lib/python3.12" "$opt/producto" > /dev/null || true

# --- El instalador -------------------------------------------------------------------------
cp "$definicion" "$opt/instalador/producto.json"
cp "$ayudante" "$opt/instalador/libraedge_instalador.py"
for script in comun.sh instalar.sh preparar_postgres.sh preparar_nodo.sh respaldo.sh verificar_nodo.sh; do
    install -m 755 "$linux/$script" "$opt/instalador/$script"
done
chmod 644 "$opt/instalador/comun.sh"
install -m 644 "$linux/bandeja_linux.py" "$opt/bandeja/bandeja_linux.py"
install -m 755 "$linux/abrir_bandeja.sh" "$opt/bandeja/abrir_bandeja.sh"
install -m 644 "$linux/libraedge-nodo-bandeja.desktop" "$raiz/etc/xdg/autostart/libraedge-nodo-bandeja.desktop"
cp "$instalador/libraedge.ico" "$opt/libraedge.ico"
ln -s /opt/libraedge-nodo/instalador/instalar.sh "$raiz/usr/sbin/libraedge-nodo-instalar"
ln -s /opt/libraedge-nodo/instalador/verificar_nodo.sh "$raiz/usr/sbin/libraedge-nodo-verificar"

reemplazar() {  # reemplazar ORIGEN DESTINO MODO
    sed -e "s|@PRODUCTO@|$producto|g" -e "s|@NOMBRE@|$nombre|g" -e "s|@PUERTO@|$puerto|g" \
        -e "s|@ASGI@|$asgi|g" -e "s|@BASES@|$bases|g" -e "s|@BASE_NODO@|$base_nodo|g" \
        -e "s|@OTROS@|$otros|g" "$1" > "$2"
    chmod "$3" "$2"
    if grep -q '@[A-Z_]*@' "$2"; then echo "Quedo un @VARIABLE@ sin reemplazar en $2" >&2; exit 1; fi
}
for unidad in "$linux"/systemd/*; do
    reemplazar "$unidad" "$raiz/usr/lib/systemd/system/$(basename "$unidad")" 644
done
for script in preinst postinst prerm postrm; do
    reemplazar "$linux/debian/$script" "$raiz/DEBIAN/$script" 755
done

# --- El control ------------------------------------------------------------------------------
tamano="$(du -sk --exclude=DEBIAN "$raiz" | cut -f1)"
cat > "$raiz/DEBIAN/control" <<EOF
Package: libraedge-nodo-$producto
Version: $version
Architecture: amd64
Maintainer: Mariano Cappucci <noreply@librasuite.com.ar>
Section: misc
Priority: optional
Installed-Size: $tamano
Depends: postgresql-16, postgresql-client-16, postgresql-common, systemd, adduser, util-linux, procps
Recommends: python3, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gnome-shell-extension-appindicator, xdg-utils
Homepage: https://github.com/marianocappucci/libraedge
Description: Nodo LibraEdge de $nombre (sucursal sin internet)
 La instancia local de $nombre para una sucursal: el producto con su propio
 Python 3.12, PostgreSQL 16 (PGDG) en un cluster propio en 127.0.0.1:55432,
 el proceso que sincroniza con el central, el respaldo verificado cada hora
 y el icono de bandeja. Se configura con la carga del nodo que emite el
 central: sudo libraedge-nodo-instalar /ruta/carga-nodo.json
 Revision del producto: $(cat "$opt/producto/REVISION").
EOF

deb="$salida/libraedge-nodo-${producto}_${version}_amd64.deb"
rm -f "$deb"
dpkg-deb --root-owner-group -Zxz -z6 --build "$raiz" "$deb" > /dev/null
echo
echo "Listo: $deb ($(du -h "$deb" | cut -f1); instalado ocupa $((tamano / 1024)) MB), revision $(cat "$opt/producto/REVISION")."
