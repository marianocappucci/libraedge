"""Los ids que el nodo se inventa y el central reescribe: el id provisorio y el mapeo (ADR-021).

Un nodo puede dar de alta una fila de referencia (hoy, un cliente) sin ver al central. Esa fila necesita un id YA, y el
del central no se conoce: si el nodo usara el siguiente de su propia secuencia chocaria con las filas del central (que
llegan por el espejo con **su** id) o las pisaria. La decision del humano (2026-10-21) es que **el central asigna el
id definitivo y reescribe las referencias**. Este modulo es la mitad de datos de esa decision:

- **`siguiente_id_provisorio(conn)`**: el id con que el nodo guarda la fila hasta que el central le asigne el suyo. Es
  **negativo** (-1, -2, ...): un id del central es siempre positivo, asi que un provisorio no se cruza nunca con uno.
  Sale de `local_sequences`, en la transaccion de quien llama, y **no se reusa jamas**: el central anota el
  mapeo `(nodo, tabla, id_nodo)` y un id reusado apuntaria a otra fila.
- **`libraedge_mapeo`**: la tabla `(node_id, tabla, id_nodo, id_central)` que el central llena al aplicar el alta. Es
  lo unico que sabe que el `-3` del nodo S02 es el `57` del central.
- **`traducir_id(conn, node_id, tabla, id_nodo)`**: el id del central para un id del nodo. Lo usa el aplicador de
  cada operacion que lleva una referencia (una venta con su `cliente_id`).

## Por que el mapeo se espeja y no hay un `uid`

La primera idea fue una columna `uid` en la tabla (un UUID que viaja con el alta) para que el nodo reconozca, al
bajar la fila del central, su fila provisoria. No alcanza: si el central **deduplica** (el CUIT ya existia, o dos
nodos dieron de alta el mismo) la fila que baja es otra y su `uid` no es el del nodo. El mapeo, en cambio, dice
exactamente lo que el central decidio, sea cual sea el motivo. Por eso `libraedge_mapeo` es una tabla mas del espejo
(la publica el central y cada nodo se queda con las filas **suyas**) y es ella la que dispara el reapuntado
(`sync/reconciliacion.py`). Ver ADR-021.
"""

from datetime import UTC, datetime

from libraedge.db.changelog import validar_identificador

#: La tabla del mapeo. Va en el espejo del nodo con esta clave.
TABLA = "libraedge_mapeo"
CLAVE = ("node_id", "tabla", "id_nodo")

#: El nombre de la secuencia en `local_sequences`. Una sola por base: un id provisorio es unico en todas las tablas.
SECUENCIA_PROVISORIA = "id_provisorio"


class SinMapeo(ValueError):
    """Un id provisorio (negativo) del nodo que el central no conoce: su alta no se aplico.

    Es un `ValueError` a proposito: el receptor lo manda a revision manual y el nodo no traba la cola (ADR-020).
    Al resolver el alta, se reintenta esta operacion (en el orden en que se crearon)."""


class MapeoEnConflicto(ValueError):
    """El central ya tiene un mapeo distinto para ese `(nodo, tabla, id_nodo)`: el nodo reuso un id provisorio
    (una PC reemplazada que arranco la secuencia de cero con el mismo `node_id`). Revision manual."""


def es_provisorio(id_) -> bool:
    """Si `id_` es un id provisorio de un nodo: un entero negativo."""
    return isinstance(id_, int) and not isinstance(id_, bool) and id_ < 0


def siguiente_id_provisorio(conn) -> int:
    """El proximo id provisorio del nodo, negativo y nunca repetido. **No commitea**: va en la transaccion del alta.

    Si la transaccion se deshace, el numero se deshace con ella (y como la operacion del alta se encola en la misma
    transaccion, no hay un id que haya llegado al central sin haber existido). Atomico entre procesos: el `UPDATE` toma
    la fila y la mantiene hasta el commit, asi que dos altas simultaneas no reciben el mismo numero."""
    conn.execute(
        "INSERT INTO local_sequences (name, next_value) VALUES (?, 1) ON CONFLICT (name) DO NOTHING",
        (SECUENCIA_PROVISORIA,),
    )
    # `fetchall` y no `fetchone`: en SQLite un `RETURNING` aplica el cambio recien al agotar el cursor.
    filas = conn.execute(
        "UPDATE local_sequences SET next_value = next_value + 1 WHERE name = ? RETURNING next_value",
        (SECUENCIA_PROVISORIA,),
    ).fetchall()
    return -(int(filas[0][0]) - 1)


def traducir_id(conn, node_id: str, tabla: str, id_nodo, *, exigir: bool = True):
    """El id del central para un id del nodo `node_id`, en `tabla`.

    - `None` -> `None`; un id **no negativo** ya es del central (el nodo uso una fila espejada): vuelve igual.
    - Un id provisorio (negativo) se busca en `libraedge_mapeo`. Si no esta: `SinMapeo` (con `exigir=True`, el default;
      es lo que hace que una venta que lleva un cliente que no subio sea rechazada en vez de quedar a nombre de otro) o
      `None` (con `exigir=False`, para quien solo quiere saber si ya esta).
    """
    if id_nodo is None:
        return None
    id_nodo = int(id_nodo)
    if id_nodo >= 0:
        return id_nodo
    fila = conn.execute(
        f"SELECT id_central FROM {TABLA} WHERE node_id = ? AND tabla = ? AND id_nodo = ?",
        (node_id, validar_identificador(tabla), id_nodo),
    ).fetchone()
    if fila is not None:
        return int(fila[0])
    if not exigir:
        return None
    raise SinMapeo(
        f"el id provisorio {id_nodo} de {tabla} del nodo {node_id} no esta mapeado en el central: falta aplicar el "
        "alta que lo creo (quedo en revision manual, o todavia no subio)"
    )


def anotar_mapeo(conn, node_id: str, tabla: str, id_nodo: int, id_central: int) -> bool:
    """Anota `(node_id, tabla, id_nodo) -> id_central` en el central. Devuelve `True` si lo escribio, `False` si ya
    estaba igual (es idempotente: el outbox entrega al menos una vez). Si ya estaba **distinto**, `MapeoEnConflicto`.

    No commitea: va en la transaccion que aplica el alta, junto con la fila que crea."""
    if not es_provisorio(id_nodo):
        raise ValueError(f"un mapeo es de un id provisorio (negativo), no de {id_nodo!r}")
    tabla = validar_identificador(tabla)
    previo = traducir_id(conn, node_id, tabla, id_nodo, exigir=False)
    if previo is not None:
        if previo != int(id_central):
            raise MapeoEnConflicto(
                f"el nodo {node_id} ya tenia {tabla} {id_nodo} mapeado al {previo} del central y ahora llega "
                f"como {id_central}: el nodo reuso un id provisorio"
            )
        return False
    conn.execute(
        f"INSERT INTO {TABLA} (node_id, tabla, id_nodo, id_central, creado_at) VALUES (?, ?, ?, ?, ?)",
        (node_id, tabla, int(id_nodo), int(id_central), datetime.now(UTC).isoformat()),
    )
    return True


def init_mapeo_schema(conn) -> None:
    """Crea `libraedge_mapeo`. Idempotente. La llama `init_schema()`, asi que aparece en el central y en el nodo con el
    arranque de siempre, sin una migracion del producto."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLA} (
            node_id TEXT NOT NULL,
            tabla TEXT NOT NULL,
            id_nodo BIGINT NOT NULL,
            id_central BIGINT NOT NULL,
            creado_at TEXT NOT NULL,
            PRIMARY KEY (node_id, tabla, id_nodo)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_libraedge_mapeo_central ON {TABLA}(node_id, tabla, id_central)")
