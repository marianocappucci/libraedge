#!/usr/bin/env bash
# Arma el .deb de la app de un puesto secundario para Ubuntu 22.04/24.04/26.04 amd64 (ADR-017).
#
#     instalador/linux/puesto/armar_deb.sh <producto> --escritorio DIR [--version X.Y.Z] [--salida DIR]
#
# Es SOLO la app de escritorio del producto (sin nodo, sin PostgreSQL, sin Python): el mismo binario que va en el
# nodo (escritorio/armar.py lo deja en DIR como libraedge-nodo-<producto>), instalado con otro nombre en otra carpeta,
# asi arranca en modo puesto (no hay instalador/producto.json al lado) y su WM_CLASS es el del .desktop de el puesto.
# Deja salida/libraedge-puesto-<producto>_<version>_amd64.deb. No pide sudo.
set -euo pipefail

uso() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 1 ] || uso
producto="$1"; shift
version="0.0.0"; escritorio=""
puesto="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
instalador="$(cd "$puesto/../.." && pwd)"
salida="$instalador/salida"
while [ $# -gt 0 ]; do
    case "$1" in
        --escritorio) escritorio="$(cd "$2" && pwd)"; shift 2 ;;
        --version) version="$2"; shift 2 ;;
        --salida) mkdir -p "$2"; salida="$(cd "$2" && pwd)"; shift 2 ;;
        *) uso ;;
    esac
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([~+.][0-9A-Za-z.~+-]*)?$ ]] || { echo "Version no valida: $version" >&2; exit 2; }
definicion="$instalador/productos/$producto.json"
[ -f "$definicion" ] || { echo "No hay definicion para '$producto' ($definicion)." >&2; exit 2; }
[ -n "$escritorio" ] || uso
app="$escritorio/libraedge-nodo-$producto"
[ -f "$app" ] || { echo "No esta la app de escritorio de $producto en $escritorio (escritorio/armar.py)." >&2; exit 2; }
nombre="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["nombre"])' "$definicion")"

paquete="libraedge-puesto-$producto"
trabajo="$instalador/paquete-linux/$paquete"
raiz="$trabajo/raiz"
opt="$raiz/opt/libraedge-puesto/$producto"
rm -rf "$trabajo"
mkdir -p "$opt" "$raiz/DEBIAN" "$raiz/usr/share/applications" "$salida"

install -m 755 "$app" "$opt/$paquete"
install -m 644 "$instalador/marca/$producto.svg" "$opt/marca.svg"
install -m 755 "$puesto/dock.sh" "$opt/dock.sh"

reemplazar() {  # reemplazar ORIGEN DESTINO MODO
    sed -e "s|@PRODUCTO@|$producto|g" -e "s|@NOMBRE@|$nombre|g" "$1" > "$2"
    chmod "$3" "$2"
    if grep -q '@[A-Z_]*@' "$2"; then echo "Quedo un @VARIABLE@ sin reemplazar en $2" >&2; exit 1; fi
}
reemplazar "$puesto/libraedge-puesto.desktop" "$raiz/usr/share/applications/$paquete.desktop" 644
reemplazar "$puesto/postinst" "$raiz/DEBIAN/postinst" 755
reemplazar "$puesto/postrm" "$raiz/DEBIAN/postrm" 755
# Si el sistema cambia sus favoritos por defecto (ubuntu-settings), se rehace el override del dock.
echo "interest-noawait /usr/share/glib-2.0/schemas" > "$raiz/DEBIAN/triggers"
chmod 644 "$raiz/DEBIAN/triggers"

tamano="$(du -sk --exclude=DEBIAN "$raiz" | cut -f1)"
cat > "$raiz/DEBIAN/control" <<CONTROL
Package: $paquete
Version: $version
Architecture: amd64
Maintainer: Mariano Cappucci <noreply@librasuite.com.ar>
Section: misc
Priority: optional
Installed-Size: $tamano
Depends: libwebkit2gtk-4.1-0, libgtk-3-0
Recommends: xdg-utils
Homepage: https://github.com/marianocappucci/libraedge
Description: $nombre para los otros puestos del local (LibraEdge)
 La version de escritorio de $nombre para las PC del local que no son la
 principal: busca sola en la red la PC principal (la que tiene el nodo
 LibraEdge de $nombre) y abre el sistema desde ella, aunque no haya internet.
 No trae nodo, base de datos ni Python.
CONTROL

deb="$salida/${paquete}_${version}_amd64.deb"
rm -f "$deb"
dpkg-deb --root-owner-group -Zxz -z6 --build "$raiz" "$deb" > /dev/null
echo "Listo: $deb ($(du -h "$deb" | cut -f1); instalado ocupa $((tamano / 1024)) MB)."
