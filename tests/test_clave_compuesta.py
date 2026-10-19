"""El espejo con clave primaria compuesta (v0.7), y el CLI del nodo que lo configura.

VentaLibra tiene `party_roles (party_id, role)`: una tabla de relacion sin `id`.
Hasta v0.6 `MirrorApplier` armaba el `ON CONFLICT` y el `DELETE` con una sola
columna y esa tabla no se podia espejar.
"""

import json

import pytest

from libraedge.db.changelog import (
    columnas_de_clave,
    init_changelog_schema,
    instalar_trigger,
    listar_cambios,
    row_id_de_clave,
    sembrar,
)
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import OutboxOperation, ReferenceChange, ReferenceOperation
from libraedge.sync.pull import MirrorApplier

CLAVE = ("party_id", "role")


@pytest.fixture
def base(conn):
    conn.execute(
        "CREATE TABLE party_roles (party_id INTEGER NOT NULL, role TEXT NOT NULL,"
        " desde TEXT, PRIMARY KEY (party_id, role))"
    )
    conn.commit()
    return conn


def _cambio(cursor, operacion, payload, row_id=None):
    return ReferenceChange(
        cursor=cursor, table_name="party_roles",
        row_id=row_id or row_id_de_clave((payload["party_id"], payload["role"])),
        operation=ReferenceOperation(operacion), payload=payload,
    )


def _filas(conn):
    return sorted(
        tuple(fila) for fila in
        conn.execute("SELECT party_id, role, desde FROM party_roles").fetchall()
    )


def test_columnas_de_clave():
    assert columnas_de_clave("id") == ("id",)
    assert columnas_de_clave("party_id+role") == CLAVE
    assert columnas_de_clave(["party_id", "role"]) == CLAVE
    for mala in ("", "a+", ("a", "a"), "a;b"):
        with pytest.raises(ValueError):
            columnas_de_clave(mala)


def test_row_id_de_clave():
    assert row_id_de_clave([7]) == "7"
    assert row_id_de_clave([7, "cliente"]) == '["7", "cliente"]'


def test_el_espejo_inserta_actualiza_y_es_idempotente_con_clave_compuesta(base):
    aplicador = MirrorApplier(base, {"party_roles": CLAVE})
    aplicador.aplicar(_cambio(1, "upsert", {"party_id": 7, "role": "cliente", "desde": "2026-01"}))
    aplicador.aplicar(_cambio(2, "upsert", {"party_id": 7, "role": "proveedor", "desde": "2026-02"}))
    aplicador.aplicar(_cambio(3, "upsert", {"party_id": 7, "role": "cliente", "desde": "2026-03"}))
    aplicador.aplicar(_cambio(3, "upsert", {"party_id": 7, "role": "cliente", "desde": "2026-03"}))
    base.commit()
    assert _filas(base) == [(7, "cliente", "2026-03"), (7, "proveedor", "2026-02")]


def test_el_espejo_borra_solo_la_fila_de_esa_clave(base):
    aplicador = MirrorApplier(base, {"party_roles": CLAVE})
    aplicador.aplicar(_cambio(1, "upsert", {"party_id": 7, "role": "cliente", "desde": None}))
    aplicador.aplicar(_cambio(2, "upsert", {"party_id": 7, "role": "proveedor", "desde": None}))
    aplicador.aplicar(_cambio(3, "delete", {"party_id": 7, "role": "cliente", "desde": None}))
    base.commit()
    assert _filas(base) == [(7, "proveedor", None)]


def test_un_delete_sin_payload_usa_el_row_id(base):
    aplicador = MirrorApplier(base, {"party_roles": CLAVE})
    aplicador.aplicar(_cambio(1, "upsert", {"party_id": 7, "role": "cliente", "desde": None}))
    aplicador.aplicar(ReferenceChange(
        cursor=2, table_name="party_roles", row_id='["7", "cliente"]',
        operation=ReferenceOperation.DELETE, payload=None))
    base.commit()
    assert _filas(base) == []

    with pytest.raises(ValueError, match="sin los valores de su clave"):
        aplicador.aplicar(ReferenceChange(
            cursor=3, table_name="party_roles", row_id="7",
            operation=ReferenceOperation.DELETE, payload=None))


def test_un_upsert_sin_las_columnas_de_la_clave_se_rechaza(base):
    aplicador = MirrorApplier(base, {"party_roles": CLAVE})
    with pytest.raises(ValueError, match="sin las columnas de su clave"):
        aplicador.aplicar(_cambio(1, "upsert", {"party_id": 7, "desde": None}, row_id="x"))


def test_una_tabla_solo_de_clave_no_actualiza_nada(base):
    """Sin columnas fuera de la clave, el upsert es `DO NOTHING`."""
    base.execute("CREATE TABLE etiquetas (a INTEGER NOT NULL, b TEXT NOT NULL, PRIMARY KEY (a, b))")
    aplicador = MirrorApplier(base, {"etiquetas": "a+b"})
    payload = {"a": 1, "b": "x"}
    cambio = ReferenceChange(cursor=1, table_name="etiquetas", row_id='["1", "x"]',
                             operation=ReferenceOperation.UPSERT, payload=payload)
    aplicador.aplicar(cambio)
    aplicador.aplicar(cambio)
    base.commit()
    assert base.execute("SELECT COUNT(*) FROM etiquetas").fetchone()[0] == 1


def test_sembrar_con_clave_compuesta_arma_el_row_id_igual_que_el_trigger(base, motor):
    init_changelog_schema(base)
    base.execute("INSERT INTO party_roles (party_id, role) VALUES (?, ?)", (7, "cliente"))
    base.commit()
    assert sembrar(base, "party_roles", CLAVE) == 1
    base.commit()
    (cambio,) = listar_cambios(base)
    assert cambio.row_id == '["7", "cliente"]'
    assert cambio.payload["role"] == "cliente"

    if motor == "sqlite":
        return
    # El trigger (PL/pgSQL, solo en el central) arma el mismo texto.
    instalar_trigger(base, "party_roles", CLAVE)
    base.commit()
    base.execute("INSERT INTO party_roles (party_id, role) VALUES (?, ?)", (8, "proveedor"))
    base.execute("DELETE FROM party_roles WHERE party_id = ? AND role = ?", (7, "cliente"))
    base.commit()
    _, alta, baja = listar_cambios(base)
    assert alta.row_id == '["8", "proveedor"]'
    assert str(baja.operation) == "delete"
    assert baja.row_id == '["7", "cliente"]'
    assert baja.payload == {"party_id": 7, "role": "cliente", "desde": None}


def test_el_trigger_de_clave_simple_sigue_igual(base, motor):
    """La funcion del trigger cambio (acepta varias columnas): una tabla de `id`
    sigue con el `row_id` de siempre."""
    if motor == "sqlite":
        pytest.skip("el trigger es PL/pgSQL; el central siempre corre PostgreSQL")
    init_changelog_schema(base)
    base.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT)")
    instalar_trigger(base, "productos")
    base.commit()
    base.execute("INSERT INTO productos (id, nombre) VALUES (?, ?)", (5, "yerba"))
    base.commit()
    assert [c.row_id for c in listar_cambios(base)] == ["5"]


# ── El CLI del nodo ─────────────────────────────────────────────────────────

def test_tablas_espejo_con_clave_compuesta():
    from libraedge.cli import tablas_espejo

    assert tablas_espejo("productos, party_roles:party_id+role ,clientes:codigo") == {
        "productos": "id", "party_roles": ("party_id", "role"), "clientes": "codigo"}


def version_fija(conexion):
    """La funcion que el test carga por `modulo:funcion`."""
    return "fija-1"


def test_version_esquema_del_nodo(conn):
    from libraedge.cli import version_esquema_del_nodo

    assert version_esquema_del_nodo(conn, None) is None, "sin Alembic no hay version"
    conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
    conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", ("0027",))
    conn.commit()
    assert version_esquema_del_nodo(conn, "alembic") == "alembic_version=0027"
    assert version_esquema_del_nodo(conn, "") == "alembic_version=0027"
    assert version_esquema_del_nodo(conn, "ninguna") is None
    assert version_esquema_del_nodo(conn, "tests.test_clave_compuesta:version_fija") == "fija-1"
    with pytest.raises(SystemExit, match="no se entiende"):
        version_esquema_del_nodo(conn, "rara")


def test_el_cli_del_nodo_lista_y_reenvia_lo_que_esta_en_revision(monkeypatch, capsys):
    """`libraedge-nodo revision`, por el camino real del proceso (PostgreSQL)."""
    from libracore.db import core

    from libraedge.cli import main
    from libraedge.db.schema import init_schema
    from tests.conftest import url_postgres

    url = url_postgres()
    core.configure(url)
    conexion = core.get_connection()
    try:
        conexion.execute("DROP SCHEMA public CASCADE")
        conexion.execute("CREATE SCHEMA public")
        conexion.commit()
        init_schema(conexion)
        repo = NodeRepository(conexion)
        repo.enqueue_operation(OutboxOperation(
            operation_id="node-1:1", node_id="node-1", sequence=1,
            operation_type="venta.anulada", aggregate_type="venta",
            aggregate_id="node-1:venta:1", occurred_at="2026-10-19T12:00:00Z",
            schema_version=1, payload={}))
        repo.mark_operation_manual_review("node-1:1", "sin manejador para venta.anulada v1")
    finally:
        conexion.close()
        core._db_path = None
        core._database_url = None

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", url)
    try:
        assert main(["revision", "listar"]) == 0
        (linea,) = capsys.readouterr().out.splitlines()
        assert json.loads(linea)["motivo"] == "sin manejador para venta.anulada v1"

        assert main(["revision", "reenviar", "node-1:1"]) == 0
        assert "en cola otra vez" in capsys.readouterr().out
        assert main(["revision", "reenviar", "node-1:1"]) == 1
        assert "no hay ninguna operacion" in capsys.readouterr().err
    finally:
        core._db_path = None
        core._database_url = None
