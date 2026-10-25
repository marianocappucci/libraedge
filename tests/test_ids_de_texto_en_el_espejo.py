"""Las altas del nodo con ids de **texto** (v0.23, ADR-026): reapuntar, bajar el mapeo y declarar el alta.

Es lo que necesita libragenda (GestioLibra y MedLibra): `clients.id` es un uuid y `patients.id` un `p-042`, a la vez
clave primaria y clave foranea a `clients.id`. El id provisorio es el texto `"-N"` y su mapeo baja por
`libraedge_mapeo_texto`. Contra los dos motores (fixture `conn`): PostgreSQL no compara un entero con una columna de
texto, y SQLite si.
"""

import sqlite3

import pytest

from libraedge import aprovisionamiento as ap
from libraedge.altas import AltaEnNodo, Referencia, formatear_altas, parsear_altas
from libraedge.db.changelog import listar_cambios, row_id_de_clave, tablas_publicadas
from libraedge.db.guarda import MENSAJE, instalar_guarda_de_espejo, tablas_con_guarda
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import ReferenceChange, ReferenceOperation
from libraedge.mapeo import CLAVE, TABLA, TABLA_TEXTO, anotar_mapeo
from libraedge.sync.pull import MirrorApplier, PullWorker
from libraedge.sync.reconciliacion import (
    HECHO,
    PENDIENTE,
    YA_ESTABA,
    clave_es_entera,
    claves_foraneas_hacia,
    reapuntar,
)
from tests.test_aprovisionamiento import _aprov, _crear_producto

NODO = "S02-pc1"
UUID = "3f2b8c1e-5a6d-4c8e-9b7a-1d2e3f4a5b6c"
LIBRO = Referencia("asientos", "tercero_id", (("rol", "cliente"),))
ESPEJO = {"clients": "id", "lista_cliente": "client_id", TABLA: CLAVE, TABLA_TEXTO: CLAVE}


@pytest.fixture
def base(conn):
    """Un cliente provisorio (`-1`) y uno definitivo (`c-57`), con hijas de cada clase:

    - `turnos` y `patients`: FK normales; `patients.id` es **a la vez** la clave primaria y la FK (el caso de MedLibra);
    - `lista_cliente`: FK de una tabla **espejada** (la autoridad es el central);
    - `asientos`: referencia sin FK, compartida con proveedores (`rol`).
    """
    for ddl in (
        "CREATE TABLE clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, dni TEXT)",
        "CREATE TABLE turnos (id TEXT PRIMARY KEY, client_id TEXT REFERENCES clients(id), hora TEXT)",
        "CREATE TABLE patients (id TEXT PRIMARY KEY REFERENCES clients(id), obra TEXT)",
        "CREATE TABLE lista_cliente (client_id TEXT PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE, lista TEXT)",
        "CREATE TABLE asientos (id INTEGER PRIMARY KEY, rol TEXT NOT NULL, tercero_id TEXT NOT NULL, importe INTEGER)",
    ):
        conn.execute(ddl)
    conn.execute("INSERT INTO clients (id, name, dni) VALUES ('-1', 'Ana (nodo)', '20-1')")
    conn.execute("INSERT INTO clients (id, name, dni) VALUES ('c-57', 'Ana', '20-1')")
    conn.execute("INSERT INTO clients (id, name, dni) VALUES ('c-58', 'Otro', '20-2')")
    conn.execute("INSERT INTO turnos (id, client_id, hora) VALUES ('t1', '-1', '09:00'), ('t2', '-1', '10:00'),"
                 " ('t3', 'c-58', '11:00')")
    conn.execute("INSERT INTO patients (id, obra) VALUES ('-1', 'OSDE')")
    conn.execute("INSERT INTO lista_cliente (client_id, lista) VALUES ('-1', 'mayorista'), ('c-58', 'minorista')")
    conn.execute("INSERT INTO asientos (id, rol, tercero_id, importe) VALUES"
                 " (1, 'cliente', '-1', 100), (2, 'proveedor', '-1', 5), (3, 'cliente', 'c-58', 7)")
    conn.commit()
    return conn


def _filas(conn, sql):
    return sorted(tuple(f) for f in conn.execute(sql).fetchall())


# ── reapuntar con ids de texto ──────────────────────────────────────────────

def test_las_claves_foraneas_de_texto_se_leen_del_catalogo(base):
    assert claves_foraneas_hacia(base, "clients") == [
        ("lista_cliente", "client_id"), ("patients", "id"), ("turnos", "client_id")]


def test_reapunta_las_claves_foraneas_de_texto_y_borra_la_provisoria(base):
    resultado = reapuntar(base, "clients", "-1", "c-57", referencias=(LIBRO,), espejadas={"lista_cliente"})
    base.commit()

    assert resultado == HECHO
    assert _filas(base, "SELECT id, client_id FROM turnos") == [("t1", "c-57"), ("t2", "c-57"), ("t3", "c-58")]
    assert _filas(base, "SELECT id, name FROM clients") == [("c-57", "Ana"), ("c-58", "Otro")]


def test_la_clave_primaria_que_es_tambien_clave_foranea_se_reapunta(base):
    """El `patients.id` de MedLibra: la ficha del paciente provisorio pasa a colgar del cliente definitivo."""
    reapuntar(base, "clients", "-1", "c-57", espejadas={"lista_cliente"})
    base.commit()
    assert _filas(base, "SELECT id, obra FROM patients") == [("c-57", "OSDE")]


def test_la_referencia_sin_fk_de_texto_se_reapunta_solo_donde_corresponde(base):
    reapuntar(base, "clients", "-1", "c-57", referencias=(LIBRO,), espejadas={"lista_cliente"})
    base.commit()
    assert _filas(base, "SELECT id, rol, tercero_id FROM asientos") == [
        (1, "cliente", "c-57"), (2, "proveedor", "-1"), (3, "cliente", "c-58")]


def test_lo_de_una_tabla_espejada_se_borra_y_no_se_reapunta_con_ids_de_texto(base):
    reapuntar(base, "clients", "-1", "c-57", espejadas={"lista_cliente"})
    base.commit()
    assert _filas(base, "SELECT client_id FROM lista_cliente") == [("c-58",)]


def test_si_la_provisoria_de_texto_ya_no_esta_no_hace_nada(base):
    reapuntar(base, "clients", "-1", "c-57", espejadas={"lista_cliente"})
    base.commit()
    assert reapuntar(base, "clients", "-1", "c-57", espejadas={"lista_cliente"}) == YA_ESTABA


def test_si_la_definitiva_de_texto_todavia_no_bajo_no_toca_nada(base):
    assert reapuntar(base, "clients", "-1", "c-99", referencias=(LIBRO,), espejadas={"lista_cliente"}) == PENDIENTE
    base.commit()
    assert _filas(base, "SELECT client_id FROM turnos WHERE id IN ('t1', 't2')") == [("-1",), ("-1",)]
    assert _filas(base, "SELECT id FROM clients WHERE id = '-1'") == [("-1",)]


@pytest.mark.parametrize("provisorio, definitivo", [
    ("5", "c-57"), ("p-042", "c-57"), ("", "c-57"), ("-1", "-2"), ("-1", ""), ("-1", None), ("-1", -2), (None, "c-57")])
def test_solo_reapunta_de_un_menos_n_a_un_id_del_central(base, provisorio, definitivo):
    with pytest.raises(ValueError, match="no es un id"):
        reapuntar(base, "clients", provisorio, definitivo)
    assert _filas(base, "SELECT id FROM clients") == [("-1",), ("c-57",), ("c-58",)]


def test_clave_es_entera_lee_el_tipo_de_la_columna(base):
    base.execute("CREATE TABLE clientes (id INTEGER PRIMARY KEY)")
    base.execute("CREATE TABLE grandes (id BIGINT PRIMARY KEY)")
    assert clave_es_entera(base, "clientes") and clave_es_entera(base, "grandes")
    assert not clave_es_entera(base, "clients") and not clave_es_entera(base, "patients")
    assert not clave_es_entera(base, "asientos", "rol")
    assert clave_es_entera(base, "no_existe"), "lo que no esta se comporta como antes de v0.23"


# ── el espejo: baja el mapeo de texto ───────────────────────────────────────

def cliente(cursor, id_, nombre="Ana"):
    return ReferenceChange(cursor=cursor, table_name="clients", row_id=id_, operation=ReferenceOperation.UPSERT,
                           payload={"id": id_, "name": nombre, "dni": "20-1"})


def mapeo_de_texto(cursor, id_nodo, id_central, node_id=NODO, tabla="clients", operacion="upsert"):
    payload = {"node_id": node_id, "tabla": tabla, "id_nodo": id_nodo, "id_central": id_central,
               "creado_at": "2026-10-25T12:00:00+00:00"}
    return ReferenceChange(cursor=cursor, table_name=TABLA_TEXTO, row_id=row_id_de_clave((node_id, tabla, id_nodo)),
                           operation=ReferenceOperation(operacion), payload=payload)


@pytest.fixture
def aplicador(base):
    return MirrorApplier(base, ESPEJO, node_id=NODO, altas=[AltaEnNodo("clients", (LIBRO,), id_de_texto=True)])


def test_baja_el_cliente_y_despues_el_mapeo_de_texto_y_el_nodo_reapunta_todo(base, aplicador):
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    base.commit()

    assert _filas(base, "SELECT id, client_id FROM turnos") == [("t1", "c-57"), ("t2", "c-57"), ("t3", "c-58")]
    assert _filas(base, "SELECT id FROM patients") == [("c-57",)]
    assert _filas(base, "SELECT tercero_id FROM asientos WHERE id = 1") == [("c-57",)]
    assert _filas(base, "SELECT id, name FROM clients") == [("c-57", "Ana"), ("c-58", "Otro")], "la provisoria se borro"
    assert aplicador.sin_reconciliar == {}


def test_el_mapeo_de_texto_de_este_nodo_queda_guardado_como_texto(base, aplicador):
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    base.commit()
    assert _filas(base, f"SELECT node_id, tabla, id_nodo, id_central FROM {TABLA_TEXTO}") == [
        (NODO, "clients", "-1", "c-57")]
    assert _filas(base, f"SELECT * FROM {TABLA}") == [], "el mapeo de enteros no se toca"


def test_reaplicar_el_mismo_mapeo_de_texto_despues_de_un_corte_no_hace_nada(base, aplicador):
    for _ in range(2):
        aplicador.aplicar(cliente(1, "c-57"))
        aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
        base.commit()
    assert _filas(base, "SELECT client_id FROM turnos") == [("c-57",), ("c-57",), ("c-58",)]
    assert _filas(base, "SELECT id FROM clients") == [("c-57",), ("c-58",)]


def test_dos_altas_de_texto_que_el_central_unifico_terminan_en_una_sola_ficha(base, aplicador):
    base.execute("INSERT INTO clients (id, name, dni) VALUES ('-2', 'Ana otra vez', '20-1')")
    base.execute("INSERT INTO turnos (id, client_id, hora) VALUES ('t4', '-2', '12:00')")
    base.commit()
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    aplicador.aplicar(mapeo_de_texto(3, "-2", "c-57"))
    base.commit()
    assert _filas(base, "SELECT client_id FROM turnos") == [("c-57",), ("c-57",), ("c-57",), ("c-58",)]
    assert _filas(base, "SELECT id FROM clients") == [("c-57",), ("c-58",)]


def test_el_mapeo_de_texto_de_otro_nodo_se_ignora(base, aplicador):
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57", node_id="S03-pc1"))
    base.commit()
    assert _filas(base, f"SELECT * FROM {TABLA_TEXTO}") == []
    assert _filas(base, "SELECT client_id FROM turnos WHERE id IN ('t1', 't2')") == [("-1",), ("-1",)]


def test_borrar_una_fila_del_mapeo_de_texto_no_deshace_nada(base, aplicador):
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    aplicador.aplicar(mapeo_de_texto(3, "-1", "c-57", operacion="delete"))
    base.commit()
    assert _filas(base, "SELECT client_id FROM turnos") == [("c-57",), ("c-57",), ("c-58",)]


def test_el_mapeo_de_texto_antes_que_el_cliente_espera_y_el_barrido_lo_termina(base, aplicador):
    aplicador.aplicar(mapeo_de_texto(1, "-1", "c-99"))
    base.commit()
    assert _filas(base, "SELECT client_id FROM turnos WHERE id IN ('t1', 't2')") == [("-1",), ("-1",)]
    assert ("clients", "-1") in aplicador.sin_reconciliar

    assert aplicador.reconciliar_pendientes() == 0, "sigue sin bajar"

    aplicador.aplicar(cliente(2, "c-99"))
    base.commit()
    assert aplicador.reconciliar_pendientes() == 1
    assert _filas(base, "SELECT client_id FROM turnos") == [("c-58",), ("c-99",), ("c-99",)]
    assert _filas(base, "SELECT id FROM clients") == [("c-57",), ("c-58",), ("c-99",)]
    assert aplicador.sin_reconciliar == {}


def test_el_barrido_sin_provisorias_de_texto_no_hace_nada(base, aplicador):
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    base.commit()
    assert aplicador.reconciliar_pendientes() == 0


def test_un_nodo_que_no_espeja_el_mapeo_de_texto_no_barre_las_tablas_de_texto(base):
    sin_texto = {k: v for k, v in ESPEJO.items() if k != TABLA_TEXTO}
    aplicador = MirrorApplier(base, sin_texto, node_id=NODO, altas=[AltaEnNodo("clients", (LIBRO,))])
    assert aplicador.reconciliar_pendientes() == 0
    assert _filas(base, "SELECT id FROM clients") == [("-1",), ("c-57",), ("c-58",)]


def test_un_reapuntado_de_texto_que_falla_no_frena_el_espejo_y_se_reintenta(base, aplicador, capsys):
    base.execute("CREATE TABLE perfil (id TEXT PRIMARY KEY, client_id TEXT UNIQUE REFERENCES clients(id))")
    base.execute("INSERT INTO perfil (id, client_id) VALUES ('f1', '-1')")
    base.commit()
    aplicador.aplicar(cliente(1, "c-57"))
    base.execute("INSERT INTO perfil (id, client_id) VALUES ('f2', 'c-57')")    # choca con la UNIQUE
    base.commit()

    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    aplicador.aplicar(cliente(3, "c-59", nombre="Beto"))      # lo que viene detras se aplica igual
    base.commit()

    assert _filas(base, "SELECT id FROM clients") == [("-1",), ("c-57",), ("c-58",), ("c-59",)]
    assert _filas(base, "SELECT client_id FROM turnos WHERE id IN ('t1', 't2')") == [("-1",), ("-1",)]
    assert ("clients", "-1") in aplicador.sin_reconciliar
    assert "no se pudo reapuntar clients -1 -> c-57" in capsys.readouterr().err

    base.execute("DELETE FROM perfil WHERE id = 'f1'")
    base.commit()
    assert aplicador.reconciliar_pendientes() == 1
    assert _filas(base, "SELECT client_id FROM turnos WHERE id IN ('t1', 't2')") == [("c-57",), ("c-57",)]
    assert aplicador.sin_reconciliar == {}


def test_el_ciclo_de_bajada_completo_con_ids_de_texto(base):
    class Central:
        def pull(self, cursor, limit):
            return (mapeo_de_texto(1, "-1", "c-57"), cliente(2, "c-57")) if cursor < 1 else ()

    repo = NodeRepository(base)
    repo.asegurar_identidad_local(NODO)
    aplicador = MirrorApplier(base, ESPEJO, node_id=NODO, altas=[AltaEnNodo("clients", (LIBRO,))])
    PullWorker(repo, Central(), aplicador).run_once(NODO)

    assert _filas(base, "SELECT client_id FROM turnos") == [("c-57",), ("c-57",), ("c-58",)]
    assert _filas(base, "SELECT id FROM clients") == [("c-57",), ("c-58",)]
    assert repo.get_server_cursor(NODO) == 2


def test_un_nodo_con_tablas_de_enteros_y_de_texto_reapunta_cada_una_con_su_mapeo(base):
    """Un producto puede tener las dos (LibraDesk, de enteros, no; pero la maquinaria no lo impide)."""
    for ddl in (
        "CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)",
        "CREATE TABLE ventas (id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id))",
    ):
        base.execute(ddl)
    base.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Beto (nodo)'), (91, 'Beto')")
    base.execute("INSERT INTO ventas (id, cliente_id) VALUES (1, -1)")
    base.commit()
    espejo = {**ESPEJO, "clientes": "id"}
    aplicador = MirrorApplier(base, espejo, node_id=NODO,
                              altas=[AltaEnNodo("clients", (LIBRO,), id_de_texto=True), AltaEnNodo("clientes")])

    entero = ReferenceChange(
        cursor=3, table_name=TABLA, row_id=row_id_de_clave((NODO, "clientes", -1)), operation=ReferenceOperation.UPSERT,
        payload={"node_id": NODO, "tabla": "clientes", "id_nodo": -1, "id_central": 91, "creado_at": "x"})
    aplicador.aplicar(cliente(1, "c-57"))
    aplicador.aplicar(mapeo_de_texto(2, "-1", "c-57"))
    aplicador.aplicar(entero)
    base.commit()

    assert _filas(base, "SELECT id, cliente_id FROM ventas") == [(1, 91)]
    assert _filas(base, "SELECT id FROM clientes") == [(91,)]
    assert _filas(base, "SELECT client_id FROM turnos WHERE id = 't1'") == [("c-57",)]
    assert _filas(base, f"SELECT id_nodo, id_central FROM {TABLA}") == [(-1, 91)]
    assert _filas(base, f"SELECT id_nodo, id_central FROM {TABLA_TEXTO}") == [("-1", "c-57")]
    assert aplicador.sin_reconciliar == {}


def test_el_barrido_atiende_las_tablas_de_enteros_y_las_de_texto_a_la_vez(base):
    for ddl in (
        "CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)",
        "CREATE TABLE ventas (id INTEGER PRIMARY KEY, cliente_id INTEGER REFERENCES clientes(id))",
    ):
        base.execute(ddl)
    base.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Beto (nodo)')")
    base.execute("INSERT INTO ventas (id, cliente_id) VALUES (1, -1)")
    base.commit()
    aplicador = MirrorApplier(base, {**ESPEJO, "clientes": "id"}, node_id=NODO,
                              altas=[AltaEnNodo("clients", (LIBRO,)), AltaEnNodo("clientes")])
    aplicador.aplicar(mapeo_de_texto(1, "-1", "c-99"))
    aplicador.aplicar(ReferenceChange(
        cursor=2, table_name=TABLA, row_id=row_id_de_clave((NODO, "clientes", -1)), operation=ReferenceOperation.UPSERT,
        payload={"node_id": NODO, "tabla": "clientes", "id_nodo": -1, "id_central": 91, "creado_at": "x"}))
    base.commit()
    assert {("clients", "-1"), ("clientes", -1)} == set(aplicador.sin_reconciliar)

    aplicador.aplicar(cliente(3, "c-99"))
    aplicador.aplicar(ReferenceChange(cursor=4, table_name="clientes", row_id="91",
                                      operation=ReferenceOperation.UPSERT, payload={"id": 91, "nombre": "Beto"}))
    base.commit()
    assert aplicador.reconciliar_pendientes() == 2
    assert aplicador.sin_reconciliar == {}
    assert _filas(base, "SELECT cliente_id FROM ventas") == [(91,)]


# ── la guarda del espejo deja pasar un «-N» de texto ────────────────────────

def test_la_guarda_deja_pasar_las_filas_con_id_provisorio_de_texto(base, motor):
    if motor == "sqlite":
        pytest.skip("la guarda es un trigger PL/pgSQL y el nodo real corre PostgreSQL; en SQLite es un no-op")
    base.execute("DELETE FROM turnos")
    base.execute("DELETE FROM patients")
    base.execute("DELETE FROM lista_cliente")
    base.execute("DELETE FROM clients")
    base.commit()
    assert instalar_guarda_de_espejo(base, ["clients"], {"clients": "id"}) == ("clients",)
    base.commit()
    assert tablas_con_guarda(base) == ("clients",)

    base.execute("INSERT INTO clients (id, name) VALUES ('-1', 'provisoria')")        # alta del nodo: pasa
    base.execute("UPDATE clients SET name = 'editada' WHERE id = '-1'")               # edicion de la provisoria: pasa
    base.commit()
    base.execute("DELETE FROM clients WHERE id = '-1'")                                # baja de la provisoria: pasa
    base.commit()
    for sql in (
        f"INSERT INTO clients (id, name) VALUES ('{UUID}', 'del central')",
        "INSERT INTO clients (id, name) VALUES ('p-042', 'del central')",
    ):
        with pytest.raises(sqlite3.DatabaseError) as exc:
            base.execute(sql)
        base.rollback()
        assert MENSAJE in str(exc.value)

    base.execute("INSERT INTO clients (id, name) VALUES ('-2', 'otra')")
    base.commit()
    with pytest.raises(sqlite3.DatabaseError, match=MENSAJE):
        base.execute("UPDATE clients SET id = 'c-9' WHERE id = '-2'")   # convertir una provisoria en del central: no
    base.rollback()


# ── declarar el alta ────────────────────────────────────────────────────────

def test_una_alta_de_ids_de_texto_suma_el_mapeo_de_texto_al_espejo_y_a_las_de_solo_lectura():
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "modulos"],
                   altas_en_nodo=[AltaEnNodo("modulos", id_de_texto=True)])
    assert aprov.hay_ids_de_texto
    assert TABLA in aprov.nombres and TABLA_TEXTO in aprov.nombres
    assert aprov.solo_lectura == frozenset({TABLA, TABLA_TEXTO}), "en el nodo solo el espejo escribe los mapeos"
    assert next(t for t in aprov.tablas if t.nombre == TABLA_TEXTO).clave == ("node_id", "tabla", "id_nodo")


def test_sin_ids_de_texto_el_espejo_no_cambia(conn):
    """Un producto de enteros (los cinco de hoy) no ve una tabla mas."""
    aprov = _aprov(altas_en_nodo=["categorias"])
    assert not aprov.hay_ids_de_texto and TABLA_TEXTO not in aprov.nombres
    assert aprov.solo_lectura == frozenset({TABLA})
    alta = ap.registrar(_crear_producto(conn), aprov, "node-1", salida=lambda _: None)
    assert TABLA_TEXTO not in alta.espejo and TABLA_TEXTO not in alta.solo_lectura


def test_si_el_producto_ya_listaba_el_mapeo_de_texto_no_se_duplica():
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "modulos", TABLA_TEXTO],
                   altas_en_nodo=[AltaEnNodo("modulos", id_de_texto=True)])
    assert [t.nombre for t in aprov.tablas].count(TABLA_TEXTO) == 1


def test_el_alta_de_ids_de_texto_lleva_el_mapeo_de_texto_a_la_carga_del_nodo(conn):
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "modulos"],
                   altas_en_nodo=[AltaEnNodo("modulos", id_de_texto=True)])
    alta = ap.registrar(_crear_producto(conn), aprov, "node-1", salida=lambda _: None)
    assert alta.espejo[TABLA_TEXTO] == ("node_id", "tabla", "id_nodo") and alta.espejo[TABLA] == CLAVE
    assert TABLA_TEXTO in alta.solo_lectura
    lineas = alta.entorno_del_nodo()
    assert "libraedge_mapeo_texto:node_id+tabla+id_nodo" in ",".join(
        linea for linea in lineas if linea.startswith("LIBRAEDGE_TABLAS_ESPEJO="))
    # La variable de las altas es la de siempre: el tipo lo ve el nodo en su base, no hace falta mandarlo.
    assert "LIBRAEDGE_ALTAS_EN_NODO=modulos" in lineas


def test_el_indicador_de_texto_no_viaja_en_la_variable_de_las_altas():
    altas = [AltaEnNodo("modulos", (LIBRO,), id_de_texto=True)]
    assert formatear_altas(altas) == "modulos:asientos.tercero_id@rol=cliente"
    assert parsear_altas(formatear_altas(altas)) == (AltaEnNodo("modulos", (LIBRO,)),)
    assert AltaEnNodo("modulos") == AltaEnNodo("modulos", id_de_texto=False)


def test_no_registra_el_nodo_si_el_central_no_puede_publicar_el_mapeo_de_texto(conn):
    producto = _crear_producto(conn)
    producto.execute(f"DROP TABLE {TABLA_TEXTO}")
    producto.commit()
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "modulos"],
                   altas_en_nodo=[AltaEnNodo("modulos", id_de_texto=True)])
    with pytest.raises(ap.ErrorDeAprovisionamiento, match=TABLA_TEXTO):
        ap.registrar(producto, aprov, "node-1", salida=lambda _: None)
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_publicar_publica_el_mapeo_de_texto_con_ids_de_texto_en_el_changelog(conn, motor):
    """Y el payload que baja al nodo, aplicado tal cual, reapunta: de punta a punta, con un mapeo de texto."""
    if motor == "sqlite":
        pytest.skip("publicar es del central y el changelog por trigger es PostgreSQL")
    producto = _crear_producto(conn)
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "modulos"],
                   altas_en_nodo=[AltaEnNodo("modulos", id_de_texto=True)])
    assert ap.publicar(producto, aprov, salida=lambda _: None) == 0
    assert TABLA_TEXTO in tablas_publicadas(producto)

    anotar_mapeo(producto, NODO, "modulos", "-3", "ventas")
    producto.commit()
    cambio = [c for c in listar_cambios(producto) if c.table_name == TABLA_TEXTO][-1]
    assert cambio.row_id == '["S02-pc1", "modulos", "-3"]'
    assert cambio.payload["id_nodo"] == "-3" and cambio.payload["id_central"] == "ventas"

    # El nodo recibe ese cambio: la provisoria de `modulos` ("-3") pasa a "ventas".
    producto.execute("DELETE FROM modulos")
    producto.execute(f"DELETE FROM {TABLA_TEXTO}")
    producto.execute("INSERT INTO modulos (modulo, habilitado) VALUES ('-3', 1), ('ventas', 1)")
    producto.commit()
    aplicador = MirrorApplier(producto, {"modulos": "modulo", TABLA: CLAVE, TABLA_TEXTO: CLAVE}, node_id=NODO,
                              altas=[AltaEnNodo("modulos")])
    aplicador.aplicar(cambio)
    producto.commit()
    assert _filas(producto, "SELECT modulo FROM modulos") == [("ventas",)]
    assert _filas(producto, f"SELECT id_nodo, id_central FROM {TABLA_TEXTO}") == [("-3", "ventas")]
