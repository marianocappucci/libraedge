"""De la declaracion del producto a la guarda del nodo (v0.17, ADR-019).

`Aprovisionamiento.tablas_solo_lectura` -> `Alta` -> carga (`LIBRAEDGE_TABLAS_SOLO_LECTURA`) -> `nodo.env` ->
`libraedge-nodo` instala la guarda al arrancar. Es opt-in: sin declarar nada, nada cambia.
"""

import json
import os

import pytest

from libraedge import aprovisionamiento as ap
from libraedge import instalador as ins
from libraedge.cli import construir_nodo, tablas_solo_lectura
from libraedge.db.guarda import tablas_con_guarda
from tests.test_aprovisionamiento import _aprov, _crear_producto
from tests.test_instalador import SECRETO, _armar, _carga, _definicion

# ── el producto lo declara ──────────────────────────────────────────────────


def test_por_defecto_el_producto_no_guarda_nada():
    aprov = _aprov()
    assert aprov.solo_lectura == frozenset()


def test_se_declara_con_nombres_y_tiene_que_ser_de_referencia():
    assert _aprov(tablas_solo_lectura=["productos"]).solo_lectura == frozenset({"productos"})
    assert _aprov(tablas_solo_lectura="productos").solo_lectura == frozenset({"productos"})
    with pytest.raises(ValueError, match="no estan en las tablas de referencia"):
        _aprov(tablas_solo_lectura=["ventas"])
    with pytest.raises(ValueError, match="identificador"):
        _aprov(tablas_solo_lectura=["productos; DROP TABLE x"])


def test_el_alta_lleva_las_de_solo_lectura_que_de_verdad_se_espejan(conn):
    producto = _crear_producto(conn)
    aprov = _aprov(tablas_de_referencia=["categorias", "productos", "no_existe"],
                   tablas_solo_lectura=["productos", "no_existe"])

    alta = ap.registrar(producto, aprov, "node-1", salida=lambda _: None)

    # `no_existe` no esta en esta base: no se publica, no baja, y guardarla dejaria una tabla que nadie escribe.
    assert alta.solo_lectura == ("productos",)
    assert alta.entorno_del_nodo()[-1] == "LIBRAEDGE_TABLAS_SOLO_LECTURA=productos"


def test_sin_declarar_la_carga_no_trae_la_variable(conn):
    alta = ap.registrar(_crear_producto(conn), _aprov(), "node-1", salida=lambda _: None)
    assert alta.solo_lectura == ()
    assert not any("SOLO_LECTURA" in linea for linea in alta.entorno_del_nodo())


# ── la carga y el entorno del nodo ──────────────────────────────────────────

def test_la_carga_acepta_las_tablas_de_solo_lectura_y_llegan_al_nodo_env():
    carga = ins.carga_desde_dict(
        _carga(variables={"LIBRAEDGE_TABLAS_SOLO_LECTURA": "branches"}), "ventalibra")
    assert carga.entorno["LIBRAEDGE_TABLAS_SOLO_LECTURA"] == "branches"
    assert carga.resumen()["tablas_solo_lectura"] == 1
    assert _armar(carga=carga)["LIBRAEDGE_TABLAS_SOLO_LECTURA"] == "branches"


def test_sin_la_variable_el_nodo_env_queda_como_en_v016():
    carga = ins.carga_desde_dict(_carga(), "ventalibra")
    assert "LIBRAEDGE_TABLAS_SOLO_LECTURA" not in carga.entorno
    assert "tablas_solo_lectura" not in carga.resumen()
    assert "LIBRAEDGE_TABLAS_SOLO_LECTURA" not in _armar(carga=carga)
    # vacia es lo mismo que ausente
    vacia = ins.carga_desde_dict(_carga(variables={"LIBRAEDGE_TABLAS_SOLO_LECTURA": "  "}), "ventalibra")
    assert "LIBRAEDGE_TABLAS_SOLO_LECTURA" not in vacia.entorno


@pytest.mark.parametrize("valor, error", [
    ("ventas", "no espeja"),                       # no es del espejo
    ("branches,otra", "otra"),
    ("branches; DROP TABLE x", "no valida"),
    ("branches\nPATH=/x", "salto de linea"),
])
def test_la_carga_rechaza_tablas_de_solo_lectura_que_no_corresponden(valor, error):
    with pytest.raises(ins.ErrorDeInstalacion, match=error):
        ins.carga_desde_dict(_carga(variables={"LIBRAEDGE_TABLAS_SOLO_LECTURA": valor}), "ventalibra")


def test_la_carga_sigue_rechazando_cualquier_otra_variable():
    with pytest.raises(ins.ErrorDeInstalacion, match="no le corresponden"):
        ins.carga_desde_dict(_carga(variables={"PATH": "/x"}), "ventalibra")


def test_armar_carga_la_incluye_solo_si_el_alta_la_trae():
    base = {k: v for k, v in _carga()["entorno"].items() if k != "LIBRAEDGE_CENTRAL_URL"}
    sin = ins.armar_carga("ventalibra", base, "https://c.com.ar", None)
    assert "LIBRAEDGE_TABLAS_SOLO_LECTURA" not in sin["entorno"]
    con = ins.armar_carga("ventalibra", {**base, "LIBRAEDGE_TABLAS_SOLO_LECTURA": "branches"}, "https://c.com.ar", None)
    assert con["entorno"]["LIBRAEDGE_TABLAS_SOLO_LECTURA"] == "branches"
    assert SECRETO in json.dumps(con)  # sigue siendo la carga con el secreto: se lleva y se borra


def test_una_definicion_de_producto_no_puede_fijar_la_variable():
    with pytest.raises(ins.ErrorDeInstalacion, match="LIBRAEDGE_TABLAS_SOLO_LECTURA"):
        ins.definicion_desde_dict(_definicion(variables={"LIBRAEDGE_TABLAS_SOLO_LECTURA": "x"}))


def test_revisar_entorno_no_marca_como_faltante_la_opcional():
    definicion = ins.definicion_desde_dict(_definicion())
    assert "LIBRAEDGE_TABLAS_SOLO_LECTURA" not in ins.revisar_entorno(definicion, {})


# ── el proceso del nodo ─────────────────────────────────────────────────────

def test_tablas_solo_lectura_del_cli():
    espejo = {"productos": "id", "categorias": "id"}
    assert tablas_solo_lectura("productos, categorias", espejo) == ("categorias", "productos")
    assert tablas_solo_lectura("", espejo) == () and tablas_solo_lectura(None, espejo) == ()
    with pytest.raises(SystemExit, match="ventas"):
        tablas_solo_lectura("productos,ventas", espejo)


@pytest.fixture
def nodo_en_postgres(conn, motor, monkeypatch):
    """La base de un nodo con dos tablas espejadas y el entorno con el que arranca `libraedge-nodo`."""
    if motor == "sqlite":
        pytest.skip("construir_nodo abre la base por LIBRAEDGE_DATABASE_URL y el nodo real es PostgreSQL")
    conn.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
    conn.execute("CREATE TABLE clientes (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
    conn.commit()
    for nombre, valor in {
        "LIBRAEDGE_NODE_ID": "node-1", "LIBRAEDGE_NODE_SECRET": SECRETO,
        "LIBRAEDGE_CENTRAL_URL": "https://central.example",
        "LIBRAEDGE_DATABASE_URL": os.environ["LIBRACORE_POSTGRES_URL"],
        "LIBRAEDGE_TABLAS_ESPEJO": "productos:id,clientes:id", "LIBRAEDGE_VERSION_ESQUEMA": "ninguna",
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.delenv("LIBRAEDGE_TABLAS_SOLO_LECTURA", raising=False)
    return conn


def test_el_nodo_instala_la_guarda_al_arrancar_y_converge(nodo_en_postgres, monkeypatch):
    conn = nodo_en_postgres
    monkeypatch.setenv("LIBRAEDGE_TABLAS_SOLO_LECTURA", "productos")
    nodo, conexion = construir_nodo()
    conexion.close()
    assert tablas_con_guarda(conn) == ("productos",)

    # Se arranca de nuevo sin la variable (carga nueva sin guarda): el nodo la saca.
    monkeypatch.delenv("LIBRAEDGE_TABLAS_SOLO_LECTURA")
    nodo, conexion = construir_nodo()
    conexion.close()
    conn.rollback()
    assert tablas_con_guarda(conn) == ()


def test_el_nodo_avisa_de_una_tabla_que_todavia_no_esta_y_arranca(nodo_en_postgres, monkeypatch, capsys):
    monkeypatch.setenv("LIBRAEDGE_TABLAS_ESPEJO", "productos:id,clientes:id,pendiente:id")
    monkeypatch.setenv("LIBRAEDGE_TABLAS_SOLO_LECTURA", "productos,pendiente")
    nodo, conexion = construir_nodo()
    conexion.close()
    assert "pendiente" in capsys.readouterr().err
    assert tablas_con_guarda(nodo_en_postgres) == ("productos",)
