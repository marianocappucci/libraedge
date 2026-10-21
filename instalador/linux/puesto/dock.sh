#!/bin/sh
# Las apps de puesto instalados (ADR-017), fijadas en el dock de todos los usuarios, como el acceso directo de la PC
# principal (ADR-013, instalador/linux/dock.sh). Sin Python (el puesto no lo trae): el valor por defecto de
# `org.gnome.shell favorite-apps` se lee con gsettings en un entorno vacio (sin la configuracion de ningun
# usuario), se le suman al final los libraedge-puesto-*.desktop instalados y se escribe en un override propio,
# para la sesion generica y para la «ubuntu». Sin ningun puesto instalado, se borra el override.
#
#     dock.sh       (postinst, trigger y postrm; nunca hace fallar al paquete)
set -u
ESQUEMAS=/usr/share/glib-2.0/schemas
OVERRIDE="$ESQUEMAS/91_libraedge-puesto.gschema.override"
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
puestos=""
hay_puesto=0
for d in "$APLICACIONES"/libraedge-nodo-*.desktop "$APLICACIONES"/libraedge-puesto-*.desktop; do
    [ -f "$d" ] || continue
    case "$d" in *libraedge-puesto-*) hay_puesto=1 ;; esac
    puestos="$puestos $(basename "$d")"
done
if [ "$hay_puesto" = 0 ]; then
    echo "Dock: no quedan apps de puesto; vuelven los favoritos del sistema."
    exit 0
fi

# con_puestos "<favorite-apps>": la lista, con cada puesto al final si no estaba. El formato es el de gsettings
# (['a.desktop', 'b.desktop'] o @as []), que es el de un .gschema.override.
con_puestos() {
    lista="$(printf '%s' "$1" | sed -e 's/^@as //' -e 's/^ *//' -e 's/ *$//')"
    for c in $puestos; do
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
generico="$(con_puestos "$(por_defecto GNOME)")" || { echo "Dock: favorite-apps no es una lista; no se toca."; exit 0; }
ubuntu="$(con_puestos "$(por_defecto ubuntu:GNOME)")" || { echo "Dock: favorite-apps no es una lista; no se toca."; exit 0; }
{
    echo "# Lo escribe /opt/libraedge-puesto/dock.sh (LibraEdge, ADR-017): los favoritos del dock por defecto del"
    echo "# sistema mas las apps de puesto instalados. Lo borra desinstalar el ultimo."
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
echo "Dock:$puestos fijado en los favoritos por defecto de todos los usuarios."
