# shellcheck shell=bash
# El central de prueba del CI de los instaladores en Ubuntu (ADR-014): levantarlo y hablarle.
#
#     bash .github/central-de-prueba.sh levantar <producto>    (un paso: queda corriendo para los siguientes)
#     . .github/central-de-prueba.sh                           (los otros pasos: emitir, secreto_ok, ...)
#
# Es tests/central_de_prueba.py: el router del motor (create_nodos_router y create_canje_router) con https de verdad
# en localhost:8443, y su autoridad instalada en el almacen del sistema (update-ca-certificates), que es contra lo que
# verifica el Python del nodo. Ningun secreto ni codigo se imprime: van por la entrada de curl y jq.

CENTRAL_DIR="$RUNNER_TEMP/central"
DIRECCION="localhost:8443"
BASE="https://$DIRECCION"

levantar() {
    local producto="$1"
    mkdir -p "$CENTRAL_DIR"
    nohup uv run --extra server --with trustme python tests/central_de_prueba.py --producto "$producto" \
        --dir "$CENTRAL_DIR" > "$RUNNER_TEMP/central.log" 2>&1 &
    for _ in $(seq 1 180); do
        [ -f "$CENTRAL_DIR/listo" ] && break
        sleep 1
    done
    [ -f "$CENTRAL_DIR/listo" ] || { cat "$RUNNER_TEMP/central.log"; echo "El central de prueba no arranco"; return 1; }
    sudo cp "$CENTRAL_DIR/ca.pem" /usr/local/share/ca-certificates/libraedge-prueba.crt
    sudo update-ca-certificates > /dev/null
    curl -fsS "$BASE/api/nodos-offline" > /dev/null  # ya con el almacen del sistema, sin --cacert
}

# POST a la administracion: emitir /vinculacion '{"sucursal_id": "1"}'  |  emitir /<node_id>/vinculacion
emitir() {
    if [ -n "${2:-}" ]; then
        curl -fsS -X POST -H 'content-type: application/json' -d "$2" "$BASE/api/nodos-offline$1"
    else
        curl -fsS -X POST "$BASE/api/nodos-offline$1"
    fi
}

# El codigo HTTP de canjear un codigo (200, 404...). El codigo va por la entrada de curl.
canje_http() {
    jq -n --arg c "$1" --arg p "${PRODUCTO_DE_PRUEBA:-}" '{codigo: $c} + (if $p == "" then {} else {producto: $p} end)' \
        | curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d @- "$BASE/sync/v1/vinculacion"
}

secreto_del_nodo() { sudo sed -n 's/^LIBRAEDGE_NODE_SECRET=//p' /etc/libraedge-nodo/nodo.env; }
huella_del_secreto() { secreto_del_nodo | sha256sum; }

# El nodo.env tiene el nodo emitido y un secreto que el central verifica.
secreto_ok() {
    sudo grep -qx "LIBRAEDGE_NODE_ID=$1" /etc/libraedge-nodo/nodo.env || { echo "nodo.env no tiene el nodo $1"; return 1; }
    secreto_del_nodo | jq -R --arg n "$1" '{node_id: $n, secreto: .}' \
        | curl -fsS -X POST -H 'content-type: application/json' -d @- "$BASE/prueba/secreto" | jq -e .ok > /dev/null \
        || { echo "El secreto del nodo.env no es el que verifica el central"; return 1; }
    echo "El nodo $1 quedo con un secreto que el central verifica."
}

# Ni el secreto del nodo, ni la clave de PostgreSQL, ni ningun codigo emitido en los logs.
sin_secretos_en_los_logs() {
    local dir="$RUNNER_TEMP/logs-codigo"
    mkdir -p "$dir"
    sudo journalctl -u 'libraedge-*' --no-pager 2> /dev/null | tee "$dir/journal.log" > /dev/null || true
    sudo cp -r /var/log/libraedge-nodo "$dir/" 2> /dev/null || true
    sudo cp /var/log/apt/term.log /var/log/dpkg.log "$dir/" 2> /dev/null || true
    sudo chown -R "$(id -u)" "$dir"
    cp "$RUNNER_TEMP"/*.log "$dir/"
    local buscados=()
    buscados+=("$(secreto_del_nodo)")
    buscados+=("$(sudo cat /var/lib/libraedge-nodo/postgres.clave)")
    if [ -f "$RUNNER_TEMP/codigos.txt" ]; then
        while IFS= read -r c; do
            [ -n "$c" ] && buscados+=("$c" "${c/-/}")
        done < "$RUNNER_TEMP/codigos.txt"
    fi
    for b in "${buscados[@]}"; do
        [ -n "$b" ] || continue
        if grep -rqF -- "$b" "$dir"; then
            echo "Un secreto o un codigo aparece en: $(grep -rlF -- "$b" "$dir" | tr '\n' ' ')"
            return 1
        fi
    done
    echo "Ni el secreto, ni la clave de PostgreSQL, ni los ${#buscados[@]} valores buscados aparecen en $(find "$dir" -type f | wc -l) logs."
}

if [ "${1:-}" = levantar ]; then
    set -euo pipefail
    levantar "$2"
fi
