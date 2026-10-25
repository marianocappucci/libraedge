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

## Desde v0.23: el id provisorio puede ser un texto (ADR-026)

libragenda (GestioLibra y MedLibra) tiene ids de **texto** (un uuid, o `p-042` en MedLibra). Su id provisorio es el
texto **`"-N"`** (`"-1"`, `"-2"`, ...): el mismo numero negativo de siempre, escrito. `es_provisorio`, `traducir_id`,
`anotar_mapeo` y `siguiente_id_provisorio(conn, texto=True)` lo entienden, y el id entero sigue **exactamente igual**.

Su mapeo vive en **otra tabla, `libraedge_mapeo_texto`**, y no en `libraedge_mapeo`. Es a proposito: `libraedge_mapeo`
tiene `BIGINT` en `id_nodo` e `id_central`, y los productos la leen con SQL propio (`id_nodo = :id` con un entero,
`id_central IN (...)`); pasarla a `TEXT` hace que PostgreSQL rechace esas consultas (`operator does not exist: text =
smallint`), y en LibraClub el `except Exception` de `_ids_equivalentes` se lo traga en silencio. Una tabla aparte no
cambia un solo byte de lo que ya existe y tampoco cambia la forma de las filas que el central manda a los nodos de
antes. Que mapeo se usa lo dice el **tipo del id que se pide**: un entero va a `libraedge_mapeo` y devuelve un entero;
un `"-N"` va a `libraedge_mapeo_texto` y devuelve un texto.
"""

import re
from datetime import UTC, datetime

from libraedge.db.changelog import validar_identificador

#: La tabla del mapeo. Va en el espejo del nodo con esta clave.
TABLA = "libraedge_mapeo"
CLAVE = ("node_id", "tabla", "id_nodo")
#: El mapeo de los ids provisorios **de texto** (`"-N"`): mismas columnas y misma clave, pero `id_nodo` e `id_central`
#: son `TEXT`. Va en el espejo del nodo con esta clave, igual que la otra (ver el docstring del modulo).
TABLA_TEXTO = "libraedge_mapeo_texto"
CLAVE_TEXTO = CLAVE

#: Un id provisorio de texto: `-` y un numero sin ceros a la izquierda (el que da `siguiente_id_provisorio`). Hasta 18
#: cifras, para que siempre quepa tambien como entero.
_PROVISORIO_DE_TEXTO = re.compile(r"-[1-9][0-9]{0,17}")

#: El nombre de la secuencia en `local_sequences`. Una sola por base: un id provisorio es unico en todas las tablas.
SECUENCIA_PROVISORIA = "id_provisorio"


class SinMapeo(ValueError):
    """Un id provisorio (negativo) del nodo que el central no conoce: su alta no se aplico.

    Es un `ValueError` a proposito: el receptor lo manda a revision manual y el nodo no traba la cola (ADR-020).
    Al resolver el alta, se reintenta esta operacion (en el orden en que se crearon)."""


class MapeoEnConflicto(ValueError):
    """El central ya tiene un mapeo distinto para ese `(nodo, tabla, id_nodo)`: el nodo reuso un id provisorio
    (una PC reemplazada que arranco la secuencia de cero con el mismo `node_id`). Revision manual."""


def es_provisorio_de_texto(id_) -> bool:
    """Si `id_` es un id provisorio **de texto**: `"-N"` (un `-` y un numero sin ceros a la izquierda)."""
    return isinstance(id_, str) and _PROVISORIO_DE_TEXTO.fullmatch(id_) is not None


def es_provisorio(id_) -> bool:
    """Si `id_` es un id provisorio de un nodo: un entero negativo, o el texto `"-N"` (v0.23, ADR-026)."""
    if isinstance(id_, str):
        return es_provisorio_de_texto(id_)
    return isinstance(id_, int) and not isinstance(id_, bool) and id_ < 0


def tabla_del_mapeo(id_nodo) -> str:
    """La tabla del mapeo de un id provisorio: `libraedge_mapeo` al entero, `libraedge_mapeo_texto` al `"-N"`."""
    return TABLA_TEXTO if es_provisorio_de_texto(id_nodo) else TABLA


def siguiente_id_provisorio(conn, *, texto: bool = False):
    """El proximo id provisorio del nodo, negativo y nunca repetido. **No commitea**: va en la transaccion del alta.

    Un entero (`-1`, `-2`, ...) o, con `texto=True`, el mismo numero como texto (`"-1"`, `"-2"`, ...) para una tabla de
    ids de texto. La secuencia es una sola por base: el numero no se repite entre las dos formas.

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
    numero = -(int(filas[0][0]) - 1)
    return str(numero) if texto else numero


def traducir_id(conn, node_id: str, tabla: str, id_nodo, *, exigir: bool = True):
    """El id del central para un id del nodo `node_id`, en `tabla`.

    - `None` -> `None`; un id **no negativo** ya es del central (el nodo uso una fila espejada): vuelve igual. Un texto
      que no es `"-N"` (un uuid, un `p-042`) tambien es del central y vuelve igual.
    - Un id provisorio se busca en el mapeo: un entero negativo en `libraedge_mapeo` (y devuelve un **entero**, como
      siempre) y un `"-N"` en `libraedge_mapeo_texto` (y devuelve un **texto**). Si no esta: `SinMapeo` (con
      `exigir=True`, el default; es lo que hace que una venta que lleva un cliente que no subio sea rechazada en vez de
      quedar a nombre de otro) o `None` (con `exigir=False`, para quien solo quiere saber si ya esta).
    - Un `"-N"` que no esta en el mapeo de texto se busca ademas en el de enteros (y ahi devuelve un entero): es lo que
      hacia v0.22, que convertia cualquier texto con `int()`. Una tabla es de enteros o de texto, nunca de las dos, asi
      que la segunda busqueda no puede encontrar la fila de otra tabla.
    """
    if id_nodo is None:
        return None
    if isinstance(id_nodo, str):
        if not es_provisorio_de_texto(id_nodo):
            return id_nodo
        fila = conn.execute(
            f"SELECT id_central FROM {TABLA_TEXTO} WHERE node_id = ? AND tabla = ? AND id_nodo = ?",
            (node_id, validar_identificador(tabla), id_nodo),
        ).fetchone()
        if fila is not None:
            return str(fila[0])
    elif int(id_nodo) >= 0:
        return int(id_nodo)
    id_nodo = int(id_nodo)
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


def anotar_mapeo(conn, node_id: str, tabla: str, id_nodo, id_central) -> bool:
    """Anota `(node_id, tabla, id_nodo) -> id_central` en el central. Devuelve `True` si lo escribio, `False` si ya
    estaba igual (es idempotente: el outbox entrega al menos una vez). Si ya estaba **distinto**, `MapeoEnConflicto`.

    Un `id_nodo` entero (`-3`) va a `libraedge_mapeo` con un `id_central` entero; un `"-3"` va a `libraedge_mapeo_texto`
    con un `id_central` de texto (el uuid o el `p-042` que el central le asigno).

    No commitea: va en la transaccion que aplica el alta, junto con la fila que crea."""
    if not es_provisorio(id_nodo):
        raise ValueError(f"un mapeo es de un id provisorio (negativo), no de {id_nodo!r}")
    tabla = validar_identificador(tabla)
    if es_provisorio_de_texto(id_nodo):
        return _anotar_de_texto(conn, node_id, tabla, id_nodo, id_central)
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


def _anotar_de_texto(conn, node_id: str, tabla: str, id_nodo: str, id_central) -> bool:
    if isinstance(id_central, bool) or not isinstance(id_central, str | int) or not str(id_central).strip():
        raise ValueError(f"el id del central de un mapeo de texto es un texto no vacio, no {id_central!r}")
    id_central = str(id_central)
    if es_provisorio(id_central):
        raise ValueError(f"el id del central no puede ser provisorio: {id_central!r}")
    fila = conn.execute(
        f"SELECT id_central FROM {TABLA_TEXTO} WHERE node_id = ? AND tabla = ? AND id_nodo = ?",
        (node_id, tabla, id_nodo),
    ).fetchone()
    if fila is not None:
        if str(fila[0]) != id_central:
            raise MapeoEnConflicto(
                f"el nodo {node_id} ya tenia {tabla} {id_nodo} mapeado al {fila[0]} del central y ahora llega "
                f"como {id_central}: el nodo reuso un id provisorio"
            )
        return False
    conn.execute(
        f"INSERT INTO {TABLA_TEXTO} (node_id, tabla, id_nodo, id_central, creado_at) VALUES (?, ?, ?, ?, ?)",
        (node_id, tabla, id_nodo, id_central, datetime.now(UTC).isoformat()),
    )
    return True


def init_mapeo_schema(conn) -> None:
    """Crea `libraedge_mapeo` y `libraedge_mapeo_texto`. Idempotente. La llama `init_schema()`, asi que aparecen en el
    central y en el nodo con el arranque de siempre, sin una migracion del producto.

    Desde v0.23 (ADR-026) la segunda es nueva y **no toca la primera**: una base de v0.22 sube con sus mapeos enteros
    tal cual, y volver a v0.22 deja una tabla de mas que nadie lee (`DROP TABLE libraedge_mapeo_texto` la saca)."""
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
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLA_TEXTO} (
            node_id TEXT NOT NULL,
            tabla TEXT NOT NULL,
            id_nodo TEXT NOT NULL,
            id_central TEXT NOT NULL,
            creado_at TEXT NOT NULL,
            PRIMARY KEY (node_id, tabla, id_nodo)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_libraedge_mapeo_texto_central ON {TABLA_TEXTO}(node_id, tabla, id_central)")
