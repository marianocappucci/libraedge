#!/usr/bin/env bash
# Abre el producto desde la copia local de esta PC (ADR-013). Lo usan el acceso
# directo del menu de aplicaciones (/usr/share/applications/libraedge-nodo-<producto>.desktop)
# y «Abrir <Producto>» de la bandeja: el mismo lanzamiento en los dos.
#
#     abrir_producto.sh [URL]       por defecto http://localhost:8000/
#
# En esta PC se entra SIEMPRE por la copia local, aunque haya internet: el nodo
# sincroniza por detras. Con Chrome, Chromium o Edge, en modo aplicacion
# (--app: una ventana sin barra de direcciones, con el titulo y el icono de la
# pagina); si no hay ninguno (Ubuntu trae Firefox, que no tiene modo
# aplicacion), la URL con xdg-open en el navegador por defecto.
set -u
url="${1:-http://localhost:8000/}"
for navegador in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge microsoft-edge-stable; do
    if command -v "$navegador" > /dev/null 2>&1; then
        exec "$navegador" --app="$url"
    fi
done
exec xdg-open "$url"
