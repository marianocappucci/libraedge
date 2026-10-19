#!/usr/bin/env bash
# Abre la bandeja en las sesiones graficas activas, despues de instalar o
# actualizar (en Windows: ShellExecAsOriginalUser al final del .iss). Al iniciar
# sesion la abre el autostart; esto cubre a quien ya estaba conectado.
#
# Nunca hace fallar la instalacion: sin sesion grafica (un servidor, el CI) no
# hace nada.
set -u
bandeja=/opt/libraedge-nodo/bandeja/bandeja_linux.py

# La de antes (una actualizacion): se cierra para abrir la nueva.
pkill -f "$bandeja" 2> /dev/null || true
command -v loginctl > /dev/null || exit 0
[ -x /usr/bin/python3 ] || exit 0

while read -r sesion _; do
    [ -n "$sesion" ] || continue
    tipo="$(loginctl show-session "$sesion" -p Type --value 2> /dev/null)"
    activa="$(loginctl show-session "$sesion" -p Active --value 2> /dev/null)"
    usuario="$(loginctl show-session "$sesion" -p Name --value 2> /dev/null)"
    case "$tipo" in x11 | wayland) ;; *) continue ;; esac
    [ "$activa" = yes ] && [ -n "$usuario" ] || continue
    # En el administrador de servicios del usuario, que tiene DISPLAY/WAYLAND_DISPLAY
    # y el bus de la sesion (GNOME los importa al iniciar).
    if timeout 20 systemd-run --user --machine="$usuario@.host" --collect --quiet \
            --unit="libraedge-bandeja-$sesion" /usr/bin/python3 "$bandeja" 2> /dev/null; then
        echo "Bandeja abierta en la sesion de $usuario."
    else
        echo "AVISO: no se pudo abrir la bandeja en la sesion de $usuario: aparece al volver a iniciar sesion."
    fi
done < <(loginctl list-sessions --no-legend 2> /dev/null)
exit 0
