"""El chequeo del producto antes de registrar un nodo: `Aprovisionamiento.validar_registro` (ADR-023).

Decision del humano (2026-10-21): si la instancia tiene depositos sin sucursal, vincular se niega. LibraEdge no
conoce depositos: el producto declara una funcion `(conn) -> list[str]` de problemas y el central, si hay alguno,
contesta 409 con el motivo **antes de escribir nada** (ni nodo, ni prefijo, ni codigo gastado).
"""

import contextlib
import json

import pytest

from libraedge import aprovisionamiento as ap
from libraedge.db.repository import NodeRepository
from libraedge.pantalla_del_central import create_canje_router, create_nodos_router
from tests.test_aprovisionamiento import _crear_producto, _sucursales
from tests.test_pantalla_del_central import CENTRAL, CUIT_FICTICIO

SIN_SUCURSAL = ["El deposito «Galpon» no tiene sucursal.", "El deposito «Fondo» no tiene sucursal."]


class Problemas:
    """Un `validar_registro` de juguete: devuelve lo que el test le diga y anota con que lo llamaron."""

    def __init__(self, problemas=()):
        self.problemas = problemas
        self.llamadas = []

    def __call__(self, conn):
        self.llamadas.append(conn)
        return self.problemas


@pytest.fixture
def producto(conn):
    return _crear_producto(conn, sucursales=(("1", "Centro", 1), ("2", "Norte", 1)))


@pytest.fixture
def config(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"empresa_nombre": "Comercio de Prueba SA", "empresa_cuit": CUIT_FICTICIO,
                                "empresa_iva_condition": "RI"}), encoding="utf-8")
    return ruta


def _aprov(validar_registro=None):
    return ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias", "productos"],
                                listar_sucursales=_sucursales, validar_registro=validar_registro)


def _nodos(conn):
    return [f[0] for f in conn.execute("SELECT node_id FROM node_identity ORDER BY node_id").fetchall()]


# ── registrar ───────────────────────────────────────────────────────────────


def test_sin_la_costura_se_registra_como_siempre(producto):
    assert _aprov().validar_registro is None
    assert ap.registrar(producto, _aprov(), "nodo-1", "1").node_id == "nodo-1"


@pytest.mark.parametrize("devuelve", [[], (), None])
def test_sin_problemas_se_registra(producto, devuelve):
    validar = Problemas(devuelve)

    assert ap.registrar(producto, _aprov(validar), "nodo-1", "1").node_id == "nodo-1"
    assert validar.llamadas == [producto], "se le pasa la conexion del alta"


def test_con_problemas_registrar_se_niega_y_no_escribe_nada(producto):
    validar = Problemas(SIN_SUCURSAL)

    with pytest.raises(ap.RegistroConProblemas) as error:
        ap.registrar(producto, _aprov(validar), "nodo-1", "1")

    assert isinstance(error.value, ap.ErrorDeAprovisionamiento)  # el CLI y las pantallas ya lo atrapan
    assert error.value.problemas == tuple(SIN_SUCURSAL)
    texto = str(error.value)
    assert all(problema in texto for problema in SIN_SUCURSAL) and "No se puede vincular" in texto
    assert _nodos(producto) == []
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_los_textos_vacios_no_son_problemas(producto):
    assert ap.registrar(producto, _aprov(Problemas(["", "   ", None])), "nodo-1", "1")


def test_se_chequea_tambien_al_re_registrar_una_pc_que_se_reemplaza(producto):
    validar = Problemas()
    ap.registrar(producto, _aprov(validar), "nodo-1", "1")
    validar.problemas = SIN_SUCURSAL
    secreto_viejo = producto.execute("SELECT secret_hash FROM node_identity").fetchone()[0]

    with pytest.raises(ap.RegistroConProblemas):
        ap.registrar(producto, _aprov(validar), "nodo-1", "1")

    assert producto.execute("SELECT secret_hash FROM node_identity").fetchone()[0] == secreto_viejo, \
        "el secreto del nodo que ya andaba no se invalida si el re-registro se niega"


def test_las_otras_guardas_van_antes(producto):
    """El orden es el de siempre (sucursal, segundo nodo, adicional) y recien despues el chequeo del producto."""
    validar = Problemas(SIN_SUCURSAL)
    ap.verificar_registro(producto, _aprov())  # sin costura: no hace nada

    with pytest.raises(ap.SucursalInvalida):
        ap.registrar(producto, _aprov(validar), "nodo-1", "99")
    assert validar.llamadas == []

    NodeRepository(producto).register_node("nodo-viejo", branch_id="1", prefijo="S01")
    with pytest.raises(ap.SegundoNodoEnLaSucursal):
        ap.registrar(producto, _aprov(validar), "nodo-1", "1")
    assert validar.llamadas == []


# ── las pantallas del central ───────────────────────────────────────────────


def _cliente(conn, config, validar, **canje):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    aprov = _aprov(validar)
    abrir = lambda: contextlib.nullcontext(conn)  # noqa: E731
    entorno = {"LIBRAEDGE_CENTRAL_URL": CENTRAL}
    app = FastAPI()
    app.include_router(create_nodos_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno))
    app.include_router(create_canje_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno,
                                           ip_del_request=lambda r: "198.51.100.7", **canje))
    return TestClient(app, base_url="https://central.prueba")


def _es_el_409(respuesta):
    assert respuesta.status_code == 409, respuesta.text
    detalle = respuesta.json()["detail"]
    assert detalle["codigo"] == "registro_con_problemas"
    assert all(problema in detalle["mensaje"] for problema in SIN_SUCURSAL)


def test_el_alta_por_codigo_contesta_409_y_no_emite_nada(producto, config):
    cliente = _cliente(producto, config, Problemas(SIN_SUCURSAL))

    _es_el_409(cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}))

    assert _nodos(producto) == []
    assert producto.execute("SELECT COUNT(*) FROM libraedge_vinculacion").fetchone()[0] == 0


def test_el_alta_por_codigo_anda_cuando_no_hay_problemas(producto, config):
    cliente = _cliente(producto, config, Problemas())
    respuesta = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"})
    assert respuesta.status_code == 200 and respuesta.json()["codigo"]


def test_el_alta_por_archivo_contesta_409(producto, config):
    cliente = _cliente(producto, config, Problemas(SIN_SUCURSAL))

    _es_el_409(cliente.post("/api/nodos-offline", json={"sucursal_id": "1"}))

    assert _nodos(producto) == []


def test_otra_carga_para_un_nodo_contesta_409_y_conserva_su_secreto(producto, config):
    validar = Problemas()
    cliente = _cliente(producto, config, validar)
    datos = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()
    secreto = producto.execute("SELECT secret_hash FROM node_identity").fetchone()[0]
    validar.problemas = SIN_SUCURSAL

    _es_el_409(cliente.post(f"/api/nodos-offline/{datos['node_id']}/carga"))

    assert producto.execute("SELECT secret_hash FROM node_identity").fetchone()[0] == secreto


def test_otro_codigo_para_un_nodo_contesta_409_en_vez_de_emitir_uno_que_el_canje_va_a_rechazar(producto, config):
    validar = Problemas()
    cliente = _cliente(producto, config, validar)
    datos = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()
    validar.problemas = SIN_SUCURSAL

    _es_el_409(cliente.post(f"/api/nodos-offline/{datos['node_id']}/vinculacion"))

    validar.problemas = []
    assert cliente.post(f"/api/nodos-offline/{datos['node_id']}/vinculacion").status_code == 200


def test_el_canje_contesta_409_y_no_gasta_el_codigo(producto, config):
    validar = Problemas()
    cliente = _cliente(producto, config, validar)
    codigo = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()["codigo"]
    validar.problemas = SIN_SUCURSAL

    _es_el_409(cliente.post("/sync/v1/vinculacion", json={"codigo": codigo, "producto": "juguete"}))

    validar.problemas = []
    canje = cliente.post("/sync/v1/vinculacion", json={"codigo": codigo, "producto": "juguete"})
    assert canje.status_code == 200, "el codigo sigue sirviendo cuando el sistema esta en orden"
    assert canje.json()["entorno"]["LIBRAEDGE_NODE_SECRET"]
