#!/usr/bin/env bash
# Respaldo de las bases del nodo, con verificacion. El respaldo.ps1 de Windows.
#
#     respaldo.sh <bases separadas por coma> <base del nodo> [destino secundario]
#
# Lo corre libraedge-respaldo.service (timer cada hora) como el usuario
# postgres: entra por el socket local con autenticacion peer, asi que no lee la
# clave de ningun lado. Tambien se puede correr a mano:
#
#     sudo systemctl start libraedge-respaldo.service && journalctl -u libraedge-respaldo -n 30
#
# POR QUE EXISTE: el sync_outbox es el unico lugar donde vive una venta cobrada
# sin internet que todavia no llego al central. QUE NO RESUELVE: un respaldo en
# el MISMO disco no protege de que el disco se rompa; para eso, el tercer
# argumento (LIBRAEDGE_RESPALDO_SECUNDARIO en /etc/libraedge-nodo/respaldo.env):
# un pendrive o una carpeta de red.
set -uo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

[ $# -ge 2 ] || morir "uso: respaldo.sh <bases> <base del nodo> [destino secundario]"
IFS=, read -r -a bases <<< "$1"
base_nodo="$2"
secundario="${3:-${LIBRAEDGE_RESPALDO_SECUNDARIO:-}}"
conservar="${LIBRAEDGE_RESPALDO_CONSERVAR:-48}"  # por base: a uno por hora, dos dias
printf '%s\n' "${bases[@]}" | grep -qx "$base_nodo" || morir "La base del nodo ($base_nodo) no esta entre las bases."

# Los volcados tienen la base entera (usuarios con sus hashes incluidos).
umask 077
[ -d "$RESPALDOS" ] || morir "No esta $RESPALDOS (la crea el paquete, del usuario postgres)."
sello="$(date +%Y%m%d-%H%M%S)"

consultar() {  # consultar BASE SQL
    "$PG_BIN/psql" -X -q -p "$PG_PUERTO" -d "$1" -tAc "$2" 2>&1
}

# Lo que se compara entre la base viva y el respaldo restaurado.
medir() {  # medir BASE ES_NODO
    local texto
    texto="tablas=$(consultar "$1" "SELECT count(1) FROM information_schema.tables WHERE table_schema = 'public'")"
    if [ "$2" = 1 ]; then
        texto+=", sin_confirmar=$(consultar "$1" "SELECT count(1) FROM sync_outbox WHERE status <> 'acknowledged'")"
        if [ "$(consultar "$1" "SELECT to_regclass('public.sales') IS NOT NULL")" = "t" ]; then
            texto+=", ventas=$(consultar "$1" "SELECT count(1) FROM sales")"
        fi
    fi
    echo "$texto"
}

# Una base. Corre en un subshell (por el trap que borra la base de verificacion);
# cada paso se chequea a mano porque `set -e` no rige dentro de un `( ... ) ||`.
respaldar_base() {  # respaldar_base BASE ES_NODO
    local base="$1" es_nodo="$2" archivo vivo tam verificacion copia a b
    archivo="$RESPALDOS/nodo-$base-$sello.dump"
    vivo="$(medir "$base" "$es_nodo")"
    echo "[$base] en la base: $vivo."
    "$PG_BIN/pg_dump" -p "$PG_PUERTO" -d "$base" -Fc -f "$archivo" || { echo "pg_dump fallo."; return 1; }
    tam="$(stat -c %s "$archivo")"
    echo "[$base] escrito: $(basename "$archivo") ($tam bytes)"
    [ "$tam" -ge 1024 ] || { echo "El respaldo pesa $tam bytes: no puede estar bien."; return 1; }

    # LA VERIFICACION: se restaura en una base descartable y se cuenta. pg_dump
    # puede salir con 0 y dejar un archivo que no restaura. Con el PID: dos
    # corridas a la vez no comparten la base (paso en la VM de Windows).
    verificacion="libraedge_verificar_respaldo_${BASHPID}"
    consultar postgres "DROP DATABASE IF EXISTS $verificacion" > /dev/null
    consultar postgres "CREATE DATABASE $verificacion" > /dev/null
    trap 'consultar postgres "DROP DATABASE IF EXISTS $verificacion" > /dev/null' EXIT
    # pg_restore avisa por cosas inofensivas: lo que decide son las medidas.
    "$PG_BIN/pg_restore" -p "$PG_PUERTO" -d "$verificacion" --no-owner --no-privileges "$archivo" > /dev/null 2>&1
    copia="$(medir "$verificacion" "$es_nodo")"
    echo "[$base] en el respaldo: $copia."
    if [ "$copia" != "$vivo" ]; then
        echo "El respaldo NO coincide con la base ($copia contra $vivo). Se conserva el archivo, pero no cuenta."
        return 1
    fi
    echo "[$base] verificado: el respaldo restaura y las medidas coinciden."

    if [ -n "$secundario" ]; then
        if [ -d "$secundario" ]; then
            cp "$archivo" "$secundario/" || { echo "No se pudo copiar a $secundario."; return 1; }
            # Un pendrive que se desconecto a mitad deja el tamano bien y el contenido cortado.
            a="$(sha256sum < "$archivo")"; b="$(sha256sum < "$secundario/$(basename "$archivo")")"
            [ "$a" = "$b" ] || { echo "La copia en $secundario no coincide con el original."; return 1; }
            echo "[$base] segunda copia en $secundario, con el mismo SHA256."
        else
            echo "AVISO: no existe $secundario: la segunda copia NO se hizo (un pendrive desconectado?)."
        fi
    fi

    # Rotacion: DESPUES de verificar el nuevo.
    find "$RESPALDOS" -maxdepth 1 -name "nodo-$base-*.dump" -printf '%T@ %p\n' | sort -rn \
        | tail -n +"$((conservar + 1))" | cut -d' ' -f2- | xargs -r rm -f --
    echo "[$base] quedan $(find "$RESPALDOS" -maxdepth 1 -name "nodo-$base-*.dump" | wc -l) respaldos en $RESPALDOS."
}

fallos=0
for base in "${bases[@]}"; do
    es_nodo=0; [ "$base" = "$base_nodo" ] && es_nodo=1
    ( respaldar_base "$base" "$es_nodo" ) || { echo "AVISO: [$base] el respaldo fallo."; fallos=$((fallos + 1)); }
done
if [ -z "$secundario" ]; then
    echo "AVISO: sin destino secundario el respaldo queda en el MISMO disco que la base." \
         "Protege de una base corrupta, no de un disco roto."
fi
[ "$fallos" = 0 ]
