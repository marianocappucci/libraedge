"""El reapuntado de una alta del nodo (v0.18, ADR-021): del id provisorio al que asigno el central.

Contra los dos motores (fixture `conn`): las claves foraneas se leen de `pg_constraint` en PostgreSQL y de `PRAGMA
foreign_key_list` en SQLite, y borrar una fila con hijas se comporta distinto si las FK no se movieron antes.
"""

import pytest

from libraedge.altas import Referencia
from libraedge.sync.reconciliacion import (
    HECHO,
    PENDIENTE,
    YA_ESTABA,
    claves_foraneas_hacia,
    reapuntar,
    savepoint,
)

LIBRO = Referencia("asientos", "tercero_id", (("rol", "cliente"),))


@pytest.fixture
def base(conn):
    """Un cliente provisorio (-1) y uno definitivo (57), con hijas de cada clase:

    - `ventas` y `remitos`: FK normales (el segundo con `ON DELETE SET NULL`, que borraria el dato si no se reapuntara);
    - `lista_cliente`: FK de una tabla **espejada** (la autoridad es el central);
    - `asientos`: referencia sin FK, compartida con proveedores (`rol`).
    """
    for ddl in (
        "CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)",
        "CREATE TABLE ventas (id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id), total INTEGER)",
        "CREATE TABLE remitos (id INTEGER PRIMARY KEY, client_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL)",
        "CREATE TABLE lista_cliente (cliente_id INTEGER PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,"
        " lista TEXT)",
        "CREATE TABLE asientos (id INTEGER PRIMARY KEY, rol TEXT NOT NULL, tercero_id INTEGER NOT NULL,"
        " importe INTEGER)",
    ):
        conn.execute(ddl)
    conn.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Ana (nodo)')")
    conn.execute("INSERT INTO clientes (id, nombre) VALUES (57, 'Ana')")
    conn.execute("INSERT INTO clientes (id, nombre) VALUES (58, 'Otro')")
    conn.execute("INSERT INTO ventas (id, cliente_id, total) VALUES (1, -1, 100), (2, -1, 250), (3, 58, 10)")
    conn.execute("INSERT INTO remitos (id, client_id) VALUES (1, -1), (2, 58)")
    conn.execute("INSERT INTO lista_cliente (cliente_id, lista) VALUES (-1, 'mayorista'), (58, 'minorista')")
    conn.execute("INSERT INTO asientos (id, rol, tercero_id, importe) VALUES"
                 " (1, 'cliente', -1, 100), (2, 'proveedor', -1, 5), (3, 'cliente', 58, 7)")
    conn.commit()
    return conn


def _ids(conn, sql):
    return sorted(tuple(f) for f in conn.execute(sql).fetchall())


def test_las_claves_foraneas_se_leen_del_catalogo(base):
    assert claves_foraneas_hacia(base, "clientes") == [
        ("lista_cliente", "cliente_id"), ("remitos", "client_id"), ("ventas", "cliente_id")]


def test_una_clave_foranea_hacia_otra_tabla_no_aparece(base):
    base.execute("CREATE TABLE zonas (id INTEGER PRIMARY KEY)")
    base.execute("CREATE TABLE sucursales (id INTEGER PRIMARY KEY, zona_id INTEGER REFERENCES zonas(id))")
    assert claves_foraneas_hacia(base, "zonas") == [("sucursales", "zona_id")]
    assert ("sucursales", "zona_id") not in claves_foraneas_hacia(base, "clientes")


def test_reapunta_las_claves_foraneas_y_borra_la_provisoria(base):
    resultado = reapuntar(base, "clientes", -1, 57, referencias=(LIBRO,), espejadas={"lista_cliente"})
    base.commit()

    assert resultado == HECHO
    assert _ids(base, "SELECT id, cliente_id, total FROM ventas") == [(1, 57, 100), (2, 57, 250), (3, 58, 10)]
    assert _ids(base, "SELECT id, nombre FROM clientes") == [(57, "Ana"), (58, "Otro")]


def test_el_ON_DELETE_SET_NULL_no_pierde_el_vinculo_porque_se_reapunta_antes_de_borrar(base):
    reapuntar(base, "clientes", -1, 57, espejadas={"lista_cliente"})
    base.commit()
    assert _ids(base, "SELECT id, client_id FROM remitos") == [(1, 57), (2, 58)]


def test_la_referencia_sin_clave_foranea_se_reapunta_solo_donde_corresponde(base):
    """`tercero_id` sirve a varias tablas: el asiento de un PROVEEDOR con el mismo numero no es del cliente."""
    reapuntar(base, "clientes", -1, 57, referencias=(LIBRO,), espejadas={"lista_cliente"})
    base.commit()
    assert _ids(base, "SELECT id, rol, tercero_id FROM asientos") == [
        (1, "cliente", 57), (2, "proveedor", -1), (3, "cliente", 58)]


def test_sin_declarar_la_referencia_sin_fk_queda_como_estaba(base):
    """Es lo que pasa si el producto no la declara: por eso se declara."""
    reapuntar(base, "clientes", -1, 57, espejadas={"lista_cliente"})
    base.commit()
    assert _ids(base, "SELECT id, tercero_id FROM asientos") == [(1, -1), (2, -1), (3, 58)]


def test_lo_de_una_tabla_espejada_se_borra_y_no_se_reapunta(base):
    """La lista de precios del cliente la manda el central: la fila de la provisoria no es suya y vale la de 57."""
    reapuntar(base, "clientes", -1, 57, espejadas={"lista_cliente"})
    base.commit()
    assert _ids(base, "SELECT cliente_id, lista FROM lista_cliente") == [(58, "minorista")]


def test_una_tabla_espejada_con_la_definitiva_ya_bajada_no_choca(base):
    """La definitiva ya tiene su fila en la tabla espejada (bajo del central): reapuntar chocaria con la PK."""
    base.execute("INSERT INTO lista_cliente (cliente_id, lista) VALUES (57, 'mayorista')")
    base.commit()
    assert reapuntar(base, "clientes", -1, 57, espejadas={"lista_cliente"}) == HECHO
    base.commit()
    assert _ids(base, "SELECT cliente_id FROM lista_cliente") == [(57,), (58,)]


def test_si_la_provisoria_ya_no_esta_no_hace_nada(base):
    reapuntar(base, "clientes", -1, 57, referencias=(LIBRO,), espejadas={"lista_cliente"})
    base.commit()
    antes = _ids(base, "SELECT * FROM ventas")
    assert reapuntar(base, "clientes", -1, 57, referencias=(LIBRO,), espejadas={"lista_cliente"}) == YA_ESTABA
    assert _ids(base, "SELECT * FROM ventas") == antes


def test_si_la_definitiva_todavia_no_bajo_no_toca_nada(base):
    """Sin la fila 99 las FK no se podrian reapuntar, y borrar la provisoria dejaria las ventas sin cliente."""
    assert reapuntar(base, "clientes", -1, 99, referencias=(LIBRO,), espejadas={"lista_cliente"}) == PENDIENTE
    base.commit()
    assert _ids(base, "SELECT cliente_id FROM ventas WHERE id IN (1, 2)") == [(-1,), (-1,)]
    assert _ids(base, "SELECT id FROM clientes WHERE id < 0") == [(-1,)]
    assert _ids(base, "SELECT tercero_id FROM asientos WHERE id = 1") == [(-1,)]


@pytest.mark.parametrize("provisorio, definitivo", [(5, 57), (0, 57), (-1, -2)])
def test_solo_reapunta_de_un_id_negativo_a_uno_del_central(base, provisorio, definitivo):
    """Es la unica operacion del espejo que borra una fila que el central no borro: no puede apuntar a otra cosa."""
    with pytest.raises(ValueError, match="no es un id"):
        reapuntar(base, "clientes", provisorio, definitivo)
    assert _ids(base, "SELECT id FROM clientes") == [(-1,), (57,), (58,)]


def test_un_nombre_de_tabla_peligroso_no_pasa(base):
    with pytest.raises(ValueError, match="identificador"):
        reapuntar(base, "clientes; DROP TABLE ventas", -1, 57)
    with pytest.raises(ValueError, match="identificador"):
        reapuntar(base, "clientes", -1, 57, referencias=(Referencia("asientos", "tercero_id"),), clave="id; x")


def test_una_fk_de_la_tabla_hacia_si_misma_tambien_se_reapunta(conn):
    conn.execute("CREATE TABLE zonas (id INTEGER PRIMARY KEY, padre_id INTEGER REFERENCES zonas(id))")
    conn.execute("INSERT INTO zonas (id, padre_id) VALUES (7, NULL), (-1, 7), (-2, -1)")
    conn.commit()
    # la hija -2 cuelga de -1: al reapuntar -1 -> 8 tiene que quedar colgando de 8
    conn.execute("INSERT INTO zonas (id, padre_id) VALUES (8, 7)")
    reapuntar(conn, "zonas", -1, 8)
    conn.commit()
    assert _ids(conn, "SELECT id, padre_id FROM zonas") == [(-2, 8), (7, None), (8, 7)]


def test_el_savepoint_deshace_un_reapuntado_a_medias(base):
    """Si falla a la mitad, no queda la mitad escrita: ni en SQLite, que no aborta, ni en PostgreSQL, que si."""
    base.execute("CREATE TABLE perfil (id INTEGER PRIMARY KEY, cliente_id INTEGER UNIQUE REFERENCES clientes(id))")
    base.execute("INSERT INTO perfil (id, cliente_id) VALUES (1, -1), (2, 57)")
    base.commit()

    with pytest.raises(Exception) as exc, savepoint(base):
        reapuntar(base, "clientes", -1, 57, referencias=(LIBRO,), espejadas={"lista_cliente"})
    assert "unique" in str(exc.value).lower() or "UNIQUE" in str(exc.value)

    # La transaccion sigue sana y nada cambio, ni siquiera lo que se habia reapuntado antes de fallar.
    assert _ids(base, "SELECT id, cliente_id FROM ventas ORDER BY id") == [(1, -1), (2, -1), (3, 58)]
    assert _ids(base, "SELECT id FROM clientes WHERE id < 0") == [(-1,)]
    assert _ids(base, "SELECT tercero_id FROM asientos WHERE id = 1") == [(-1,)]
