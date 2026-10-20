#!/usr/bin/env bash
# Abre el producto desde la copia local de esta PC (ADR-013 y ADR-015). Lo usan
# «Abrir <Producto>» de la bandeja, la accion «Abrir en el navegador» del acceso
# directo (/usr/share/applications/libraedge-nodo-<producto>.desktop) y la app
# de escritorio cuando no puede crear su ventana.
#
#     abrir_producto.sh [URL]                 la app de escritorio si esta; si no, el navegador
#     abrir_producto.sh --navegador [URL]     siempre el navegador (el respaldo)
#
# URL: por defecto http://localhost:8000/. En esta PC se entra SIEMPRE por la
# copia local, aunque haya internet: el nodo sincroniza por detras.
#
# La app es /opt/libraedge-nodo/escritorio/libraedge-nodo-<producto> (un nodo
# por PC: hay una sola). El navegador: Chrome, Chromium o Edge en modo
# aplicacion (--app: una ventana sin barra de direcciones); si no hay ninguno
# (Ubuntu trae Firefox, que no tiene modo aplicacion), la URL con xdg-open en el
# navegador por defecto.
set -u
navegador=0
if [ "${1:-}" = --navegador ]; then navegador=1; shift; fi
url="${1:-http://localhost:8000/}"
if [ "$navegador" = 0 ]; then
    for app in "${LIBRAEDGE_ESCRITORIO_DIR:-/opt/libraedge-nodo/escritorio}"/libraedge-nodo-*; do
        if [ -f "$app" ] && [ -x "$app" ]; then
            exec "$app"
        fi
    done
fi
for programa in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge microsoft-edge-stable; do
    if command -v "$programa" > /dev/null 2>&1; then
        exec "$programa" --app="$url"
    fi
done
exec xdg-open "$url"
