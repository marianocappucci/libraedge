#!/bin/sh
# Las apps de caja instaladas (ADR-017), fijadas en el dock de todos los usuarios, como el acceso directo de la PC
# principal (ADR-013, instalador/linux/dock.sh). Sin Python (la caja no lo trae): el valor por defecto de
# `org.gnome.shell favorite-apps` se lee con gsettings en un entorno vacio (sin la configuracion de ningun
# usuario), se le suman al final los libraedge-caja-*.desktop instalados y se escribe en un override propio,
# para la sesion generica y para la «ubuntu». Sin ninguna caja instalada, se borra el override.
#
#     dock.sh       (postinst, trigger y postrm; nunca hace fallar al paquete)
set -u
ESQUEMAS=/usr/share/glib-2.0/schemas
OVERRIDE="$ESQUEMAS/91_libraedge-caja.gschema.override"
APLICACIONES=/usr/share/applications

rm -f "$OVERRIDE"
if [ ! -f "$ESQUEMAS/org.gnome.shell.gschema.xml" ] || ! command -v glib-compile-schemas > /dev/null 2>&1 \
        || ! command -v gsettings > /dev/null 2>&1; then
    glib-compile-schemas "$ESQUEMAS" 2> /dev/null || true
    echo "Dock: no hay GNOME Shell en esta PC; el acceso directo queda en el menu de aplicaciones."
    exit 0
fi
glib-compile-schemas "$ESQUEMAS" || exit 0

# Si en la misma PC tambien esta el nodo (no hace falta, pero pasa en el CI), su acceso directo queda: este override
# tiene mas prioridad que el del nodo (90_) y pisaria sus favoritos.
cajas=""
hay_caja=0
for d in "$APLICACIONES"/libraedge-nodo-*.desktop "$APLICACIONES"/libraedge-caja-*.desktop; do
    [ -f "$d" ] || continue
    case "$d" in *libraedge-caja-*) hay_caja=1 ;; esac
    cajas="$cajas $(basename "$d")"
done
if [ "$hay_caja" = 0 ]; then
    echo "Dock: no quedan apps de caja; vuelven los favoritos del sistema."
    exit 0
fi

# con_cajas "<favorite-apps>": la lista, con cada caja al final si no estaba. El formato es el de gsettings
# (['a.desktop', 'b.desktop'] o @as []), que es el de un .gschema.override.
con_cajas() {
    lista="$(printf '%s' "$1" | sed -e 's/^@as //' -e 's/^ *//' -e 's/ *$//')"
    for c in $cajas; do
        case "$lista" in
            *"'$c'"*) ;;
            "[]") lista="['$c']" ;;
            *"]") lista="${lista%]}, '$c']" ;;
            *) return 1 ;;
        esac
    done
    printf '%s\n' "$lista"
}
por_defecto() {
    env -i PATH=/usr/bin:/bin HOME=/nonexistent GSETTINGS_BACKEND=memory XDG_CURRENT_DESKTOP="$1" \
        gsettings get org.gnome.shell favorite-apps
}
generico="$(con_cajas "$(por_defecto GNOME)")" || { echo "Dock: favorite-apps no es una lista; no se toca."; exit 0; }
ubuntu="$(con_cajas "$(por_defecto ubuntu:GNOME)")" || { echo "Dock: favorite-apps no es una lista; no se toca."; exit 0; }
{
    echo "# Lo escribe /opt/libraedge-caja/dock.sh (LibraEdge, ADR-017): los favoritos del dock por defecto del"
    echo "# sistema mas las apps de caja instaladas. Lo borra desinstalar la ultima."
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
    echo "Dock: glib-compile-schemas rechazo el override; se quito."
    exit 0
fi
echo "Dock:$cajas fijado en los favoritos por defecto de todos los usuarios."
