"""De la declaracion del producto al nodo que reapunta sus altas (v0.18, ADR-021).

`Aprovisionamiento.altas_en_nodo` -> `Alta` -> carga (`LIBRAEDGE_ALTAS_EN_NODO`) -> `nodo.env` -> `libraedge-nodo`
(el aplicador con `node_id` y `altas`, y la guarda que deja pasar lo provisorio). Es opt-in: sin declarar nada, nada
cambia.
"""

import os

import pytest

from libraedge import aprovisionamiento as ap
from libraedge import instalador as ins
from libraedge.altas import AltaEnNodo, Referencia
from libraedge.cli import altas_en_nodo, construir_nodo
from libraedge.db.changelog import listar_cambios, tablas_publicadas
from libraedge.db.guarda import tablas_con_guarda
from libraedge.mapeo import TABLA, anotar_mapeo
from tests.test_aprovisionamiento import _aprov, _crear_producto
from tests.test_instalador import SECRETO, _armar, _carga

ASIENTOS = Referencia("asientos", "tercero_id", (("rol", "cliente"),))
ALTAS = [AltaEnNodo("categorias", (ASIENTOS,))]


def _con_altas(**cambios):
    return _aprov(altas_en_nodo=ALTAS, **cambios)


# ── el producto lo declara ──────────────────────────────────────────────────

def test_por_defecto_el_producto_no_declara_altas_y_nada_cambia():
    aprov = _aprov()
    assert aprov.altas == () and aprov.nombres == {"categorias", "productos"} and aprov.solo_lectura == frozenset()


def test_declarar_altas_suma_el_mapeo_al_espejo_y_a_las_de_solo_lectura():
    aprov = _con_altas()
    assert aprov.altas == (AltaEnNodo("categorias", (ASIENTOS,)),)
    assert TABLA in aprov.nombres
    assert aprov.solo_lectura == frozenset({TABLA}), "en el nodo solo el espejo escribe el mapeo"
    mapeo = next(t for t in aprov.tablas if t.nombre == TABLA)
    assert mapeo.clave == ("node_id", "tabla", "id_nodo")


def test_si_el_producto_ya_listaba_el_mapeo_no_se_duplica():
    aprov = _con_altas(tablas_de_referencia=["categorias", "productos", TABLA])
    assert [t.nombre for t in aprov.tablas].count(TABLA) == 1


def test_una_alta_tiene_que_ser_de_una_tabla_de_referencia():
    with pytest.raises(ValueError, match="no estan en las tablas de referencia"):
        _aprov(altas_en_nodo=["ventas"])
    with pytest.raises(ValueError, match="identificador"):
        _aprov(altas_en_nodo=["categorias; DROP TABLE x"])


# ── registrar ───────────────────────────────────────────────────────────────

def test_el_alta_lleva_las_altas_el_mapeo_y_la_variable(conn):
    alta = ap.registrar(_crear_producto(conn), _con_altas(), "node-1", salida=lambda _: None)

    assert alta.altas == (AltaEnNodo("categorias", (ASIENTOS,)),)
    assert alta.espejo[TABLA] == ("node_id", "tabla", "id_nodo")
    assert "libraedge_mapeo:node_id+tabla+id_nodo" in ",".join(
        linea for linea in alta.entorno_del_nodo() if linea.startswith("LIBRAEDGE_TABLAS_ESPEJO="))
    assert "LIBRAEDGE_ALTAS_EN_NODO=categorias:asientos.tercero_id@rol=cliente" in alta.entorno_del_nodo()
    assert TABLA in alta.solo_lectura


def test_de_registrar_a_la_carga_y_al_nodo_env_la_variable_llega_entera(conn):
    """El valor lleva un `=` adentro (`@rol=cliente`): el central parte cada linea en el primero, y la carga que lee el
    instalador la valida con las mismas reglas con que la va a leer el nodo."""
    alta = ap.registrar(_crear_producto(conn), _con_altas(), "node-1", salida=lambda _: None)
    entorno = dict(linea.split("=", 1) for linea in alta.entorno_del_nodo())
    assert entorno["LIBRAEDGE_ALTAS_EN_NODO"] == "categorias:asientos.tercero_id@rol=cliente"

    carga = ins.armar_carga("juguete", entorno, "https://central.example", None)
    assert carga["entorno"]["LIBRAEDGE_ALTAS_EN_NODO"] == "categorias:asientos.tercero_id@rol=cliente"
    leida = ins.carga_desde_dict(carga, "juguete")
    assert leida.resumen()["altas_en_nodo"] == ["categorias"]


def test_sin_altas_la_carga_no_trae_la_variable_ni_el_mapeo(conn):
    alta = ap.registrar(_crear_producto(conn), _aprov(), "node-1", salida=lambda _: None)
    assert alta.altas == () and TABLA not in alta.espejo
    assert not any("ALTAS_EN_NODO" in linea for linea in alta.entorno_del_nodo())


def test_no_registra_el_nodo_si_el_central_no_puede_publicar_el_mapeo(conn):
    """Un nodo registrado asi daria de alta filas que nunca se reapuntan: mejor no registrarlo. No deja nada escrito."""
    producto = _crear_producto(conn)
    producto.execute(f"DROP TABLE {TABLA}")
    producto.commit()
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="libraedge_mapeo"):
        ap.registrar(producto, _con_altas(), "node-1", salida=lambda _: None)
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_no_registra_el_nodo_si_la_tabla_con_alta_no_esta_en_la_base(conn):
    producto = _crear_producto(conn)
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "no_existe"], altas_en_nodo=["no_existe"])
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="no_existe"):
        ap.registrar(producto, aprov, "node-1", salida=lambda _: None)


def test_publicar_publica_el_mapeo_y_el_central_lo_baja_a_los_nodos(conn, motor):
    """El mapeo es una tabla mas del espejo: el trigger del changelog la captura con su clave compuesta."""
    if motor == "sqlite":
        pytest.skip("publicar es del central y el changelog por trigger es PostgreSQL")
    producto = _crear_producto(conn)
    assert ap.publicar(producto, _con_altas(), salida=lambda _: None) == 0
    assert TABLA in tablas_publicadas(producto)

    anotar_mapeo(producto, "S02-pc1", "categorias", -3, 1)
    producto.commit()
    cambio = [c for c in listar_cambios(producto) if c.table_name == TABLA][-1]
    assert cambio.row_id == '["S02-pc1", "categorias", "-3"]'
    assert cambio.payload["id_nodo"] == -3 and cambio.payload["id_central"] == 1


# ── la carga y el entorno del nodo ──────────────────────────────────────────

ESPEJO_CON_ALTAS = "branches:id,party_roles:party_id+role,libraedge_mapeo:node_id+tabla+id_nodo"
VARIABLES = {"LIBRAEDGE_TABLAS_ESPEJO": ESPEJO_CON_ALTAS,
             "LIBRAEDGE_ALTAS_EN_NODO": "branches:asientos.tercero_id@rol=cliente"}


def test_la_carga_acepta_las_altas_y_llegan_al_nodo_env():
    carga = ins.carga_desde_dict(_carga(variables=VARIABLES), "ventalibra")
    assert carga.entorno["LIBRAEDGE_ALTAS_EN_NODO"] == "branches:asientos.tercero_id@rol=cliente"
    assert carga.resumen()["altas_en_nodo"] == ["branches"]
    assert _armar(carga=carga)["LIBRAEDGE_ALTAS_EN_NODO"] == "branches:asientos.tercero_id@rol=cliente"


def test_sin_la_variable_el_nodo_env_queda_como_en_v017():
    carga = ins.carga_desde_dict(_carga(), "ventalibra")
    assert "LIBRAEDGE_ALTAS_EN_NODO" not in carga.entorno and "altas_en_nodo" not in carga.resumen()
    assert "LIBRAEDGE_ALTAS_EN_NODO" not in _armar(carga=carga)
    vacia = ins.carga_desde_dict(_carga(variables={"LIBRAEDGE_ALTAS_EN_NODO": " "}), "ventalibra")
    assert "LIBRAEDGE_ALTAS_EN_NODO" not in vacia.entorno


@pytest.mark.parametrize("valor, error", [
    ("ventas", "no espeja"),                                # no es del espejo
    ("branches:asientos", "no se entiende"),                # la referencia sin columna
    ("branches; DROP TABLE x", "no se entiende"),
    ("party_roles", "clave compuesta"),
    ("branches\nPATH=/x", "salto de linea"),
])
def test_la_carga_rechaza_altas_que_no_corresponden(valor, error):
    with pytest.raises(ins.ErrorDeInstalacion, match=error):
        ins.carga_desde_dict(_carga(variables={**VARIABLES, "LIBRAEDGE_ALTAS_EN_NODO": valor}), "ventalibra")


def test_la_carga_rechaza_altas_sin_el_mapeo_en_el_espejo():
    with pytest.raises(ins.ErrorDeInstalacion, match="libraedge_mapeo"):
        ins.carga_desde_dict(
            _carga(variables={"LIBRAEDGE_ALTAS_EN_NODO": "branches"}), "ventalibra")


def test_armar_carga_la_incluye_solo_si_el_alta_la_trae():
    base = {k: v for k, v in _carga()["entorno"].items() if k != "LIBRAEDGE_CENTRAL_URL"}
    assert "LIBRAEDGE_ALTAS_EN_NODO" not in ins.armar_carga("ventalibra", base, "https://c.com.ar", None)["entorno"]
    con = ins.armar_carga("ventalibra", {**base, **VARIABLES}, "https://c.com.ar", None)
    assert con["entorno"]["LIBRAEDGE_ALTAS_EN_NODO"] == "branches:asientos.tercero_id@rol=cliente"


def test_una_definicion_de_producto_no_puede_fijar_la_variable():
    from tests.test_instalador import _definicion

    with pytest.raises(ins.ErrorDeInstalacion, match="LIBRAEDGE_ALTAS_EN_NODO"):
        ins.definicion_desde_dict(_definicion(variables={"LIBRAEDGE_ALTAS_EN_NODO": "x"}))


# ── el proceso del nodo ─────────────────────────────────────────────────────

def test_altas_en_nodo_del_cli():
    espejo = {"clients": "id", "parties": "id", TABLA: ("node_id", "tabla", "id_nodo")}
    altas = altas_en_nodo("clients:cc_asientos.tercero_id@rol=cliente,parties", espejo)
    assert [a.tabla for a in altas] == ["clients", "parties"]
    assert altas[0].referencias == (Referencia("cc_asientos", "tercero_id", (("rol", "cliente"),)),)
    assert altas_en_nodo("", espejo) == () and altas_en_nodo(None, espejo) == ()
    with pytest.raises(SystemExit, match="ventas"):
        altas_en_nodo("clients,ventas", espejo)
    with pytest.raises(SystemExit, match="no se entiende"):
        altas_en_nodo("clients:x", espejo)
    with pytest.raises(SystemExit, match="compuesta"):
        altas_en_nodo("party_roles", {**espejo, "party_roles": ("party_id", "role")})
    with pytest.raises(SystemExit, match=TABLA):
        altas_en_nodo("clients", {"clients": "id"})


@pytest.fixture
def nodo_en_postgres(conn, motor, monkeypatch):
    if motor == "sqlite":
        pytest.skip("construir_nodo abre la base por LIBRAEDGE_DATABASE_URL y el nodo real es PostgreSQL")
    conn.execute("CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
    conn.commit()
    for nombre, valor in {
        "LIBRAEDGE_NODE_ID": "node-1", "LIBRAEDGE_NODE_SECRET": SECRETO,
        "LIBRAEDGE_CENTRAL_URL": "https://central.example",
        "LIBRAEDGE_DATABASE_URL": os.environ["LIBRACORE_POSTGRES_URL"],
        "LIBRAEDGE_TABLAS_ESPEJO": "clientes:id,libraedge_mapeo:node_id+tabla+id_nodo",
        "LIBRAEDGE_VERSION_ESQUEMA": "ninguna",
        "LIBRAEDGE_TABLAS_SOLO_LECTURA": "clientes,libraedge_mapeo",
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.delenv("LIBRAEDGE_ALTAS_EN_NODO", raising=False)
    return conn


def test_el_nodo_con_altas_deja_pasar_lo_provisorio_y_sigue_guardando_lo_del_central(nodo_en_postgres, monkeypatch):
    conn = nodo_en_postgres
    monkeypatch.setenv("LIBRAEDGE_ALTAS_EN_NODO", "clientes")
    nodo, conexion = construir_nodo()
    conexion.close()
    conn.rollback()
    assert tablas_con_guarda(conn) == ("clientes", TABLA)

    conn.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Ana')")
    conn.commit()
    with pytest.raises(Exception, match="solo_en_el_central"):
        conn.execute("INSERT INTO clientes (id, nombre) VALUES (5, 'colado')")
    conn.rollback()
    with pytest.raises(Exception, match="solo_en_el_central"):
        conn.execute(f"INSERT INTO {TABLA} (node_id, tabla, id_nodo, id_central, creado_at) VALUES ('x','y',-1,1,'z')")
    conn.rollback()


def test_el_nodo_sin_altas_guarda_todo_como_en_v017(nodo_en_postgres):
    conn = nodo_en_postgres
    nodo, conexion = construir_nodo()
    conexion.close()
    conn.rollback()
    with pytest.raises(Exception, match="solo_en_el_central"):
        conn.execute("INSERT INTO clientes (id, nombre) VALUES (-1, 'Ana')")
    conn.rollback()


def test_el_nodo_arma_el_aplicador_con_las_altas(nodo_en_postgres, monkeypatch):
    monkeypatch.setenv("LIBRAEDGE_ALTAS_EN_NODO", "clientes")
    nodo, conexion = construir_nodo()
    conexion.close()
    aplicador = nodo.pull_worker.applier
    assert [a.tabla for a in aplicador._altas.values()] == ["clientes"] and aplicador._node_id == "node-1"


def test_el_nodo_no_arranca_con_altas_sin_el_mapeo_en_el_espejo(nodo_en_postgres, monkeypatch):
    monkeypatch.setenv("LIBRAEDGE_TABLAS_ESPEJO", "clientes:id")
    monkeypatch.setenv("LIBRAEDGE_TABLAS_SOLO_LECTURA", "clientes")
    monkeypatch.setenv("LIBRAEDGE_ALTAS_EN_NODO", "clientes")
    with pytest.raises(SystemExit, match=TABLA):
        construir_nodo()


# ── el instalador no importa libraedge: su parser tiene que coincidir con el del nodo ───────────────────────────────

@pytest.mark.parametrize("crudo", [
    "branches",
    "branches:asientos.tercero_id@rol=cliente",
    "branches:asientos.tercero_id@rol=cliente+otra.cliente_id,party_roles2",
    "branches:asientos.tercero_id@rol=cliente@origen=caja",
    "",
])
def test_el_parser_del_instalador_coincide_con_el_del_nodo(crudo):
    from libraedge.altas import parsear_altas

    espejo = {"branches": ("id",), "party_roles2": ("id",), TABLA: ("node_id", "tabla", "id_nodo")}
    assert ins.parsear_altas_en_nodo(crudo, espejo) == tuple(a.tabla for a in parsear_altas(crudo))


@pytest.mark.parametrize("crudo", [
    "branches:asientos", "branches:asientos.tercero_id@rol", "bran ches", "branches:x.y@rol=a b",
    "branches:x.y@rol=a'b",
])
def test_los_dos_parsers_rechazan_lo_mismo(crudo):
    from libraedge.altas import parsear_altas

    with pytest.raises(ValueError):
        parsear_altas(crudo)
    with pytest.raises(ins.ErrorDeInstalacion):
        ins.parsear_altas_en_nodo(crudo, {"branches": ("id",), TABLA: ("node_id", "tabla", "id_nodo")})
