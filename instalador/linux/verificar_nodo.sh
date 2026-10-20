#!/usr/bin/env bash
# Verifica un nodo instalado en Linux. Es el verificar_nodo.ps1 de Windows.
#
#     sudo libraedge-nodo-verificar [--sin-central] [--intervalo 60]
#
# Corre como root (lee nodo.env, que es root 600) y NO imprime ningun valor
# secreto: del nodo.env solo nombra variables. Sale con la cantidad de chequeos
# que fallaron (0 = todo bien). Con --sin-central, que el nodo no vea al central
# es un AVISO: es lo esperable con la carga de prueba o con el local sin internet.
set -uo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

sin_central=0
intervalo=60
while [ $# -gt 0 ]; do
    case "$1" in
        --sin-central) sin_central=1; shift ;;
        --intervalo) intervalo="$2"; shift 2 ;;
        *) echo "uso: libraedge-nodo-verificar [--sin-central] [--intervalo SEGUNDOS]" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "Correr como root: sudo libraedge-nodo-verificar" >&2; exit 2; }

fallas=0
bien()  { echo "OK     $*"; }
avisa() { echo "AVISO  $*"; }
falla() { echo "FALLA  $*"; fallas=$((fallas + 1)); }

for archivo in "$PYTHON" "$AYUDANTE" "$DEFINICION" "$ARCHIVO_ENV" "$CLAVE_PG" "$PG_BIN/psql"; do
    [ -e "$archivo" ] || { falla "No esta $archivo"; exit "$fallas"; }
done
nombre="$(plan_campo 'p["nombre"]')"
echo "Nodo LibraEdge de $nombre en $RAIZ_NODO"
echo

# --- 1. Los servicios -------------------------------------------------------------
for servicio in "$PG_SERVICIO" "${SERVICIOS_NODO[@]}" "$SERVICIO_DESCUBRIMIENTO" libraedge-respaldo.timer libraedge-estado.path; do
    activo="$(systemctl is-active "$servicio" 2> /dev/null)"
    habilitado="$(systemctl is-enabled "$servicio" 2> /dev/null)"
    if [ "$activo" = active ] && [ "$habilitado" = enabled ]; then
        bien "$servicio: activo, arranca solo"
    else
        falla "$servicio: ${activo:-?}, ${habilitado:-?}"
    fi
done

# --- 2. El archivo de entorno -------------------------------------------------------
permisos="$(stat -c '%U:%G %a' "$ARCHIVO_ENV")"
if [ "$permisos" = "root:root 600" ]; then bien "nodo.env: root:root 600"
else falla "nodo.env: $permisos (tiene el secreto del nodo: tiene que ser root:root 600)"; fi
permisos="$(stat -c '%U:%G %a' "$CLAVE_PG")"
if [ "$permisos" = "root:root 600" ]; then bien "postgres.clave: root:root 600"
else falla "postgres.clave: $permisos (tiene la clave de la base: tiene que ser root:root 600)"; fi
if [ "$(head -c 3 "$ARCHIVO_ENV" | od -An -tx1 | tr -d ' ')" = "efbbbf" ]; then falla "nodo.env: tiene BOM"; fi
if faltan="$(ayudante revisar-entorno --definicion "$DEFINICION" --entorno "$ARCHIVO_ENV")"; then
    bien "nodo.env: estan todas las variables del producto"
else
    falla "nodo.env: faltan variables -> $(echo "$faltan" | tr '\n' ';')"
fi

# --- 3. PostgreSQL ----------------------------------------------------------------------
consultar() { psql_pg -d "$1" -tAc "$2" 2>&1; }
if ! ahora="$(consultar postgres "SELECT now()")"; then
    falla "PostgreSQL no acepta conexiones en el puerto $PG_PUERTO"
else
    case "$ahora" in
        *-03) bien "PostgreSQL arriba; now() = $ahora (zona -03)" ;;
        *) falla "PostgreSQL arriba pero now() = $ahora: la zona no es -03" ;;
    esac
    checksums="$(consultar postgres "SHOW data_checksums")"
    if [ "$checksums" = on ]; then bien "PostgreSQL: data_checksums on"
    else falla "PostgreSQL: data_checksums $checksums"; fi
    escucha="$(consultar postgres "SHOW listen_addresses")"
    if [ "$escucha" = "127.0.0.1" ]; then bien "PostgreSQL: solo en 127.0.0.1"
    else falla "PostgreSQL escucha en $escucha"; fi

    # --- 4. Migraciones: cada cadena de Alembic deja su alembic_version* con una fila.
    cadenas=0
    for base in $(plan_campo '" ".join(b["nombre"] for b in p["bases"])'); do
        for tabla in $(consultar "$base" "SELECT table_name FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name LIKE 'alembic_version%' ORDER BY 1"); do
            version="$(consultar "$base" "SELECT string_agg(version_num, ',') FROM $tabla")"
            if [ -n "$version" ]; then cadenas=$((cadenas + 1)); echo "       $base.$tabla = $version"
            else falla "migraciones: $base.$tabla esta vacia"; fi
        done
    done
    esperadas="$(plan_campo 'len(p["migraciones"])')"
    if [ "$cadenas" -ge "$esperadas" ]; then bien "migraciones: $cadenas cadenas aplicadas (la definicion corre $esperadas)"
    else falla "migraciones: $cadenas cadenas aplicadas y la definicion corre $esperadas"; fi
    base_nodo="$(plan_campo 'p["base_del_nodo"]')"
    if pendientes="$(psql_pg -d "$base_nodo" -tAc "SELECT count(1) FROM sync_outbox" 2> /dev/null)"; then
        bien "migraciones: sync_outbox existe en $base_nodo ($pendientes operaciones)"
    else
        falla "migraciones: no hay sync_outbox en $base_nodo"
    fi
fi

# --- 5. libraedge-nodo estado (con el entorno del servicio y como su usuario) ------------
if estado="$(cd "$PRODUCTO_DIR" && ayudante ejecutar --entorno "$ARCHIVO_ENV" --como "$USUARIO" libraedge-nodo estado 2>&1)"; then
    bien "libraedge-nodo estado: contesta"
    en_linea="$(echo "$estado" | py -c 'import json, sys; print(json.load(sys.stdin).get("en_linea"))' 2> /dev/null)"
    if [ "$en_linea" = True ]; then bien "nodo en linea con el central"
    else
        error="$(echo "$estado" | py -c 'import json, sys; print(json.load(sys.stdin).get("ultimo_error"))' 2> /dev/null)"
        if [ "$sin_central" = 1 ]; then avisa "nodo sin conexion con el central (esperado con --sin-central): $error"
        else falla "nodo sin conexion con el central: $error"; fi
    fi
else
    falla "libraedge-nodo estado: sin estado ($estado)"
fi

# --- 6. El archivo de estado (lo lee la bandeja con el usuario de la sesion) ------------
if [ ! -f "$ESTADO" ]; then
    falla "estado.json: no existe ($ESTADO)"
else
    edad=$(( $(date +%s) - $(stat -c %Y "$ESTADO") ))
    limite=$(( 3 * intervalo + 30 ))
    if [ "$edad" -le "$limite" ]; then bien "estado.json: escrito hace $edad s"
    else falla "estado.json: escrito hace $edad s (mas de $limite s: el servicio del nodo no cicla)"; fi
    if runuser -u nobody -- test -r "$ESTADO"; then bien "estado.json: legible para la bandeja"
    else falla "estado.json: la bandeja (usuario de la sesion) no lo puede leer"; fi
fi

# --- 7. El producto respondiendo: la salud tiene que ser un objeto JSON (la SPA
# contesta 200 con el index.html a cualquier ruta, y eso no prueba nada).
url="http://127.0.0.1:$(plan_campo 'p["puerto"]')$(plan_campo 'p["salud"]')"
if py - "$url" <<'EOF'
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=10) as r:
    sys.exit(0 if r.status == 200 and isinstance(json.load(r), dict) else 1)
EOF
then bien "$nombre responde en $url"
else falla "$nombre no responde un objeto JSON en $url"; fi

# --- 7b. Las otras cajas encuentran esta PC (ADR-017): el que contesta, preguntandole como una caja ----
producto="$(plan_campo 'p["producto"]')"
if [ "$(stat -c '%a' "$DESCUBRIMIENTO" 2> /dev/null)" != 644 ]; then
    falla "descubrimiento.json: falta o no es 0644 ($DESCUBRIMIENTO)"
elif py "$RAIZ_NODO/instalador/libraedge_descubrimiento.py" buscar --producto "$producto" --a 127.0.0.1 --espera 3 > /dev/null; then
    bien "descubrimiento: contesta en UDP $PUERTO_DESCUBRIMIENTO a las cajas de $nombre"
else
    falla "descubrimiento: no contesta en UDP $PUERTO_DESCUBRIMIENTO (las otras cajas no encuentran esta PC)"
fi

# --- 8. El emisor ----------------------------------------------------------------------------
config="$DATA_DIR/config.json"
if [ -f "$config" ]; then
    if iva="$(py - "$config" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
if not (c.get("empresa_cuit") and c.get("empresa_iva_condition")):
    sys.exit(1)
print(c["empresa_iva_condition"])
EOF
    )"; then bien "config.json: emisor con CUIT y condicion de IVA ($iva)"
    else avisa "config.json: falta CUIT o condicion de IVA del emisor; el nodo no va a poder facturar"; fi
else
    avisa "config.json: no existe en $DATA_DIR; el nodo no va a poder facturar"
fi

# --- 9. El respaldo y la bandeja ----------------------------------------------------------------
proximo="$(systemctl show libraedge-respaldo.timer -p NextElapseUSecRealtime --value 2> /dev/null)"
[ -z "$proximo" ] || echo "       respaldo: el proximo es $proximo"
if [ -f /etc/xdg/autostart/libraedge-nodo-bandeja.desktop ]; then bien "bandeja: arranca al iniciar sesion"
else falla "bandeja: falta /etc/xdg/autostart/libraedge-nodo-bandeja.desktop"; fi

echo
if [ "$fallas" = 0 ]; then echo "Nodo verificado: todo bien."; else echo "Nodo con $fallas fallas."; fi
exit "$fallas"
