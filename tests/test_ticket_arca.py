"""El ticket de ARCA prestado por el central (ADR-009).

Tickets y certificados ficticios: ningún test sale a ARCA ni a la red. El
cliente del nodo se prueba contra el router de verdad, cambiando ``urlopen`` por
uno que le habla al ``TestClient``.
"""

import contextlib
import io
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.sync import esquema
from libraedge.sync import ticket as modulo
from libraedge.sync.api import create_sync_router
from libraedge.sync.ticket import ClienteDeTicket, SinConexionConElCentral, TicketRechazado

TOKEN = "TOKEN-FICTICIO-NO-ES-DE-ARCA"
SIGN = "SIGN-FICTICIO-NO-ES-DE-ARCA"
AHORA = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _ticket(emisor_id=1, horas=12):
    vence = AHORA + timedelta(hours=horas)
    return {"token": TOKEN, "sign": SIGN, "expiracion": vence.isoformat(), "ambiente": "homologacion",
            "cuit": "20000000001", "emisor_id": emisor_id}


class NodoSinArca(Exception):
    """Como la de libracore: trae ``detalle()``."""

    codigo = "nodo_sin_arca"

    def detalle(self):
        return {"codigo": self.codigo, "mensaje": str(self)}


@pytest.fixture
def central():
    conexion = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conexion)
    yield conexion
    conexion.close()


def _montar(central, prestar_ticket, version_esquema=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(create_sync_router(
        lambda: contextlib.nullcontext(central), prestar_ticket=prestar_ticket,
        version_esquema=version_esquema))
    secreto = NodeRepository(central).register_node("node-1", branch_id="b1")
    return TestClient(app), secreto


def _pedir(cliente, secreto, emisor_id=1, node_id="node-1", cabeceras=None):
    return cliente.post("/sync/v1/ticket-arca", json={"node_id": node_id, "emisor_id": emisor_id},
                        headers={"Authorization": f"Bearer {secreto}", **(cabeceras or {})})


# ── El central ──────────────────────────────────────────────────────────────


def test_presta_el_ticket_a_un_nodo_activo_y_anota_el_latido(central):
    pedidos = []

    async def prestar(emisor_id):
        pedidos.append(emisor_id)
        return _ticket(emisor_id)

    cliente, secreto = _montar(central, prestar)
    respuesta = _pedir(cliente, secreto, emisor_id=3)

    assert respuesta.status_code == 200
    assert respuesta.json()["token"] == TOKEN
    assert respuesta.headers["cache-control"] == "no-store"
    assert pedidos == [3]
    visto = central.execute("SELECT last_seen_at FROM node_identity WHERE node_id='node-1'").fetchone()[0]
    assert visto, "pedir el ticket es un contacto del nodo"


def test_acepta_una_funcion_sincronica_y_emisor_none(central):
    cliente, secreto = _montar(central, lambda emisor_id: _ticket(emisor_id))
    respuesta = _pedir(cliente, secreto, emisor_id=None)
    assert respuesta.status_code == 200
    assert respuesta.json()["emisor_id"] is None


@pytest.mark.parametrize("caso", ["sin_bearer", "secreto_malo", "revocado"])
def test_solo_nodos_activos_y_autenticados(central, caso):
    pedidos = []
    cliente, secreto = _montar(central, lambda e: pedidos.append(e) or _ticket(e))
    if caso == "sin_bearer":
        respuesta = cliente.post("/sync/v1/ticket-arca", json={"node_id": "node-1", "emisor_id": 1})
    elif caso == "secreto_malo":
        respuesta = _pedir(cliente, "otro-secreto")
    else:
        NodeRepository(central).deactivate_node("node-1")
        respuesta = _pedir(cliente, secreto)
    assert respuesta.status_code == 401
    assert pedidos == [], "sin autenticar no se le pide nada a ARCA"


def test_esquema_distinto_contesta_409(central):
    cliente, secreto = _montar(central, lambda e: _ticket(e), version_esquema="v2")
    respuesta = _pedir(cliente, secreto, cabeceras={esquema.CABECERA: "v1"})
    assert respuesta.status_code == 409
    assert respuesta.json()["detail"]["error"] == "esquema_incompatible"
    assert _pedir(cliente, secreto, cabeceras={esquema.CABECERA: "v2"}).status_code == 200


def test_sin_la_costura_contesta_501_despues_de_autenticar(central):
    cliente, secreto = _montar(central, None)
    assert _pedir(cliente, "otro-secreto").status_code == 401
    respuesta = _pedir(cliente, secreto)
    assert respuesta.status_code == 501
    assert respuesta.json()["detail"]["codigo"] == modulo.CODIGO_SIN_COSTURA


@pytest.mark.parametrize("cuerpo", [{"emisor_id": 1}, {"node_id": "node-1", "emisor_id": "uno"},
                                    {"node_id": "node-1", "emisor_id": True}])
def test_pedido_mal_formado_422(central, cuerpo):
    cliente, secreto = _montar(central, lambda e: _ticket(e))
    respuesta = cliente.post("/sync/v1/ticket-arca", json=cuerpo, headers={"Authorization": f"Bearer {secreto}"})
    assert respuesta.status_code == 422


@pytest.mark.parametrize(("error", "status", "codigo"), [
    (NodoSinArca("El central no tiene la configuración de ARCA de este emisor."), 422, "nodo_sin_arca"),
    (LookupError("no existe"), 422, modulo.CODIGO_NO_DISPONIBLE),
    (RuntimeError(f"ARCA rechazó token={TOKEN}"), 502, modulo.CODIGO_ARCA_NO_RESPONDE),
])
def test_errores_del_producto_sin_filtrar_el_texto(central, caplog, error, status, codigo):
    def prestar(emisor_id):
        raise error

    cliente, secreto = _montar(central, prestar)
    with caplog.at_level(logging.DEBUG):
        respuesta = _pedir(cliente, secreto)
    assert respuesta.status_code == status
    assert respuesta.json()["detail"]["codigo"] == codigo
    assert TOKEN not in respuesta.text
    assert TOKEN not in caplog.text


def test_el_ticket_nunca_va_al_log(central, caplog):
    cliente, secreto = _montar(central, lambda e: _ticket(e))
    with caplog.at_level(logging.DEBUG):
        assert _pedir(cliente, secreto).status_code == 200
    assert TOKEN not in caplog.text and SIGN not in caplog.text


def test_respuesta_sin_token_es_502(central):
    cliente, secreto = _montar(central, lambda e: {"token": "", "sign": SIGN})
    assert _pedir(cliente, secreto).status_code == 502


# ── El nodo ─────────────────────────────────────────────────────────────────


class _Respuesta:
    def __init__(self, cuerpo):
        self._cuerpo = cuerpo

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_contra(test_client, contador):
    """Un ``urlopen`` que le habla al TestClient, con la misma forma de errores que urllib."""

    def urlopen(pedido, timeout=None):
        contador.append(pedido.full_url)
        ruta = pedido.full_url.split("://", 1)[1].split("/", 1)[1]
        respuesta = test_client.post("/" + ruta, content=pedido.data, headers=dict(pedido.header_items()))
        if respuesta.status_code >= 400:
            raise HTTPError(pedido.full_url, respuesta.status_code, "error", {}, io.BytesIO(respuesta.content))
        return _Respuesta(respuesta.content)

    return urlopen


@pytest.fixture
def reloj():
    estado = {"ahora": AHORA}
    estado["fn"] = lambda: estado["ahora"]
    return estado


def _cliente_del_nodo(central, monkeypatch, prestar, reloj, **kwargs):
    test_client, secreto = _montar(central, prestar, version_esquema=kwargs.pop("version_central", None))
    llamadas = []
    monkeypatch.setattr(modulo, "urlopen", _urlopen_contra(test_client, llamadas))
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", secreto, reloj=reloj["fn"], **kwargs)
    return cliente, llamadas


def test_el_nodo_guarda_el_ticket_en_memoria_hasta_el_margen(central, monkeypatch, reloj):
    cliente, llamadas = _cliente_del_nodo(central, monkeypatch, lambda e: _ticket(e, horas=1), reloj)

    assert cliente.pedir_ticket(1)["token"] == TOKEN
    assert cliente.pedir_ticket(1)["token"] == TOKEN
    assert len(llamadas) == 1, "el segundo sale de la caché"

    cliente.pedir_ticket(2)
    assert len(llamadas) == 2, "la caché es por emisor"

    reloj["ahora"] = AHORA + timedelta(minutes=51)  # vence a los 60, margen de 10
    cliente.pedir_ticket(1)
    assert len(llamadas) == 3, "cerca del vencimiento se pide de nuevo"

    cliente.olvidar(1)
    cliente.pedir_ticket(1)
    assert len(llamadas) == 4


def test_ticket_sin_vencimiento_entendible_no_se_guarda(central, monkeypatch, reloj):
    sin_zona = {**_ticket(), "expiracion": "2026-10-19T20:00:00"}
    cliente, llamadas = _cliente_del_nodo(central, monkeypatch, lambda e: sin_zona, reloj)
    cliente.pedir_ticket(1)
    cliente.pedir_ticket(1)
    assert len(llamadas) == 2


def test_sin_red_es_sin_conexion(monkeypatch):
    def urlopen(pedido, timeout=None):
        raise URLError("Name or service not known")

    monkeypatch.setattr(modulo, "urlopen", urlopen)
    with pytest.raises(SinConexionConElCentral) as error:
        ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio").pedir_ticket(1)
    assert error.value.codigo == modulo.CODIGO_SIN_CONEXION


def test_arca_caida_en_el_central_es_sin_conexion(central, monkeypatch, reloj):
    def prestar(emisor_id):
        raise RuntimeError("timeout de WSAA")

    cliente, _ = _cliente_del_nodo(central, monkeypatch, prestar, reloj)
    with pytest.raises(SinConexionConElCentral) as error:
        cliente.pedir_ticket(1)
    assert error.value.status == 502


def test_central_sin_arca_es_rechazo(central, monkeypatch, reloj):
    def prestar(emisor_id):
        raise NodoSinArca("El central no tiene la configuración de ARCA de este emisor.")

    cliente, _ = _cliente_del_nodo(central, monkeypatch, prestar, reloj)
    with pytest.raises(TicketRechazado) as error:
        cliente.pedir_ticket(1)
    assert error.value.codigo == "nodo_sin_arca"
    assert error.value.status == 422


def test_esquema_del_nodo_viejo(central, monkeypatch, reloj):
    cliente, _ = _cliente_del_nodo(central, monkeypatch, lambda e: _ticket(e), reloj,
                                   version_central="v2", version_esquema=lambda: "v1")
    with pytest.raises(esquema.EsquemaIncompatible):
        cliente.pedir_ticket(1)


def test_para_numerador_usa_el_id_de_la_config_y_traduce_sin_conexion(central, monkeypatch, reloj):
    import asyncio

    pedidos = []
    cliente, _ = _cliente_del_nodo(central, monkeypatch, lambda e: pedidos.append(e) or _ticket(e), reloj)

    class SinConexion(Exception):
        pass

    obtener_ta = cliente.para_numerador(sin_conexion=SinConexion)
    ta = asyncio.run(obtener_ta({"id": 7, "cuit": "20000000001", "ambiente": "homologacion"}))
    assert ta["token"] == TOKEN and pedidos == [7]

    def sin_red(pedido, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(modulo, "urlopen", sin_red)
    with pytest.raises(SinConexion):
        asyncio.run(obtener_ta({"id": 8}))


def test_repr_sin_secreto():
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio")
    assert "secreto-ficticio" not in repr(cliente)


def test_cliente_desde_entorno(monkeypatch):
    for variable in ("LIBRAEDGE_CENTRAL_URL", "LIBRAEDGE_NODE_ID", "LIBRAEDGE_NODE_SECRET"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(RuntimeError, match="LIBRAEDGE_NODE_SECRET"):
        modulo.cliente_desde_entorno()
    monkeypatch.setenv("LIBRAEDGE_CENTRAL_URL", "https://central.ejemplo/")
    monkeypatch.setenv("LIBRAEDGE_NODE_ID", "node-1")
    monkeypatch.setenv("LIBRAEDGE_NODE_SECRET", "secreto-ficticio")
    cliente = modulo.cliente_desde_entorno()
    assert cliente.base_url == "https://central.ejemplo" and cliente.node_id == "node-1"
