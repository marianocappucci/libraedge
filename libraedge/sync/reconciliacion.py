"""El reapuntado: el nodo cambia el id provisorio de una fila por el que le asigno el central (ADR-021).

El nodo dio de alta una fila con un id negativo (`libraedge.mapeo`); el central la creo con su id y anoto el mapeo
`(nodo, tabla, id_nodo) -> id_central`, que baja al nodo por el espejo. Cuando el nodo ya tiene **las dos filas** (la
provisoria y la definitiva, que bajo por el espejo con su contenido), `reapuntar` hace, en la transaccion de quien
llama:

1. **Reapunta cada referencia** del id provisorio al definitivo:
   - las **claves foraneas** hacia la tabla, que se leen del catalogo de la base (`claves_foraneas_hacia`) y no de una
     lista que alguien tenga que mantener: una tabla nueva con FK al cliente se reapunta sola;
   - las **referencias sin FK** que el producto declaro (`libraedge.altas.Referencia`).
   Lo que apunta a la provisoria desde una tabla **que el nodo espeja** (una lista de precios del cliente, por
   ejemplo) no se reapunta: la provisoria no la creo el central, que es la autoridad, asi que se borra y vale la
   del central.
2. **Borra la fila provisoria.** Ya nada la referencia.

## Idempotente y sin atajos

Si la provisoria ya no esta, no hace nada (`YA_ESTABA`): es lo que pasa al reaplicar el mapeo despues de un corte. Si
la definitiva todavia no bajo, tampoco toca nada (`PENDIENTE`): el barrido del final de cada ciclo (`MirrorApplier.
reconciliar_pendientes`) lo reintenta. Lo que no es una FK de una sola columna hacia la clave de la tabla no se mueve:
no hay en la familia, y se prefiere no inventar una regla para un caso que no existe.

Es la unica operacion del espejo que **borra** una fila del nodo que el central no borro, y por eso es tan
estrecha: solo sobre un id negativo, solo cuando el central ya tiene la definitiva.

## Ids enteros y ids de texto (v0.23, ADR-026)

`provisorio` y `definitivo` son del tipo de la clave de la tabla: enteros (`-1` -> `57`) o textos (`"-1"` ->
`"p-042"`). Se pasan tal cual a la base: PostgreSQL no compara un entero con una columna de texto, asi que convertir en
el medio (el `int()` de antes) rompia las tablas de ids de texto. Quien llama los trae del mapeo que corresponde al tipo
de la tabla (`clave_es_entera` dice cual es).
"""

import sqlite3
from contextlib import contextmanager

from libraedge.altas import Referencia
from libraedge.db.changelog import validar_identificador
from libraedge.mapeo import es_provisorio

HECHO = "hecho"
YA_ESTABA = "ya_estaba"
PENDIENTE = "pendiente"


def _es_sqlite(conn) -> bool:
    return isinstance(conn, sqlite3.Connection)


def claves_foraneas_hacia(conn, tabla: str, clave: str = "id") -> list[tuple[str, str]]:
    """`[(tabla_hija, columna_hija)]`: las claves foraneas **de una columna** que apuntan a `tabla.clave`, leidas del
    catalogo de la base (PostgreSQL) o de `PRAGMA foreign_key_list` (SQLite). Incluye a la propia `tabla` si se
    referencia a si misma."""
    tabla, clave = validar_identificador(tabla), validar_identificador(clave)
    if _es_sqlite(conn):
        hijas = []
        nombres = [f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
        for hija in nombres:
            filas = conn.execute(f"PRAGMA foreign_key_list({validar_identificador(hija)})").fetchall()
            por_fk: dict[int, list] = {}
            for fila in filas:
                por_fk.setdefault(fila[0], []).append(fila)
            for partes in por_fk.values():
                if len(partes) != 1:
                    continue
                _id, _seq, padre, desde, hasta = partes[0][:5]
                if padre.lower() == tabla.lower() and (hasta or clave) == clave:
                    hijas.append((hija, desde))
        return sorted(hijas)
    filas = conn.execute(
        """SELECT cl.relname, att.attname
             FROM pg_constraint c
             JOIN pg_class cl ON cl.oid = c.conrelid
             JOIN pg_namespace n ON n.oid = cl.relnamespace
             JOIN pg_attribute att ON att.attrelid = c.conrelid AND att.attnum = c.conkey[1]
             JOIN pg_attribute fatt ON fatt.attrelid = c.confrelid AND fatt.attnum = c.confkey[1]
            WHERE c.contype = 'f' AND c.confrelid = ?::regclass
              AND array_length(c.conkey, 1) = 1 AND fatt.attname = ? AND n.nspname = current_schema()
            ORDER BY cl.relname, att.attname""",
        (tabla, clave),
    ).fetchall()
    return [(f[0], f[1]) for f in filas]


def clave_es_entera(conn, tabla: str, clave: str = "id") -> bool:
    """Si la columna `tabla.clave` es de enteros (y su mapeo es `libraedge_mapeo`) o de otra cosa (texto, uuid: es
    `libraedge_mapeo_texto`). Lee el catalogo de la base. Una tabla o columna que no esta devuelve `True`: es el
    comportamiento de antes de v0.23, y la consulta que sigue falla con su error de siempre."""
    tabla, clave = validar_identificador(tabla), validar_identificador(clave)
    if _es_sqlite(conn):
        for fila in conn.execute(f"PRAGMA table_info({tabla})").fetchall():
            if fila[1].lower() == clave.lower():
                # Las reglas de afinidad de SQLite: «INT» en el tipo declarado es afinidad entera.
                return "INT" in (fila[2] or "").upper()
        return True
    fila = conn.execute(
        "SELECT data_type FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
        (tabla, clave),
    ).fetchone()
    return fila is None or fila[0] in ("smallint", "integer", "bigint", "numeric")


def _existe(conn, tabla: str, clave: str, valor) -> bool:
    return conn.execute(f"SELECT 1 FROM {tabla} WHERE {clave} = ?", (valor,)).fetchone() is not None


def reapuntar(conn, tabla: str, provisorio: int | str, definitivo: int | str, *, clave: str = "id",
              referencias: tuple[Referencia, ...] = (), espejadas=frozenset()) -> str:
    """Reapunta todo lo que referencia a `tabla.clave = provisorio` hacia `definitivo` y borra la fila provisoria.

    Devuelve `HECHO`, `YA_ESTABA` (la provisoria ya no existe) o `PENDIENTE` (la definitiva todavia no bajo). No
    commitea ni abre savepoint: es de quien llama (`MirrorApplier` lo hace). `espejadas` son las tablas que el nodo
    espeja (ver el docstring del modulo)."""
    tabla, clave = validar_identificador(tabla), validar_identificador(clave)
    if not es_provisorio(provisorio):
        raise ValueError(f"{provisorio!r} no es un id provisorio (negativo)")
    if definitivo is None or es_provisorio(definitivo) or (isinstance(definitivo, str) and not definitivo.strip()):
        raise ValueError(f"{definitivo!r} no es un id del central (no negativo)")
    if not _existe(conn, tabla, clave, provisorio):
        return YA_ESTABA
    if not _existe(conn, tabla, clave, definitivo):
        return PENDIENTE
    for hija, columna in claves_foraneas_hacia(conn, tabla, clave):
        if hija in espejadas:
            conn.execute(f"DELETE FROM {hija} WHERE {columna} = ?", (provisorio,))
        else:
            conn.execute(f"UPDATE {hija} SET {columna} = ? WHERE {columna} = ?", (definitivo, provisorio))
    for referencia in referencias:
        condicion = "".join(f" AND {columna} = ?" for columna, _ in referencia.donde)
        conn.execute(
            f"UPDATE {referencia.tabla} SET {referencia.columna} = ? WHERE {referencia.columna} = ?{condicion}",
            (definitivo, provisorio, *(valor for _, valor in referencia.donde)),
        )
    conn.execute(f"DELETE FROM {tabla} WHERE {clave} = ?", (provisorio,))
    return HECHO


@contextmanager
def savepoint(conn, nombre: str = "libraedge_reconciliar"):
    """Un `SAVEPOINT` que se deshace si el bloque falla: un reapuntado a medias no puede quedar escrito, y en
    PostgreSQL un error aborta la transaccion entera (que es la del cursor del espejo)."""
    if getattr(conn, "in_transaction", True) is False:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {nombre}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {nombre}")
        conn.execute(f"RELEASE SAVEPOINT {nombre}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {nombre}")
