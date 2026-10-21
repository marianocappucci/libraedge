"""La guarda del espejo (v0.17, ADR-019): en el nodo, solo el espejo escribe las tablas que vienen del central.

El trigger es PL/pgSQL y el nodo real corre PostgreSQL: los tests de la guarda se saltean explicito en `[sqlite]` (que
tiene su propio test: es un no-op y lo dice). Cada test arranca con la base vacia (fixture `conn`).
"""

import sqlite3

import pytest

from libraedge.db.guarda import (
    MENSAJE,
    desinstalar_guarda_de_espejo,
    instalar_guarda_de_espejo,
    marcar_aplicando,
    tablas_con_guarda,
)
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import ReferenceChange, ReferenceOperation
from libraedge.sync.pull import MirrorApplier, PullWorker


@pytest.fixture
def nodo(conn, motor):
    """Un nodo con dos tablas espejadas: `productos` (que se va a guardar) y `clientes` (que no)."""
    conn.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, precio NUMERIC)")
    conn.execute("CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
    conn.commit()
    return conn


@pytest.fixture
def solo_postgres(motor):
    if motor == "sqlite":
        pytest.skip("la guarda es un trigger PL/pgSQL y el nodo real corre PostgreSQL; en SQLite es un no-op")


def cambio(cursor=1, tabla="productos", operacion="upsert", payload=None):
    payload = payload if payload is not None else {"id": 1, "nombre": "yerba", "precio": "100"}
    return ReferenceChange(cursor=cursor, table_name=tabla, row_id=str(payload["id"]),
                           operation=ReferenceOperation(operacion), payload=payload)


def _filas(conn, sql):
    return [tuple(fila) for fila in conn.execute(sql).fetchall()]


def _total(conn, tabla):
    return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def _bloqueada(conn, sql, params=()):
    """Corre una escritura y devuelve el error con que la guarda la frena (y deja la conexion sana)."""
    with pytest.raises(sqlite3.DatabaseError) as exc:
        conn.execute(sql, params)
    conn.rollback()
    assert MENSAJE in str(exc.value)
    return exc.value


# ── lo que la guarda frena ──────────────────────────────────────────────────

def test_una_escritura_comun_falla_con_un_error_claro(nodo, solo_postgres):
    nodo.execute("INSERT INTO productos (id, nombre) VALUES (1, 'previo')")   # antes de la guarda
    nodo.commit()
    assert instalar_guarda_de_espejo(nodo, ["productos"]) == ("productos",)
    nodo.commit()

    error = _bloqueada(nodo, "INSERT INTO productos (id, nombre) VALUES (2, 'yerba')")
    assert "productos" in str(error) and "INSERT" in str(error)
    _bloqueada(nodo, "UPDATE productos SET nombre = 'otro' WHERE id = 1")
    _bloqueada(nodo, "DELETE FROM productos WHERE id = 1")
    assert _filas(nodo, "SELECT nombre FROM productos") == [("previo",)], "ninguna escritura entro"


def test_el_error_no_es_un_integrity_error_que_un_producto_se_trague(nodo, solo_postgres):
    """Un `except IntegrityError` del producto (por un duplicado, por ejemplo) no tiene que tragarse la guarda."""
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()
    with pytest.raises(sqlite3.DatabaseError) as exc:
        nodo.execute("INSERT INTO productos (id, nombre) VALUES (1, 'x')")
    nodo.rollback()
    assert not isinstance(exc.value, sqlite3.IntegrityError)
    assert exc.value.__cause__.sqlstate == "LE001", "el SQLSTATE propio, para quien quiera distinguirlo"


# ── lo que el espejo puede hacer ────────────────────────────────────────────

def test_el_espejo_puede_escribir_aunque_haya_guarda(nodo, solo_postgres):
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()
    aplicador = MirrorApplier(nodo, {"productos": "id"})

    aplicador.aplicar(cambio(payload={"id": 1, "nombre": "yerba", "precio": "100"}))
    nodo.commit()
    aplicador.aplicar(cambio(cursor=2, payload={"id": 1, "nombre": "yerba", "precio": "120"}))
    nodo.commit()
    assert _filas(nodo, "SELECT precio FROM productos") == [(120,)]
    aplicador.aplicar(cambio(cursor=3, operacion="delete", payload={"id": 1, "nombre": "yerba"}))
    nodo.commit()
    assert _total(nodo, "productos") == 0


def test_la_guarda_se_vuelve_a_cerrar_despues_del_commit_del_espejo(nodo, solo_postgres):
    """`SET LOCAL` muere con el commit: el producto no hereda la puerta abierta del espejo."""
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()
    MirrorApplier(nodo, {"productos": "id"}).aplicar(cambio())
    nodo.commit()

    _bloqueada(nodo, "INSERT INTO productos (id, nombre) VALUES (2, 'colado')")
    assert _total(nodo, "productos") == 1


def test_el_ciclo_de_bajada_completo_pasa_por_la_guarda(nodo, solo_postgres):
    """`PullWorker` commitea despues de cada cambio: la variable se tiene que fijar en CADA aplicar."""
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()

    class Central:
        def pull(self, cursor, limit):
            return tuple(cambio(cursor=n, payload={"id": n, "nombre": f"p{n}"}) for n in (1, 2, 3))

    repo = NodeRepository(nodo)
    repo.asegurar_identidad_local("node-1")
    aplicados = PullWorker(repo, Central(), MirrorApplier(nodo, {"productos": "id"})).run_once("node-1")

    assert aplicados == 3 and _total(nodo, "productos") == 3
    assert repo.get_server_cursor("node-1") == 3


def test_una_migracion_de_datos_abre_la_guarda_a_mano(nodo, solo_postgres):
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()
    marcar_aplicando(nodo)
    nodo.execute("INSERT INTO productos (id, nombre) VALUES (1, 'migrado')")
    nodo.commit()
    assert _total(nodo, "productos") == 1
    _bloqueada(nodo, "INSERT INTO productos (id, nombre) VALUES (2, 'despues')")


# ── opt-in por tabla ────────────────────────────────────────────────────────

def test_una_tabla_fuera_de_la_lista_no_se_toca(nodo, solo_postgres):
    """Es opt-in: las altas de clientes en el nodo no estan resueltas y la guarda no puede romper el POS."""
    assert instalar_guarda_de_espejo(nodo, ["productos"]) == ("productos",)
    nodo.commit()

    nodo.execute("INSERT INTO clientes (id, nombre) VALUES (1, 'Ana')")
    nodo.execute("UPDATE clientes SET nombre = 'Ana M' WHERE id = 1")
    nodo.commit()
    assert _filas(nodo, "SELECT nombre FROM clientes") == [("Ana M",)]
    nodo.execute("DELETE FROM clientes WHERE id = 1")
    nodo.commit()
    assert tablas_con_guarda(nodo) == ("productos",)


def test_sin_lista_no_se_guarda_nada(nodo, solo_postgres):
    assert instalar_guarda_de_espejo(nodo, []) == ()
    nodo.commit()
    nodo.execute("INSERT INTO productos (id, nombre) VALUES (1, 'libre')")
    nodo.commit()
    assert tablas_con_guarda(nodo) == ()


def test_instalar_es_idempotente(nodo, solo_postgres):
    for _ in range(3):
        assert instalar_guarda_de_espejo(nodo, ["productos", "clientes"]) == ("clientes", "productos")
        nodo.commit()
    assert tablas_con_guarda(nodo) == ("clientes", "productos")
    _bloqueada(nodo, "INSERT INTO clientes (id, nombre) VALUES (1, 'x')")


def test_la_guarda_converge_con_la_lista(nodo, solo_postgres):
    """Una tabla que el producto resuelve y saca de la lista deja de estar bloqueada."""
    instalar_guarda_de_espejo(nodo, ["productos", "clientes"])
    nodo.commit()
    assert instalar_guarda_de_espejo(nodo, ["productos"]) == ("productos",)
    nodo.commit()
    assert tablas_con_guarda(nodo) == ("productos",)
    nodo.execute("INSERT INTO clientes (id, nombre) VALUES (1, 'libre otra vez')")
    nodo.commit()
    instalar_guarda_de_espejo(nodo, [])
    nodo.commit()
    assert tablas_con_guarda(nodo) == ()


def test_desinstalar_una_sola(nodo, solo_postgres):
    instalar_guarda_de_espejo(nodo, ["productos", "clientes"])
    desinstalar_guarda_de_espejo(nodo, "productos")
    desinstalar_guarda_de_espejo(nodo, "productos")   # idempotente
    nodo.commit()
    assert tablas_con_guarda(nodo) == ("clientes",)


def test_una_tabla_que_no_esta_en_la_base_se_saltea(nodo, solo_postgres):
    """El producto declara una tabla que una migracion todavia no creo: no frena el arranque, y no figura guardada."""
    assert instalar_guarda_de_espejo(nodo, ["productos", "todavia_no_existe"]) == ("productos",)
    nodo.commit()
    assert tablas_con_guarda(nodo) == ("productos",)


def test_la_guarda_no_toca_el_outbox_ni_el_changelog(nodo, solo_postgres):
    """Las tablas propias de LibraEdge no se guardan: el producto encola en `sync_outbox` en cada venta."""
    instalar_guarda_de_espejo(nodo, ["productos"])
    nodo.commit()
    NodeRepository(nodo).next_sequence()
    assert nodo.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("nombre", ["productos; DROP TABLE x", "1tabla", "a-b", ""])
def test_un_nombre_de_tabla_invalido_se_rechaza(nodo, nombre):
    with pytest.raises(ValueError):
        instalar_guarda_de_espejo(nodo, [nombre])


# ── SQLite ──────────────────────────────────────────────────────────────────

def test_sobre_sqlite_es_un_no_op_que_lo_dice(conn, motor):
    if motor != "sqlite":
        pytest.skip("este es el test de SQLite")
    conexion = conn
    conexion.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT)")

    assert instalar_guarda_de_espejo(conexion, ["productos"]) == (), "no devuelve tablas guardadas: no guarda ninguna"
    marcar_aplicando(conexion)
    desinstalar_guarda_de_espejo(conexion, "productos")
    assert tablas_con_guarda(conexion) == ()
    conexion.execute("INSERT INTO productos (id, nombre) VALUES (1, 'libre')")
    assert _total(conexion, "productos") == 1
