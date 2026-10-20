#!/usr/bin/env bash
# El acceso directo del producto, fijado en el dock de todos los usuarios (ADR-013).
#
#     dock.sh agregar libraedge-nodo-<producto>.desktop     (postinst: configure y triggered)
#
# Ubuntu (22.04, 24.04 y 26.04) arma el dock con `org.gnome.shell favorite-apps`;
# sus favoritos por defecto son distintos en cada version y salen de los
# `.gschema.override` del sistema (10_ubuntu-settings, para la sesion «ubuntu»).
# Aca se lee ese valor por defecto TAL COMO QUEDA SIN EL NUESTRO (gsettings con
# el backend en memoria: sin la configuracion de ningun usuario), se le suma el
# .desktop al final y se escribe en un override de prioridad propia (90_), para
# la sesion generica y para la «ubuntu». Cambia el valor por defecto: quien ya
# ordeno su dock a mano conserva lo suyo.
#
# Lo quita el postrm (remove y purge): borra el override y recompila. Si el
# sistema cambia sus favoritos (una actualizacion de ubuntu-settings), el
# trigger del paquete sobre /usr/share/glib-2.0/schemas vuelve a correr esto.
# Sin escritorio (un servidor, sin el esquema de gnome-shell) no hace nada.
# Nunca hace fallar la instalacion: el que llama ignora el codigo.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

ESQUEMAS=/usr/share/glib-2.0/schemas
OVERRIDE="$ESQUEMAS/90_libraedge-nodo.gschema.override"
if [ "${1:-}" != agregar ] || [ -z "${2:-}" ]; then echo "uso: dock.sh agregar <id.desktop>" >&2; exit 2; fi
desktop="$2"

rm -f "$OVERRIDE"
if [ ! -f "$ESQUEMAS/org.gnome.shell.gschema.xml" ] || ! command -v glib-compile-schemas > /dev/null \
        || ! command -v gsettings > /dev/null; then
    registrar "Dock: no hay GNOME Shell en esta PC; el acceso directo queda en el menu de aplicaciones."
    exit 0
fi
glib-compile-schemas "$ESQUEMAS"

por_defecto() {  # por_defecto XDG_CURRENT_DESKTOP
    env -i PATH=/usr/bin:/bin HOME=/nonexistent GSETTINGS_BACKEND=memory XDG_CURRENT_DESKTOP="$1" \
        gsettings get org.gnome.shell favorite-apps
}
generico="$(ayudante favoritos --actual "$(por_defecto GNOME)" --agregar "$desktop")"
ubuntu="$(ayudante favoritos --actual "$(por_defecto ubuntu:GNOME)" --agregar "$desktop")"

{
    echo "# Lo escribe /opt/libraedge-nodo/instalador/dock.sh (LibraEdge, ADR-013): los favoritos del dock"
    echo "# por defecto del sistema mas $desktop. Lo borra desinstalar el paquete."
    echo "[org.gnome.shell]"
    echo "favorite-apps=$generico"
    echo
    echo "[org.gnome.shell:ubuntu]"
    echo "favorite-apps=$ubuntu"
} > "$OVERRIDE"
chmod 644 "$OVERRIDE"
if ! glib-compile-schemas "$ESQUEMAS"; then
    rm -f "$OVERRIDE"
    glib-compile-schemas "$ESQUEMAS" || true
    morir "glib-compile-schemas rechazo el override del dock; se quito."
fi
registrar "Dock: $desktop fijado en los favoritos por defecto de todos los usuarios."
