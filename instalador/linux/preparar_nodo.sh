#!/usr/bin/env bash
# Entorno, config.json del emisor, migraciones, primera sincronizacion y los
# servicios del nodo. El preparar_nodo.ps1 de Windows.
#
#     preparar_nodo.sh [<carga-nodo.json>]
#
# Con la carga escribe (o reescribe) el nodo.env; sin ella usa el que ya esta:
# es la actualizacion por apt, que no vuelve a pedir la carga. Lo propio del
# producto sale de producto.json por medio del ayudante (libraedge/instalador.py
# tal cual), igual que en Windows.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

carga="${1:-}"
for archivo in "$PYTHON" "$AYUDANTE" "$DEFINICION"; do
    [ -e "$archivo" ] || morir "Falta $archivo: el paquete esta incompleto."
done

# El Python propio tiene que ver sus paquetes y el producto (por el .pth).
py -c "import alembic, uvicorn, libraedge, app" \
    || morir "El Python del nodo no puede importar alembic, uvicorn, libraedge o el producto (app)."

nombre="$(plan_campo 'p["nombre"]')"
registrar "Producto: $nombre (puerto $(plan_campo 'p["puerto"]'), base del nodo $(plan_campo 'p["base_del_nodo"]'))."

# --- Las carpetas y sus duenos -------------------------------------------------
# DATA_DIR (config.json, backups del producto): solo el usuario del servicio.
# estado/: la lee la bandeja con el usuario de la sesion (el estado no tiene secretos).
install -d -m 755 "$DATOS"
install -d -o "$USUARIO" -g "$USUARIO" -m 700 "$DATA_DIR"
install -d -o "$USUARIO" -g "$USUARIO" -m 755 "$ESTADO_DIR"
install -d -m 755 "$CONFIG"

# --- El archivo de entorno ---------------------------------------------------------
# Tiene el secreto del nodo. Se crea con 600 ANTES de escribirle nada (el ayudante
# solo escribe sobre un archivo que ya existe) y no se borra si ya estaba: el
# ayudante conserva SECRET_KEY y la clave del admin local.
if [ -n "$carga" ]; then
    [ -e "$ARCHIVO_ENV" ] || install -m 600 -o root -g root /dev/null "$ARCHIVO_ENV"
    chown root:root "$ARCHIVO_ENV"
    chmod 600 "$ARCHIVO_ENV"
    # Despues de un apt remove el nodo.env ya no esta (tenia el secreto del nodo), pero si lo conservado.
    previo=()
    [ ! -s "$CONSERVADO" ] || previo=(--previo "$CONSERVADO")
    ayudante entorno --definicion "$DEFINICION" --carga "$carga" --clave-pg "$CLAVE_PG" \
        --estado "$ESTADO" --data-dir "$DATA_DIR" --salida "$ARCHIVO_ENV" "${previo[@]}" \
        || morir "No se pudo escribir $ARCHIVO_ENV. El motivo esta arriba."
    if [ "${#previo[@]}" -gt 0 ]; then
        registrar "Se recuperaron SECRET_KEY y la clave del admin local de la instalacion anterior."
        rm -f "$CONSERVADO"
    fi

    # CUIT, razon social, condicion de IVA, formato del ticket: los lee la
    # facturacion del nodo del config.json de DATA_DIR. Se fusionan.
    ayudante config-emisor --definicion "$DEFINICION" --carga "$carga" --config "$DATA_DIR/config.json" \
        || morir "No se pudo escribir el config.json del emisor."
    [ ! -e "$DATA_DIR/config.json" ] || chown "$USUARIO:$USUARIO" "$DATA_DIR/config.json"
else
    [ -s "$ARCHIVO_ENV" ] || morir "No hay $ARCHIVO_ENV ni carga: falta configurar el nodo con su carga."
fi
[ "$(stat -c '%U:%G %a' "$ARCHIVO_ENV")" = "root:root 600" ] || morir "$ARCHIVO_ENV no quedo root:root 600."
ayudante revisar-entorno --definicion "$DEFINICION" --entorno "$ARCHIVO_ENV" \
    || morir "Al $ARCHIVO_ENV le faltan variables (arriba los nombres)."

# --- Las migraciones -----------------------------------------------------------------
# Las MISMAS que el central y en el mismo orden (las cuatro cadenas de
# scripts/panel_admin.py). Corren como el usuario del servicio, con el entorno
# cargado DENTRO del proceso del ayudante (ni env VAR=... ni source).
correr_como_servicio() {
    (cd "$PRODUCTO_DIR" && ayudante ejecutar --entorno "$ARCHIVO_ENV" --como "$USUARIO" "$@")
}
while IFS= read -r comando; do
    mapfile -t partes < <(py -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])))' "$comando")
    registrar "Migraciones: ${partes[*]}"
    correr_como_servicio "${partes[@]}" || morir "Fallo '${partes[*]}'."
done < <(plan_campo '"\n".join(json.dumps(m) for m in p["migraciones"])')

# --- La primera bajada, ANTES de arrancar el producto ------------------------------
# Si el espejo baja los usuarios del central, el producto no crea un admin local.
# Sin central (o con la carga de prueba) se avisa y se sigue: el servicio reintenta.
registrar "Primera sincronizacion con el central..."
if ! correr_como_servicio libraedge-nodo sincronizar > /dev/null; then
    aviso "La primera sincronizacion no termino bien. El nodo queda instalado y reintenta solo."
fi

# --- Los servicios ----------------------------------------------------------------------
systemctl daemon-reload
systemctl enable "${SERVICIOS_NODO[@]}" libraedge-respaldo.timer libraedge-estado.path > /dev/null 2>&1
systemctl restart "${SERVICIOS_NODO[@]}"
systemctl start libraedge-respaldo.timer libraedge-estado.path
registrar "Servicios: $(systemctl is-active "${SERVICIOS_NODO[@]}" | tr '\n' ' ')"

# --- El firewall ----------------------------------------------------------------------------
# Las cajas son navegadores en la red del local. Si ufw esta activo se abre el
# puerto solo para las redes privadas; si no, no se toca nada.
puerto="$(plan_campo 'p["puerto"]')"
if command -v ufw > /dev/null && ufw status 2> /dev/null | grep -q "^Status: active"; then
    for red in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
        ufw allow proto tcp from "$red" to any port "$puerto" comment "LibraEdge producto" > /dev/null
    done
    registrar "Firewall (ufw): puerto $puerto abierto para las redes privadas."
else
    registrar "Firewall: ufw no esta activo; no se agrega ninguna regla."
fi

registrar "Instalado. $nombre escucha en http://<esta-pc>:$puerto"
registrar "Verificar con: sudo libraedge-nodo-verificar"
