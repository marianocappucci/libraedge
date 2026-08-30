"""El nodo y el outbox, contra los DOS motores.

Hasta la Fase 1 del nodo espejo (2026-08-29) esta suite corria **solo contra
SQLite**, porque LibraEdge era la "excepcion permanente" de la familia: se
asumia que el nodo offline no podia llevar un PostgreSQL al lado. Esa decision
se dio vuelta -- el nodo corre el producto entero con PostgreSQL embebido, asi
que el outbox vive en la misma base que la venta, dentro de su misma
transaccion. Ver `wiki/analyses/nodo-libraedge-espejo-local.md` del wiki.

Una suite verde sobre SQLite no dice nada sobre el motor real: no chequea FKs
con el pragma apagado, tipa dinamicamente, y **no aborta la transaccion cuando
una sentencia falla**, que es justo la diferencia que rompe un `except
IntegrityError` que despues sigue usando la misma conexion.

SQLite igual se conserva como parametro, no por nostalgia: `libracommerce` lo
usa asi hoy (`tests/test_offline_sync.py` arma el repo sobre `:memory:`) y
romperselo seria romper al unico consumidor que hay.

La conexion a PostgreSQL es la de **LibraCore**, no una de psycopg cruda, y eso
es a proposito: es exactamente la que le pasa el producto al repositorio en el
nodo real. Su `_postgres.py` traduce los `?` a `%s`, sabe de `executescript`, y
convierte las excepciones de psycopg a las de `sqlite3` -- que es lo que hace
que este paquete no necesite una capa dual propia.
"""

import os
import sqlite3

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema


def url_postgres() -> str:
    """La URL de PostgreSQL, o saltea el test fuera de CI.

    **En CI no se saltea.** Si la variable falta ahi, es que el servicio no se
    levanto, y dejar la suite en verde seria peor que no tenerla: diria "el
    outbox anda en PostgreSQL" sin haberlo tocado. Mismo criterio que usan
    LibraCommerce y el resto de la familia.
    """
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "LIBRACORE_POSTGRES_URL no está definida en CI — los tests contra "
            "PostgreSQL no se saltean acá"
        )
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request):
    """Una conexión con el schema de LibraEdge creado, contra cada motor.

    El id del test dice cuál corrió (`[sqlite]` / `[postgres]`), así que un rojo
    nombra el backend sin tener que abrir nada.
    """
    if request.param == "sqlite":
        conexion = sqlite3.connect(":memory:", check_same_thread=False)
        init_schema(conexion)
        yield conexion
        conexion.close()
        return

    from libracore.db import core

    url = url_postgres()
    core.configure(url)
    conexion = core.get_connection()
    try:
        # Cada test arranca con la base vacía. El outbox afirma sobre conteos
        # de pendientes, así que una fila de un test anterior los falsearía.
        conexion.execute("DROP SCHEMA public CASCADE")
        conexion.execute("CREATE SCHEMA public")
        conexion.commit()
        init_schema(conexion)
        conexion.commit()
        yield conexion
    finally:
        conexion.close()
        core._db_path = None
        core._database_url = None


@pytest.fixture
def repo(conn) -> NodeRepository:
    return NodeRepository(conn)


@pytest.fixture
def motor(request) -> str:
    """El nombre del motor del test actual (`sqlite` o `postgres`).

    Para los pocos casos que tienen que preguntar por el backend — listar las
    tablas del catálogo, por ejemplo, que no se hace igual en los dos.
    """
    return request.node.callspec.params["conn"]
