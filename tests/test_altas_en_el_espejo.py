"""El espejo reapunta las altas del nodo (v0.18, ADR-021): del `libraedge_mapeo` que baja al id definitivo.

El central dio de alta el cliente que el nodo creo con el id -1 y anoto `(S02, clientes, -1) -> 57`; esa fila baja por
el espejo como cualquier otra y es la que le dice al nodo que cambie el -1 por el 57 y borre su provisoria.
"""

import pytest

from libraedge.altas import AltaEnNodo, Referencia
from libraedge.db.changelog import row_id_de_clave
from libraedge.db.guarda import MENSAJE, instalar_guarda_de_espejo
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import ReferenceChange, ReferenceOperation
from libraedge.mapeo import CLAVE, TABLA, anotar_mapeo
from libraedge.sync.pull import MirrorApplier, PullWorker

NODO = "S02-pc1"
LIBRO = Referencia("asientos", "tercero_id", (("rol", "cliente"),))
ESPEJO = {"clientes": "id", "lista_cliente": "cliente_id", TABLA: CLAVE}


@pytest.fixture
def base(conn):
    for ddl in (
        "CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, cuit TEXT)",
        "CREATE TABLE ventas (id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id), total INTEGER)",
        "CREATE TABLE lista_cliente (cliente_id INTEGER PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,"
        " lista TEXT)",
        "CREATE TABLE asientos (id INTEGER PRIMARY KEY, rol TEXT NOT NULL, tercero_id INTEGER NOT NULL)",
    ):
        conn.execute(ddl)
    # El nodo dio de alta a Ana (-1), le vendio dos veces y le anoto un asiento de cuenta corriente.
    conn.execute("INSERT INTO clientes (id, nombre, cuit) VALUES (-1, 'Ana', '20-1')")
    conn.execute("INSERT INTO ventas (id, cliente_id, total) VALUES (1, -1, 100), (2, -1, 250)")
    conn.execute("INSERT INTO asientos (id, rol, tercero_id) VALUES (1, 'cliente', -1)")
    conn.commit()
    return conn


@pytest.fixture
def aplicador(base):
    return MirrorApplier(base, ESPEJO, node_id=NODO, altas=[AltaEnNodo("clientes", (LIBRO,))])


def cliente(cursor, id_, nombre="Ana"):
    return ReferenceChange(cursor=cursor, table_name="clientes", row_id=str(id_), operation=ReferenceOperation.UPSERT,
                           payload={"id": id_, "nombre": nombre, "cuit": "20-1"})


def mapeo(cursor, id_nodo, id_central, node_id=NODO, tabla="clientes", operacion="upsert"):
    payload = {"node_id": node_id, "tabla": tabla, "id_nodo": id_nodo, "id_central": id_central,
               "creado_at": "2026-10-21T12:00:00+00:00"}
    return ReferenceChange(cursor=cursor, table_name=TABLA, row_id=row_id_de_clave((node_id, tabla, id_nodo)),
                           operation=ReferenceOperation(operacion), payload=payload)


def filas(conn, sql):
    return sorted(tuple(f) for f in conn.execute(sql).fetchall())


# ── el recorrido que importa ────────────────────────────────────────────────

def test_baja_el_cliente_y_despues_el_mapeo_y_el_nodo_reapunta_todo(base, aplicador):
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()

    assert filas(base, "SELECT id, cliente_id, total FROM ventas") == [(1, 57, 100), (2, 57, 250)]
    assert filas(base, "SELECT id, tercero_id FROM asientos") == [(1, 57)]
    assert filas(base, "SELECT id, nombre FROM clientes") == [(57, "Ana")], "la provisoria se borro"
    assert aplicador.sin_reconciliar == {}


def test_el_mapeo_de_este_nodo_queda_guardado(base, aplicador):
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    assert filas(base, f"SELECT node_id, tabla, id_nodo, id_central FROM {TABLA}") == [(NODO, "clientes", -1, 57)]


def test_reaplicar_el_mismo_mapeo_despues_de_un_corte_no_hace_nada(base, aplicador):
    """El cursor se reaplica desde donde quedo: todo es idempotente."""
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert filas(base, "SELECT id FROM clientes") == [(57,)]


def test_lo_que_el_cliente_del_central_ya_tenia_se_conserva(base, aplicador):
    """La definitiva manda: el nodo no le pisa los datos al cliente del central, solo cambia el id."""
    aplicador.aplicar(cliente(1, 57, nombre="Ana Maria"))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    assert filas(base, "SELECT id, nombre FROM clientes") == [(57, "Ana Maria")]


def test_dos_altas_del_mismo_nodo_que_el_central_unifico_terminan_en_un_solo_cliente(base, aplicador):
    base.execute("INSERT INTO clientes (id, nombre, cuit) VALUES (-2, 'Ana otra vez', '20-1')")
    base.execute("INSERT INTO ventas (id, cliente_id, total) VALUES (3, -2, 5)")
    base.commit()
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    aplicador.aplicar(mapeo(3, -2, 57))
    base.commit()
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,), (57,)]
    assert filas(base, "SELECT id FROM clientes") == [(57,)]


# ── lo que se ignora ────────────────────────────────────────────────────────

def test_el_mapeo_de_otro_nodo_no_se_guarda_ni_toca_nada(base, aplicador):
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57, node_id="S03-pc1"))
    base.commit()
    assert filas(base, f"SELECT * FROM {TABLA}") == []
    assert filas(base, "SELECT cliente_id FROM ventas") == [(-1,), (-1,)], "el -1 de S03 no es el -1 de este nodo"
    assert filas(base, "SELECT id FROM clientes") == [(-1,), (57,)]


def test_el_mapeo_de_una_tabla_sin_alta_en_este_nodo_se_ignora(base, aplicador):
    aplicador.aplicar(mapeo(1, -1, 57, tabla="proveedores"))
    base.commit()
    assert filas(base, f"SELECT * FROM {TABLA}") == []


def test_borrar_una_fila_del_mapeo_no_deshace_nada(base, aplicador):
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    aplicador.aplicar(mapeo(3, -1, 57, operacion="delete"))
    base.commit()
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert filas(base, f"SELECT id_central FROM {TABLA}") == [(57,)]


def test_sin_altas_el_mapeo_se_guarda_como_cualquier_otra_tabla_y_no_reapunta(base):
    """Un aplicador de v0.17 (sin `altas`): el comportamiento no cambia."""
    aplicador = MirrorApplier(base, ESPEJO)
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57, node_id="S03-pc1"))
    base.commit()
    assert filas(base, f"SELECT node_id FROM {TABLA}") == [("S03-pc1",)]
    assert filas(base, "SELECT cliente_id FROM ventas") == [(-1,), (-1,)]


# ── cuando la definitiva todavia no bajo ────────────────────────────────────

def test_si_el_mapeo_llega_antes_que_el_cliente_espera_y_el_barrido_lo_termina(base, aplicador):
    aplicador.aplicar(mapeo(1, -1, 57))
    base.commit()
    assert filas(base, "SELECT cliente_id FROM ventas") == [(-1,), (-1,)], "no se borra lo unico que hay"
    assert (("clientes", -1)) in aplicador.sin_reconciliar

    assert aplicador.reconciliar_pendientes() == 0, "sigue sin bajar"

    aplicador.aplicar(cliente(2, 57))
    base.commit()
    assert aplicador.reconciliar_pendientes() == 1
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert filas(base, "SELECT id FROM clientes") == [(57,)]
    assert aplicador.sin_reconciliar == {}


def test_el_barrido_sin_provisorias_no_hace_nada(base, aplicador):
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    assert aplicador.reconciliar_pendientes() == 0


# ── un reapuntado que falla no frena el espejo ──────────────────────────────

def test_un_reapuntado_que_falla_no_frena_el_espejo_y_se_reintenta(base, aplicador, capsys):
    """El ciclo no puede quedar trabado por una fila provisoria: la deshace, avisa y sigue con el resto de la bajada."""
    base.execute("CREATE TABLE perfil (id INTEGER PRIMARY KEY, cliente_id INTEGER UNIQUE REFERENCES clientes(id))")
    base.execute("INSERT INTO perfil (id, cliente_id) VALUES (1, -1)")
    base.commit()
    aplicador.aplicar(cliente(1, 57))
    base.execute("INSERT INTO perfil (id, cliente_id) VALUES (2, 57)")    # lo mismo en el central: choca con la UNIQUE
    base.commit()

    aplicador.aplicar(mapeo(2, -1, 57))
    aplicador.aplicar(cliente(3, 58, nombre="Beto"))      # lo que viene detras se aplica igual
    base.commit()

    assert filas(base, "SELECT id FROM clientes") == [(-1,), (57,), (58,)]
    assert filas(base, "SELECT cliente_id FROM ventas") == [(-1,), (-1,)], "nada quedo a medias"
    assert ("clientes", -1) in aplicador.sin_reconciliar
    assert "no se pudo reapuntar clientes -1 -> 57" in capsys.readouterr().err

    # Alguien arregla el choque y el siguiente barrido termina el trabajo.
    base.execute("DELETE FROM perfil WHERE id = 1")
    base.commit()
    assert aplicador.reconciliar_pendientes() == 1
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert aplicador.sin_reconciliar == {}


def test_el_mismo_fallo_no_vuelve_a_avisar_en_cada_barrido(base, aplicador, capsys):
    base.execute("CREATE TABLE perfil (id INTEGER PRIMARY KEY, cliente_id INTEGER UNIQUE REFERENCES clientes(id))")
    aplicador.aplicar(cliente(1, 57))
    base.execute("INSERT INTO perfil (id, cliente_id) VALUES (1, -1), (2, 57)")
    base.commit()
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    capsys.readouterr()
    aplicador.reconciliar_pendientes()
    aplicador.reconciliar_pendientes()
    assert capsys.readouterr().err == ""


# ── el ciclo de bajada completo ─────────────────────────────────────────────

def test_el_ciclo_de_bajada_completo_y_el_barrido_final(base):
    class Central:
        def pull(self, cursor, limit):
            return (mapeo(1, -1, 57), cliente(2, 57)) if cursor < 1 else ()

    repo = NodeRepository(base)
    repo.asegurar_identidad_local(NODO)
    aplicador = MirrorApplier(base, ESPEJO, node_id=NODO, altas=[AltaEnNodo("clientes", (LIBRO,))])
    PullWorker(repo, Central(), aplicador).run_once(NODO)

    # El mapeo llego primero (no estaba la definitiva): el barrido del final del mismo ciclo lo termina.
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert filas(base, "SELECT id FROM clientes") == [(57,)]
    assert repo.get_server_cursor(NODO) == 2


# ── la configuracion ────────────────────────────────────────────────────────

def test_las_altas_piden_el_node_id(base):
    with pytest.raises(ValueError, match="node_id"):
        MirrorApplier(base, ESPEJO, altas=["clientes"])


def test_las_altas_piden_espejar_el_mapeo(base):
    with pytest.raises(ValueError, match=TABLA):
        MirrorApplier(base, {"clientes": "id"}, node_id=NODO, altas=["clientes"])


def test_una_alta_en_una_tabla_que_el_nodo_no_espeja_es_un_error(base):
    with pytest.raises(ValueError, match="no la espeja"):
        MirrorApplier(base, {TABLA: CLAVE}, node_id=NODO, altas=["clientes"])


def test_una_alta_en_una_tabla_de_clave_compuesta_no_se_admite(base):
    with pytest.raises(ValueError, match="compuesta"):
        MirrorApplier(base, {TABLA: CLAVE, "party_roles": ("party_id", "role")}, node_id=NODO, altas=["party_roles"])


# ── la guarda deja pasar las provisorias ────────────────────────────────────

@pytest.fixture
def guardada(base, motor):
    if motor == "sqlite":
        pytest.skip("la guarda es un trigger PL/pgSQL y el nodo real corre PostgreSQL; en SQLite es un no-op")
    base.execute("DELETE FROM asientos")
    base.execute("DELETE FROM ventas")
    base.execute("DELETE FROM clientes")
    base.execute("INSERT INTO clientes (id, nombre) VALUES (57, 'Del central')")
    base.commit()
    assert instalar_guarda_de_espejo(base, ["clientes"], {"clientes": "id"}) == ("clientes",)
    base.commit()
    return base


def _bloqueada(conn, sql, params=()):
    with pytest.raises(Exception) as exc:
        conn.execute(sql, params)
    conn.rollback()
    assert MENSAJE in str(exc.value)


def test_el_producto_puede_dar_de_alta_editar_y_borrar_una_provisoria(guardada):
    guardada.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Ana')")
    guardada.execute("UPDATE clientes SET nombre = 'Ana M' WHERE id = -1")
    guardada.commit()
    assert filas(guardada, "SELECT id, nombre FROM clientes WHERE id < 0") == [(-1, "Ana M")]
    guardada.execute("DELETE FROM clientes WHERE id = -1")
    guardada.commit()
    assert filas(guardada, "SELECT id FROM clientes") == [(57,)]


def test_el_producto_sigue_sin_poder_tocar_las_filas_del_central(guardada):
    _bloqueada(guardada, "INSERT INTO clientes (id, nombre) VALUES (100, 'colado')")
    _bloqueada(guardada, "UPDATE clientes SET nombre = 'otro' WHERE id = 57")
    _bloqueada(guardada, "DELETE FROM clientes WHERE id = 57")
    assert filas(guardada, "SELECT id, nombre FROM clientes") == [(57, "Del central")]


def test_no_se_puede_convertir_una_fila_en_provisoria_ni_al_reves(guardada):
    guardada.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Ana')")
    guardada.commit()
    _bloqueada(guardada, "UPDATE clientes SET id = -9 WHERE id = 57")     # esconder una del central
    _bloqueada(guardada, "UPDATE clientes SET id = 77 WHERE id = -1")     # hacerse pasar por una del central


def test_la_guarda_sin_clave_de_alta_sigue_rechazando_todo(base, motor):
    """Una tabla guardada sin alta (v0.17) no deja pasar los negativos: la excepcion es de las tablas con alta."""
    if motor == "sqlite":
        pytest.skip("la guarda es un trigger PL/pgSQL")
    instalar_guarda_de_espejo(base, ["clientes"])
    base.commit()
    _bloqueada(base, "INSERT INTO clientes (id, nombre) VALUES (-5, 'x')")


def test_el_espejo_reapunta_y_borra_la_provisoria_aunque_la_tabla_este_guardada(base, motor):
    if motor == "sqlite":
        pytest.skip("la guarda es un trigger PL/pgSQL")
    instalar_guarda_de_espejo(base, ["clientes", "lista_cliente", TABLA], {"clientes": "id"})
    base.commit()
    aplicador = MirrorApplier(base, ESPEJO, node_id=NODO, altas=[AltaEnNodo("clientes", (LIBRO,))])
    aplicador.aplicar(cliente(1, 57))
    aplicador.aplicar(mapeo(2, -1, 57))
    base.commit()
    assert filas(base, "SELECT cliente_id FROM ventas") == [(57,), (57,)]
    assert filas(base, "SELECT id FROM clientes") == [(57,)]
    assert aplicador.sin_reconciliar == {}


def test_anotar_el_mapeo_en_el_central_no_pasa_por_el_espejo(base):
    """`anotar_mapeo` es del central; el aplicador del nodo no lo usa. Un nodo que lo llamara escribiria su propia
    fila en la tabla del espejo: la guarda (que incluye el mapeo en solo lectura) lo atrapa en PostgreSQL."""
    anotar_mapeo(base, NODO, "clientes", -1, 57)
    assert filas(base, f"SELECT id_central FROM {TABLA}") == [(57,)]
