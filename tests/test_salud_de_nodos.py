"""La salud de los nodos para el monitor «HTTP + keyword» de uptime-kuma (v0.17, ADR-018).

Contra los dos motores (fixture `conn`). Las marcas de `last_seen_at` se escriben a mano: el reloj no se mockea, se
mueve el dato.
"""

import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.sync.api import (
    PALABRA_ALERTA,
    PALABRA_OK,
    VARIABLE_TOKEN_MONITOREO,
    create_sync_router,
    salud_de_todos,
)

TOKEN = "token-de-monitoreo-de-la-instancia-0123456789"


@pytest.fixture
def central(conn):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(create_sync_router(lambda: contextlib.nullcontext(conn), operation_handler=lambda c, o: None))
    return TestClient(app)


@pytest.fixture
def con_token(monkeypatch):
    monkeypatch.setenv(VARIABLE_TOKEN_MONITOREO, TOKEN)


def _visto_hace(conn, node_id, minutos):
    marca = (datetime.now(UTC) - timedelta(minutes=minutos)).isoformat()
    conn.execute("UPDATE node_identity SET last_seen_at = ? WHERE node_id = ?", (marca, node_id))
    conn.commit()


def _nodo(conn, node_id, minutos=None, sucursal="1"):
    secreto = NodeRepository(conn).register_node(node_id, branch_id=sucursal)
    if minutos is not None:
        _visto_hace(conn, node_id, minutos)
    return secreto


BEARER = {"Authorization": f"Bearer {TOKEN}"}


# ── apagada y autenticacion ─────────────────────────────────────────────────

@pytest.mark.parametrize("ruta", ["/sync/v1/salud", "/sync/v1/salud/node-1"])
def test_sin_la_variable_las_rutas_no_existen(central, conn, monkeypatch, ruta):
    _nodo(conn, "node-1", 1)
    monkeypatch.delenv(VARIABLE_TOKEN_MONITOREO, raising=False)
    for pedido in (central.get(ruta), central.get(ruta, headers=BEARER), central.get(ruta + "?token=" + TOKEN),
                   central.get(ruta + "?umbral=abc")):
        assert pedido.status_code == 404, "apagada: ni 401 (diria que hay monitoreo) ni 422 (diria que existe)"
        assert pedido.json() == {"detail": "Not Found"}


def test_con_la_variable_vacia_tambien_esta_apagada(central, conn, monkeypatch):
    monkeypatch.setenv(VARIABLE_TOKEN_MONITOREO, "   ")
    assert central.get("/sync/v1/salud", headers={"Authorization": "Bearer "}).status_code == 404


@pytest.mark.parametrize("ruta", ["/sync/v1/salud", "/sync/v1/salud/node-1"])
@pytest.mark.parametrize("cabeceras, query", [
    ({}, ""),                                              # sin token
    ({"Authorization": "Bearer otro"}, ""),                # token equivocado
    ({"Authorization": "Basic " + TOKEN}, ""),             # esquema que no es Bearer
    ({}, "?token=otro"),
    ({}, "?token=ñandú"),                                  # no ASCII: compare_digest sobre str levantaria TypeError
    ({"Authorization": "Bearer otro"}, "?token=" + TOKEN),  # la cabecera manda sobre la query
])
def test_sin_el_token_correcto_es_401(central, conn, con_token, ruta, cabeceras, query):
    _nodo(conn, "node-1", 1)
    respuesta = central.get(ruta + query, headers=cabeceras)
    assert respuesta.status_code == 401
    assert TOKEN not in respuesta.text


def test_el_secreto_de_un_nodo_no_abre_la_salud(central, conn, con_token):
    secreto = _nodo(conn, "node-1", 1)
    assert central.get("/sync/v1/salud", headers={"Authorization": f"Bearer {secreto}"}).status_code == 401


def test_el_token_va_por_cabecera_o_por_query(central, conn, con_token):
    _nodo(conn, "node-1", 1)
    assert central.get("/sync/v1/salud/node-1", headers=BEARER).status_code == 200
    assert central.get("/sync/v1/salud/node-1", params={"token": TOKEN}).status_code == 200
    assert central.get("/sync/v1/salud", params={"token": TOKEN}).status_code == 200


# ── un nodo ─────────────────────────────────────────────────────────────────

def test_un_nodo_al_dia_es_200_con_la_palabra_clave(central, conn, con_token):
    _nodo(conn, "node-1", 2, sucursal="7")
    respuesta = central.get("/sync/v1/salud/node-1", headers=BEARER)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "al_dia" and cuerpo["palabra_clave"] == PALABRA_OK
    assert 1.5 < cuerpo["minutos_sin_verse"] < 3 and cuerpo["ultima_sincronizacion"]
    assert cuerpo["umbral_minutos"] == 15 and cuerpo["node_id"] == "node-1" and cuerpo["sucursal_id"] == "7"
    assert PALABRA_ALERTA not in respuesta.text
    assert respuesta.headers["cache-control"] == "no-store"


def test_un_nodo_sin_noticias_es_503(central, conn, con_token):
    _nodo(conn, "node-1", 60)
    respuesta = central.get("/sync/v1/salud/node-1", headers=BEARER)

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "sin_noticias" and cuerpo["palabra_clave"] == PALABRA_ALERTA
    assert 59 < cuerpo["minutos_sin_verse"] < 61
    assert PALABRA_OK not in respuesta.text, "la palabra de OK no puede aparecer en una alerta"


def test_un_nodo_que_nunca_hablo_es_503_y_no_inventa_minutos(central, conn, con_token):
    _nodo(conn, "node-1")
    respuesta = central.get("/sync/v1/salud/node-1", headers=BEARER)

    assert respuesta.status_code == 503
    assert respuesta.json()["estado"] == "nunca"
    assert respuesta.json()["minutos_sin_verse"] is None and respuesta.json()["ultima_sincronizacion"] is None


def test_el_umbral_se_cambia_por_query(central, conn, con_token):
    _nodo(conn, "node-1", 60)
    assert central.get("/sync/v1/salud/node-1", headers=BEARER).status_code == 503
    amplio = central.get("/sync/v1/salud/node-1", headers=BEARER, params={"umbral": 120})
    assert amplio.status_code == 200 and amplio.json()["umbral_minutos"] == 120
    estrecho = central.get("/sync/v1/salud/node-1", headers=BEARER, params={"umbral": 30})
    assert estrecho.status_code == 503


@pytest.mark.parametrize("umbral", ["abc", "0", "-5", "1.5", str(7 * 24 * 60 + 1)])
def test_un_umbral_invalido_es_422(central, conn, con_token, umbral):
    _nodo(conn, "node-1", 1)
    for ruta in ("/sync/v1/salud", "/sync/v1/salud/node-1"):
        assert central.get(ruta, headers=BEARER, params={"umbral": umbral}).status_code == 422


def test_un_nodo_dado_de_baja_o_inexistente_es_404(central, conn, con_token):
    _nodo(conn, "node-1", 1)
    _nodo(conn, "node-2", 1)
    NodeRepository(conn).deactivate_node("node-2")

    assert central.get("/sync/v1/salud/node-2", headers=BEARER).status_code == 404
    assert central.get("/sync/v1/salud/no-existe", headers=BEARER).status_code == 404
    assert central.get("/sync/v1/salud/node-1", headers=BEARER).status_code == 200


# ── el resumen ──────────────────────────────────────────────────────────────

def test_el_resumen_es_el_del_peor_nodo(central, conn, con_token):
    _nodo(conn, "node-1", 1, sucursal="1")
    _nodo(conn, "node-2", 90, sucursal="2")
    _nodo(conn, "node-3", 5, sucursal="3")

    respuesta = central.get("/sync/v1/salud", headers=BEARER)

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "sin_noticias" and cuerpo["palabra_clave"] == PALABRA_ALERTA
    assert 89 < cuerpo["minutos_sin_verse"] < 91, "los minutos son los del nodo que explica el estado"
    assert cuerpo["nodos_vigilados"] == 3
    assert {n["node_id"]: n["estado"] for n in cuerpo["nodos"]} == {
        "node-1": "al_dia", "node-2": "sin_noticias", "node-3": "al_dia"}


def test_el_resumen_al_dia_es_200(central, conn, con_token):
    _nodo(conn, "node-1", 1)
    _nodo(conn, "node-2", 14)
    respuesta = central.get("/sync/v1/salud", headers=BEARER)
    assert respuesta.status_code == 200 and respuesta.json()["estado"] == "al_dia"
    assert PALABRA_OK in respuesta.text and PALABRA_ALERTA not in respuesta.text


def test_el_resumen_no_vigila_las_bajas_igual_que_vigilar(central, conn, con_token):
    _nodo(conn, "node-1", 1)
    _nodo(conn, "node-viejo", 600)
    NodeRepository(conn).deactivate_node("node-viejo")
    respuesta = central.get("/sync/v1/salud", headers=BEARER)
    assert respuesta.status_code == 200
    assert [n["node_id"] for n in respuesta.json()["nodos"]] == ["node-1"]


def test_sin_nodos_activos_es_200_como_vigilar(central, conn, con_token):
    assert central.get("/sync/v1/salud", headers=BEARER).json()["estado"] == "sin_nodos"
    _nodo(conn, "node-1", 600)
    NodeRepository(conn).deactivate_node("node-1")
    respuesta = central.get("/sync/v1/salud", headers=BEARER)
    assert respuesta.status_code == 200 and respuesta.json()["nodos_vigilados"] == 0


def test_nunca_pesa_menos_que_sin_noticias_en_el_resumen(central, conn, con_token):
    _nodo(conn, "node-1")            # nunca hablo
    _nodo(conn, "node-2", 40)        # hablaba y se callo
    assert central.get("/sync/v1/salud", headers=BEARER).json()["estado"] == "sin_noticias"
    assert salud_de_todos([], 15)[1]["estado"] == "sin_nodos"


# ── lo que no tiene que pasar ───────────────────────────────────────────────

def test_la_salud_no_expone_secretos_ni_cuenta_como_contacto(central, conn, con_token):
    secreto = _nodo(conn, "node-1", 60)
    antes = conn.execute("SELECT last_seen_at FROM node_identity").fetchone()[0]

    textos = [central.get(ruta, headers=BEARER).text for ruta in ("/sync/v1/salud", "/sync/v1/salud/node-1")]

    hash_ = conn.execute("SELECT secret_hash FROM node_identity").fetchone()[0]
    for texto in textos:
        assert secreto not in texto and hash_ not in texto and TOKEN not in texto
        assert "secret" not in texto and "prefijo" not in texto
    # Mirar la salud no es una senal de vida del nodo: no mueve `last_seen_at`.
    assert conn.execute("SELECT last_seen_at FROM node_identity").fetchone()[0] == antes
