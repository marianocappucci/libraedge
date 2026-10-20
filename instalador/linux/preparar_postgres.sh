#!/usr/bin/env bash
# Prepara el PostgreSQL del nodo: clave, cluster propio, servicio y bases.
# El preparar_postgres.ps1 de Windows, con el PostgreSQL 16 de PGDG
# (apt.postgresql.org) y las herramientas de postgresql-common.
#
#     preparar_postgres.sh <bases separadas por coma>
#
# Lo corre instalar.sh como root. Es idempotente: en una reinstalacion no se
# toca el cluster, pero se crean las bases nuevas de la definicion.
#
# La clave del superusuario no se pide ni pasa por la linea de comandos: se
# genera la primera vez en postgres.clave (root, 600) y se le pone al rol por
# la ENTRADA de psql, no por un argumento.
set -euo pipefail
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/comun.sh"

[ $# -eq 1 ] || morir "uso: preparar_postgres.sh <bases separadas por coma>"
IFS=, read -r -a bases <<< "$1"
[ "${#bases[@]}" -gt 0 ] || morir "No hay bases que crear."
for base in "${bases[@]}"; do
    [[ "$base" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || morir "Nombre de base no valido: $base"
done

for programa in pg_createcluster pg_lsclusters pg_ctlcluster "/usr/lib/postgresql/$PG_VERSION/bin/initdb"; do
    command -v "$programa" > /dev/null || morir "Falta $programa: hace falta postgresql-$PG_VERSION (PGDG)."
done

# --- La clave ----------------------------------------------------------------
# Hex de 64: nada que codificar al meterla en una URL de conexion.
hay_cluster=0
pg_lsclusters --no-header | awk '{print $1"/"$2}' | grep -qx "$PG_VERSION/$PG_CLUSTER" && hay_cluster=1
install -d -m 755 "$DATOS"
if [ ! -s "$CLAVE_PG" ]; then
    if [ "$hay_cluster" = 1 ]; then
        morir "Hay un cluster $PG_VERSION/$PG_CLUSTER pero no su clave en $CLAVE_PG. Si es de una instalacion" \
              "anterior, escribir ahi la clave con la que se creo (una linea) y volver a correr."
    fi
    ( umask 077; od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "$CLAVE_PG" )
    registrar "Clave del PostgreSQL del nodo generada en $CLAVE_PG (solo root)."
fi
chown root:root "$CLAVE_PG"
chmod 600 "$CLAVE_PG"
# Los volcados de libraedge-respaldo (corre como postgres): la base entera, solo para postgres.
install -d -o postgres -g postgres -m 700 "$RESPALDOS"

# --- El cluster ------------------------------------------------------------------
if [ "$hay_cluster" = 1 ]; then
    registrar "El cluster $PG_VERSION/$PG_CLUSTER ya existe: no se re-inicializa."
else
    # La zona se fija EN EL INITDB: queda escrita en postgresql.conf y despues
    # cambiar TZ mueve el reloj del proceso, no el del servidor (reglas del wiki).
    # --data-checksums: un nodo que sufre cortes de luz tiene que enterarse de
    # una pagina rota al leerla, no meses despues (ver preparar_postgres.ps1).
    # ICU en-US como en Windows, para que todos los nodos ordenen igual.
    TZ="$ZONA" pg_createcluster "$PG_VERSION" "$PG_CLUSTER" --port "$PG_PUERTO" --start-conf auto \
        --encoding UTF8 --locale C.UTF-8 -- \
        --data-checksums --locale-provider=icu --icu-locale=en-US \
        --auth-local=peer --auth-host=scram-sha-256
    registrar "Cluster $PG_VERSION/$PG_CLUSTER creado."
fi

# Se aplica siempre (idempotente): en el camino de reparacion -cluster que
# existe y servicio que no arranca- tiene que estar igual.
conf_d="/etc/postgresql/$PG_VERSION/$PG_CLUSTER/conf.d"
install -d -o postgres -g postgres -m 755 "$conf_d"
cat > "$conf_d/libraedge.conf" <<EOF
# --- LibraEdge (lo escribe preparar_postgres.sh; se reescribe al reinstalar) ---
port = $PG_PUERTO
timezone = '$ZONA'
log_timezone = '$ZONA'
# Solo local: el nodo y el producto corren en esta PC. Exponer la base a la red
# del local seria una superficie que nadie va a vigilar.
listen_addresses = '127.0.0.1'
EOF
chown postgres:postgres "$conf_d/libraedge.conf"
sed -i 's/^\s*\(auto\|manual\|disabled\)\s*$/auto/' "/etc/postgresql/$PG_VERSION/$PG_CLUSTER/start.conf"

systemctl daemon-reload
systemctl enable "$PG_SERVICIO" > /dev/null 2>&1
systemctl start "$PG_SERVICIO"

# Arrancado no quiere decir que acepte conexiones: la recuperacion puede seguir.
listo=0
for _ in $(seq 1 60); do
    if "$PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$PG_PUERTO"; then listo=1; break; fi
    sleep 1
done
[ "$listo" = 1 ] || morir "PostgreSQL arranco pero no acepta conexiones en 127.0.0.1:$PG_PUERTO."

# La clave del rol, por la entrada de psql (nunca en un argumento). Siempre: asi
# una clave reescrita a mano en postgres.clave vuelve a coincidir con el rol.
{
    printf "SET log_statement = 'none';\n"
    printf "ALTER ROLE postgres PASSWORD '%s';\n" "$(cat "$CLAVE_PG")"
} | psql_pg -d postgres -v ON_ERROR_STOP=1 > /dev/null || morir "No se pudo poner la clave del rol postgres."

for base in "${bases[@]}"; do
    if [ "$(psql_pg -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$base'")" = "1" ]; then
        registrar "La base $base ya existe."
    else
        psql_pg -d postgres -c "CREATE DATABASE $base" || morir "No se pudo crear la base $base."
        registrar "Base $base creada."
    fi
done

# --- Las dos verificaciones que no se pueden posponer ------------------------------
# Las dos miran el servidor y no los flags: initdb puede haber salido con 0 sin
# tomarlos, y las dos son irreversibles sin rehacer el cluster.
ahora="$(psql_pg -d "${bases[0]}" -tAc "SELECT now()")"
registrar "PostgreSQL listo en 127.0.0.1:$PG_PUERTO. now() del servidor: $ahora"
case "$ahora" in
    *-03) ;;
    *) aviso "La zona NO quedo en -03 (now() = $ahora): rehacer el cluster ANTES de cargar datos." ;;
esac
orden="$(psql_pg -d "${bases[0]}" -tAc \
    "SELECT datlocprovider, coalesce(daticulocale, datcollate) FROM pg_database WHERE datname = current_database()")"
if [ -z "$orden" ]; then
    aviso "No se pudo leer el collation del cluster."
else
    registrar "Collation del nodo: $orden   (el central es c|en_US.utf8)"
    case "$orden" in *"|C") aviso "El cluster quedo en collation C: ordena por bytes. Rehacer el cluster." ;; esac
fi
[ "$(psql_pg -d postgres -tAc 'SHOW data_checksums')" = "on" ] || aviso "data_checksums no quedo prendido."
