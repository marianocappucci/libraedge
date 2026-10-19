"""La version del esquema de la base: que el nodo y el central hablen de lo mismo.

El nodo corre **el producto entero** con las mismas migraciones que el central
(ADR-003). Si el central migra y el nodo todavia no, el espejo trae filas con
una columna que la tabla del nodo no tiene y ``MirrorApplier`` revienta con un
error de SQL que no dice nada util; y lo que sube puede venir con una forma que
el central ya no espera.

## Como se declara (por producto, de forma generica)

La version es un **texto opaco** que se compara por igualdad. El producto la
declara pasandole al router del central ``version_esquema=``: un texto fijo o un
invocable ``(conexion) -> str``. Lo normal es :func:`version_de_alembic`, que
lee **todas** las tablas ``alembic_version*`` de la base --la familia tiene
varias cadenas por base: la del motor, la de libraauth y la del producto-- y no
necesita ninguna configuracion: el nodo y el central corren las mismas
migraciones, asi que si estan al dia dan el mismo texto.

El nodo manda la suya en la cabecera :data:`CABECERA`. Si el central declara
version y la del nodo no coincide (o no viene), las dos rutas contestan
**409** con la que espera. Del lado del nodo eso se lee como
:class:`EsquemaIncompatible`: la subida lo trata como un reintento --las
operaciones esperan en el outbox, no se pierden-- y el estado del nodo lo
muestra con el texto del central.

Un central que no declara version no controla nada: es el comportamiento de
siempre, para no romper a quien no lo adopto.
"""

import json
import sqlite3
from collections.abc import Callable

#: La cabecera con la que el nodo dice en que esquema esta.
CABECERA = "X-LibraEdge-Esquema"

VersionEsquema = str | Callable[[object], str | None] | None


class EsquemaIncompatible(RuntimeError):
    """El central contesto 409: el esquema del nodo no es el que espera."""

    def __init__(self, esperada: str | None, recibida: str | None, mensaje: str | None = None):
        self.esperada = esperada
        self.recibida = recibida
        super().__init__(mensaje or mensaje_de_incompatibilidad(esperada, recibida))


def mensaje_de_incompatibilidad(esperada: str | None, recibida: str | None) -> str:
    if recibida is None:
        return (f"el nodo no declara la version de su esquema y el central espera "
                f"{esperada!r}: actualizar el nodo")
    return (f"el esquema del nodo ({recibida!r}) no es el del central ({esperada!r}): "
            f"actualizar el nodo (o el central) antes de sincronizar")


def _tablas_de_version(conn) -> list[str]:
    if isinstance(conn, sqlite3.Connection):
        filas = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    else:
        filas = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()"
        ).fetchall()
    # `LIKE 'alembic_version%'` tomaria el `_` como comodin: se filtra aca.
    return sorted(fila[0] for fila in filas if str(fila[0]).startswith("alembic_version"))


def version_de_alembic(conn) -> str | None:
    """``"alembic_version=0027;alembic_version_libraauth=0006;..."``, o ``None``.

    Todas las cadenas de Alembic de la base, ordenadas, cada una con sus heads.
    ``None`` si la base no tiene ninguna: un nodo o un central sin Alembic no
    tienen version que comparar.
    """
    partes = []
    for tabla in _tablas_de_version(conn):
        heads = sorted(
            str(fila[0]) for fila in conn.execute(f"SELECT version_num FROM {tabla}").fetchall()
        )
        partes.append(f"{tabla}={','.join(heads)}")
    return ";".join(partes) or None


def resolver(version: VersionEsquema, conn) -> str | None:
    """El texto de la version: el fijo, o el que devuelve el invocable."""
    if version is None or isinstance(version, str):
        return version
    return version(conn)


def detalle_409(esperada: str | None, recibida: str | None) -> dict:
    """El cuerpo del 409. ``mensaje`` es para mostrarlo tal cual en el nodo."""
    return {
        "error": "esquema_incompatible",
        "esperada": esperada,
        "recibida": recibida,
        "mensaje": mensaje_de_incompatibilidad(esperada, recibida),
    }


def cabeceras_del_nodo(node_secret: str, version_esquema: str | None) -> dict[str, str]:
    """La autenticacion del nodo y, si la conoce, la version de su esquema."""
    cabeceras = {"Authorization": f"Bearer {node_secret}"}
    if version_esquema is not None:
        cabeceras[CABECERA] = version_esquema
    return cabeceras


def relanzar_si_es_de_esquema(exc) -> None:
    """Si ``exc`` es el 409 de esquema, levanta :class:`EsquemaIncompatible`."""
    incompatible = desde_error_http(exc)
    if incompatible is not None:
        raise incompatible from exc


def desde_error_http(exc) -> EsquemaIncompatible | None:
    """Un ``HTTPError`` 409 de ``urllib`` como :class:`EsquemaIncompatible`.

    Devuelve ``None`` si no es un 409 de esquema: ese error se deja pasar tal cual.
    """
    if getattr(exc, "code", None) != 409:
        return None
    try:
        cuerpo = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return None
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if not isinstance(detalle, dict) or detalle.get("error") != "esquema_incompatible":
        return None
    return EsquemaIncompatible(detalle.get("esperada"), detalle.get("recibida"),
                               detalle.get("mensaje"))
