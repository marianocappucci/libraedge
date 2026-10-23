"""El ticket de ARCA prestado por el central (ADR-009).

Tickets y certificados ficticios: ningún test sale a ARCA ni a la red. El
cliente del nodo se prueba contra el router de verdad, cambiando ``urlopen`` por
uno que le habla al ``TestClient``.
"""

import asyncio
import contextlib
import io
import json
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


# ── El servicio del ticket: wsfe y wscpe (v0.22, ADR-025) ───────────────────


def _ticket_de(servicio, emisor_id=None, horas=12):
    """El ticket de un servicio, como lo arma ``ta_para_el_nodo``: el token dice de cuál es."""
    ticket = {**_ticket(emisor_id, horas), "token": f"{TOKEN}-{servicio}", "servicio": servicio}
    if servicio != "wsfe":  # el CUIT de una CPE es el de su titular; el emisor no importa
        ticket.update(cuit=None, emisor_id=None, ambiente="produccion")
    return ticket


def _prestador_de_dos_servicios(pedidos):
    """El prestador con la firma de ``ta_para_el_nodo``: `servicio` por palabra clave y `ValueError` si no lo presta."""

    async def prestar(emisor_id=None, *, servicio="wsfe"):
        pedidos.append((emisor_id, servicio))
        if servicio not in ("wsfe", "wscpe"):
            raise ValueError(f"El servicio {servicio!r} no se presta a un nodo.")
        return _ticket_de(servicio, emisor_id)

    return prestar


def _pedir_servicio(cliente, secreto, **cuerpo):
    return cliente.post("/sync/v1/ticket-arca", json={"node_id": "node-1", **cuerpo},
                        headers={"Authorization": f"Bearer {secreto}"})


def test_el_central_presta_el_ticket_de_wscpe_con_el_servicio_en_el_pedido(central):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))

    respuesta = _pedir_servicio(cliente, secreto, emisor_id=None, servicio="wscpe")

    assert respuesta.status_code == 200
    assert pedidos == [(None, "wscpe")]
    cuerpo = respuesta.json()
    assert cuerpo["token"] == f"{TOKEN}-wscpe"
    assert cuerpo["servicio"] == "wscpe" and cuerpo["ambiente"] == "produccion"
    assert cuerpo["cuit"] is None and cuerpo["emisor_id"] is None
    assert respuesta.headers["cache-control"] == "no-store"


def test_el_central_presta_wsfe_si_se_lo_pide_y_por_omision(central):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))

    explicito = _pedir_servicio(cliente, secreto, emisor_id=3, servicio="wsfe")
    sin_servicio = _pedir_servicio(cliente, secreto, emisor_id=3)  # el pedido de un nodo de antes de v0.22
    nulo = _pedir_servicio(cliente, secreto, emisor_id=3, servicio=None)

    assert (explicito.status_code, sin_servicio.status_code, nulo.status_code) == (200, 200, 200)
    assert pedidos == [(3, "wsfe")] * 3, "sin servicio es wsfe, como siempre"
    assert explicito.json()["servicio"] == sin_servicio.json()["servicio"] == "wsfe"


def test_el_servicio_se_normaliza_a_minusculas(central):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))
    assert _pedir_servicio(cliente, secreto, servicio=" WSCPE ").status_code == 200
    assert pedidos == [(None, "wscpe")]


@pytest.mark.parametrize("servicio", ["", "   ", 7, True, ["wscpe"], "ws cpe", "wscpe;x", "../wsfe", "x" * 40])
def test_un_servicio_mal_formado_es_422_y_no_llega_al_prestador(central, servicio):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))
    respuesta = _pedir_servicio(cliente, secreto, servicio=servicio)
    assert respuesta.status_code == 422
    assert pedidos == []


def test_un_servicio_que_el_prestador_no_presta_es_501_ticket_no_prestado(central, caplog):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))
    with caplog.at_level(logging.DEBUG):
        respuesta = _pedir_servicio(cliente, secreto, servicio="wsfex")
    assert respuesta.status_code == 501
    assert respuesta.json()["detail"]["codigo"] == modulo.CODIGO_SIN_COSTURA == "ticket_no_prestado"
    assert pedidos == [(None, "wsfex")], "lo decide el prestador, no el router"
    # Del ValueError se loguea el tipo, no su texto (una excepcion cualquiera podria traer el ticket).
    assert "no se presta a un nodo" not in respuesta.text and "no se presta a un nodo" not in caplog.text


def test_el_prestador_real_del_motor_contesta_501_con_un_servicio_que_no_presta(central):
    """El cableado que el contrato de ADR-079 le pide al producto: ``ta_para_el_nodo`` con su ``empresa=``."""
    from libracore.nodo_offline import ta_para_el_nodo

    def prestar(emisor_id, servicio="wsfe"):
        return ta_para_el_nodo(emisor_id, servicio=servicio, empresa="agencia")

    cliente, secreto = _montar(central, prestar)
    respuesta = _pedir_servicio(cliente, secreto, servicio="wsfex")
    assert respuesta.status_code == 501
    assert respuesta.json()["detail"]["codigo"] == "ticket_no_prestado"


def test_un_value_error_pidiendo_wsfe_sigue_siendo_422(central):
    """El mapeo de ADR-009 no cambia: ``ValueError`` con el servicio de siempre es un emisor que no existe."""

    def prestar(emisor_id, servicio="wsfe"):
        raise ValueError("emisor 99 desconocido")

    cliente, secreto = _montar(central, prestar)
    respuesta = _pedir_servicio(cliente, secreto, emisor_id=99)
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"]["codigo"] == modulo.CODIGO_NO_DISPONIBLE


def test_nodo_sin_arca_de_wscpe_sigue_siendo_422(central):
    def prestar(emisor_id, servicio="wsfe"):
        raise NodoSinArca("El central no tiene el certificado y la clave de wscpe cargados.")

    cliente, secreto = _montar(central, prestar)
    respuesta = _pedir_servicio(cliente, secreto, servicio="wscpe")
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"]["codigo"] == "nodo_sin_arca"


def test_un_prestador_de_antes_de_v022_sirve_wsfe_como_antes_y_no_presta_otro_servicio(central):
    llamadas = []

    def viejo(emisor_id):  # la firma de v0.9 a v0.21: sin `servicio`
        llamadas.append(emisor_id)
        return _ticket(emisor_id)

    cliente, secreto = _montar(central, viejo)

    for cuerpo in ({"emisor_id": 4}, {"emisor_id": 4, "servicio": "wsfe"}):
        respuesta = _pedir_servicio(cliente, secreto, **cuerpo)
        assert respuesta.status_code == 200
        assert respuesta.json()["token"] == TOKEN and respuesta.json()["servicio"] == "wsfe"
    assert llamadas == [4, 4]

    pedido_wscpe = _pedir_servicio(cliente, secreto, emisor_id=None, servicio="wscpe")
    assert pedido_wscpe.status_code == 501
    assert pedido_wscpe.json()["detail"]["codigo"] == "ticket_no_prestado"
    assert llamadas == [4, 4], "al prestador viejo no se le pide wscpe: daria el ticket de wsfe con otro nombre"


@pytest.mark.parametrize("firma", ["kwargs", "palabra_clave", "posicional"])
def test_el_servicio_llega_al_prestador_con_cualquier_firma_que_lo_acepte(central, firma):
    recibido = []
    if firma == "kwargs":
        def prestar(emisor_id, **kwargs):
            recibido.append(kwargs)
            return _ticket_de("wscpe")
    elif firma == "palabra_clave":
        def prestar(emisor_id, *, servicio):
            recibido.append({"servicio": servicio})
            return _ticket_de("wscpe")
    else:
        def prestar(emisor_id, servicio="wsfe"):
            recibido.append({"servicio": servicio})
            return _ticket_de("wscpe")

    cliente, secreto = _montar(central, prestar)
    assert _pedir_servicio(cliente, secreto, servicio="wscpe").status_code == 200
    assert recibido == [{"servicio": "wscpe"}]


def test_un_ticket_de_otro_servicio_no_se_presta(central, caplog):
    """El prestador devuelve el ticket de ``wsfe`` a quien pidió ``wscpe``: es un 502, nunca el ticket equivocado."""
    cliente, secreto = _montar(central, lambda emisor_id, servicio="wsfe": _ticket_de("wsfe"))
    with caplog.at_level(logging.DEBUG):
        respuesta = _pedir_servicio(cliente, secreto, servicio="wscpe")
    assert respuesta.status_code == 502
    assert respuesta.json()["detail"]["codigo"] == modulo.CODIGO_OTRO_SERVICIO
    assert TOKEN not in respuesta.text and TOKEN not in caplog.text


def test_un_ticket_de_wscpe_que_no_dice_su_servicio_no_se_presta(central):
    sin_servicio = {k: v for k, v in _ticket_de("wscpe").items() if k != "servicio"}
    cliente, secreto = _montar(central, lambda emisor_id, servicio="wsfe": sin_servicio)
    assert _pedir_servicio(cliente, secreto, servicio="wscpe").status_code == 502


def test_el_ticket_de_wsfe_sin_servicio_sale_diciendo_que_es_de_wsfe(central):
    cliente, secreto = _montar(central, lambda emisor_id, servicio="wsfe": _ticket(emisor_id))
    assert _pedir_servicio(cliente, secreto, emisor_id=1).json()["servicio"] == "wsfe"


def test_wscpe_pasa_por_el_mismo_gate_que_wsfe(central):
    pedidos = []
    cliente, secreto = _montar(central, _prestador_de_dos_servicios(pedidos))
    sin_bearer = cliente.post("/sync/v1/ticket-arca", json={"node_id": "node-1", "servicio": "wscpe"})
    malo = _pedir_servicio(cliente, "otro-secreto", servicio="wscpe")
    NodeRepository(central).deactivate_node("node-1")
    revocado = _pedir_servicio(cliente, secreto, servicio="wscpe")
    assert (sin_bearer.status_code, malo.status_code, revocado.status_code) == (401, 401, 401)
    assert pedidos == []


# ── El nodo: la caché por (emisor, servicio) ────────────────────────────────


def _cliente_con_dos_servicios(central, monkeypatch, reloj, pedidos=None, **kwargs):
    pedidos = [] if pedidos is None else pedidos
    cliente, llamadas = _cliente_del_nodo(central, monkeypatch, _prestador_de_dos_servicios(pedidos), reloj, **kwargs)
    return cliente, llamadas, pedidos


def test_la_cache_es_por_emisor_y_servicio(central, monkeypatch, reloj):
    """Si la caché fuera por emisor, el que pide ``wscpe`` recibiría el ticket de ``wsfe`` que ya estaba guardado."""
    cliente, llamadas, pedidos = _cliente_con_dos_servicios(central, monkeypatch, reloj)

    fiscal = cliente.pedir_ticket(None)  # wsfe, el emisor por defecto
    cpe = cliente.pedir_ticket(None, "wscpe")

    assert fiscal["token"] == f"{TOKEN}-wsfe" and fiscal["servicio"] == "wsfe"
    assert cpe["token"] == f"{TOKEN}-wscpe" and cpe["servicio"] == "wscpe"
    assert pedidos == [(None, "wsfe"), (None, "wscpe")]

    # Cada uno sale de su caché, sin volver al central.
    assert cliente.pedir_ticket(None)["token"] == f"{TOKEN}-wsfe"
    assert cliente.pedir_ticket(None, "wscpe")["token"] == f"{TOKEN}-wscpe"
    assert len(llamadas) == 2

    # Y otro emisor de wsfe es otro ticket.
    cliente.pedir_ticket(2)
    assert len(llamadas) == 3


def test_olvidar_descarta_solo_el_servicio_que_se_dice(central, monkeypatch, reloj):
    cliente, llamadas, _ = _cliente_con_dos_servicios(central, monkeypatch, reloj)
    cliente.pedir_ticket(None)
    cliente.pedir_ticket(None, "wscpe")

    cliente.olvidar(None, "wscpe")
    assert cliente.pedir_ticket(None)["servicio"] == "wsfe"
    assert len(llamadas) == 2, "el de wsfe seguia guardado"
    assert cliente.pedir_ticket(None, "wscpe")["servicio"] == "wscpe"
    assert len(llamadas) == 3, "el de wscpe se pidio de nuevo"

    cliente.olvidar(None)  # sin servicio es wsfe, como siempre
    cliente.pedir_ticket(None)
    cliente.pedir_ticket(None, "wscpe")
    assert len(llamadas) == 4, "solo se volvio a pedir el de wsfe"


def test_cada_servicio_vence_por_su_cuenta(central, monkeypatch, reloj):
    def prestar(emisor_id=None, *, servicio="wsfe"):
        return _ticket_de(servicio, emisor_id, horas=1 if servicio == "wscpe" else 12)

    cliente, llamadas = _cliente_del_nodo(central, monkeypatch, prestar, reloj)
    cliente.pedir_ticket(None)
    cliente.pedir_ticket(None, "wscpe")
    reloj["ahora"] = AHORA + timedelta(minutes=51)  # el de wscpe vence a los 60 menos el margen; el de wsfe, no
    cliente.pedir_ticket(None)
    assert len(llamadas) == 2
    cliente.pedir_ticket(None, "wscpe")
    assert len(llamadas) == 3


def test_el_pedido_del_nodo_dice_el_servicio(central, monkeypatch, reloj):
    cuerpos = []
    prestador = _prestador_de_dos_servicios([])
    test_client, secreto = _montar(central, prestador)

    def urlopen(pedido, timeout=None):
        cuerpos.append(json.loads(pedido.data))
        return _urlopen_contra(test_client, [])(pedido, timeout)

    monkeypatch.setattr(modulo, "urlopen", urlopen)
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", secreto, reloj=reloj["fn"])
    cliente.pedir_ticket(5)
    cliente.pedir_ticket(None, "wscpe")
    assert cuerpos == [{"node_id": "node-1", "emisor_id": 5, "servicio": "wsfe"},
                       {"node_id": "node-1", "emisor_id": None, "servicio": "wscpe"}]


@pytest.mark.parametrize("servicio", ["", "ws cpe", 7])
def test_el_nodo_no_manda_un_servicio_mal_formado(servicio):
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio")
    with pytest.raises(ValueError, match="servicio"):
        cliente.pedir_ticket(None, servicio)
    with pytest.raises(ValueError, match="servicio"):
        cliente.olvidar(None, servicio)


def test_un_central_anterior_que_ignora_el_servicio_no_engana_al_nodo(monkeypatch, reloj):
    """Un central de antes de v0.22 contesta con el ticket de ``wsfe`` a cualquier pedido: para wscpe es un rechazo y
    no se guarda (si se guardara, se serviría el equivocado hasta que venza)."""
    pedidos = []

    def urlopen(pedido, timeout=None):
        pedidos.append(json.loads(pedido.data)["servicio"])
        return _Respuesta(json.dumps(_ticket(None)).encode())  # sin `servicio`: es de wsfe

    monkeypatch.setattr(modulo, "urlopen", urlopen)
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio", reloj=reloj["fn"])

    assert cliente.pedir_ticket(None)["token"] == TOKEN  # wsfe: sin cambios
    for _ in range(2):
        with pytest.raises(TicketRechazado) as error:
            cliente.pedir_ticket(None, "wscpe")
        assert error.value.codigo == modulo.CODIGO_OTRO_SERVICIO
        assert TOKEN not in str(error.value)
    assert pedidos == ["wsfe", "wscpe", "wscpe"], "el rechazado no se guardo"


def test_un_servicio_que_el_central_no_presta_es_un_rechazo_501(central, monkeypatch, reloj):
    cliente, _, _ = _cliente_con_dos_servicios(central, monkeypatch, reloj)
    with pytest.raises(TicketRechazado) as error:
        cliente.pedir_ticket(None, "wsfex")
    assert error.value.status == 501 and error.value.codigo == "ticket_no_prestado"


def test_las_traducciones_de_errores_siguen_igual_con_wscpe(central, monkeypatch, reloj):
    class SinConexion(Exception):
        pass

    # Sin red: SinConexionConElCentral -> la SinConexion del producto.
    def sin_red(pedido, timeout=None):
        raise URLError("Name or service not known")

    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio")
    monkeypatch.setattr(modulo, "urlopen", sin_red)
    obtener_ta = cliente.para_servicio(sin_conexion=SinConexion)
    with pytest.raises(SinConexion):
        asyncio.run(obtener_ta({"id": None}, servicio="wscpe"))
    with pytest.raises(SinConexionConElCentral):
        asyncio.run(cliente.para_servicio()({"id": None}, servicio="wscpe"))

    # El central contesta que no presta: TicketRechazado, que el motor lee como rechazo.
    def prestar(emisor_id, servicio="wsfe"):
        raise NodoSinArca("El central no tiene el certificado y la clave de wscpe cargados.")

    cliente, _ = _cliente_del_nodo(central, monkeypatch, prestar, reloj)
    with pytest.raises(TicketRechazado) as error:
        asyncio.run(cliente.para_servicio(sin_conexion=SinConexion)({"id": None}, servicio="wscpe"))
    assert error.value.codigo == "nodo_sin_arca" and error.value.rechazo_del_central is True


def test_para_servicio_con_rechazo_levanta_la_del_producto_con_el_mensaje_del_central(central, monkeypatch, reloj):
    class Rechazo(Exception):
        pass

    def prestar(emisor_id, servicio="wsfe"):
        raise NodoSinArca("El central no tiene el certificado y la clave de wscpe cargados.")

    cliente, _ = _cliente_del_nodo(central, monkeypatch, prestar, reloj)
    with pytest.raises(Rechazo, match="clave de wscpe cargados") as error:
        asyncio.run(cliente.para_servicio(rechazo=Rechazo)({"id": None}, servicio="wscpe"))
    assert isinstance(error.value.__cause__, TicketRechazado)


def test_para_servicio_sin_servicio_pide_wsfe_y_para_numerador_es_lo_mismo(central, monkeypatch, reloj):
    cliente, _, pedidos = _cliente_con_dos_servicios(central, monkeypatch, reloj)

    ta = asyncio.run(cliente.para_servicio()({"id": 7}))
    assert ta["servicio"] == "wsfe" and pedidos == [(7, "wsfe")]

    ta = asyncio.run(cliente.para_numerador()({"id": 8}))
    assert ta["servicio"] == "wsfe" and pedidos == [(7, "wsfe"), (8, "wsfe")]

    ta = asyncio.run(cliente.para_servicio()({"id": None}, servicio="wscpe"))
    assert ta["servicio"] == "wscpe" and pedidos[-1] == (None, "wscpe")


# ── Con el motor: lo que ticket_prestado y ColaDeArca esperan de obtener_ta ──


def _sin_arca_config(monkeypatch):
    from libracore.db import arca_config

    monkeypatch.setattr(arca_config, "config_del_emisor", lambda emisor_id: None)


def test_ticket_prestado_del_motor_pide_el_ticket_de_wscpe_por_para_servicio(central, monkeypatch, reloj):
    from libracore.nodo_offline import SinConexion, ticket_prestado

    _sin_arca_config(monkeypatch)
    cliente, _, pedidos = _cliente_con_dos_servicios(central, monkeypatch, reloj)

    ta, arca = asyncio.run(ticket_prestado(cliente.para_servicio(sin_conexion=SinConexion), None, servicio="wscpe")())

    assert ta["servicio"] == "wscpe" and ta["ambiente"] == "produccion" and ta["token"] == f"{TOKEN}-wscpe"
    assert arca == {"id": None}, "wscpe no tiene arca_config: al central se le dice solo de quien es"
    assert pedidos == [(None, "wscpe")]


def test_ticket_prestado_del_motor_clasifica_los_errores_del_cliente(central, monkeypatch, reloj):
    from libracore.nodo_offline import RechazoDelCentral, SinConexion, ticket_prestado

    _sin_arca_config(monkeypatch)

    # Un servicio que el central no presta: el cliente levanta TicketRechazado y el motor lo lee como rechazo.
    cliente, _, _ = _cliente_con_dos_servicios(central, monkeypatch, reloj)
    with pytest.raises(RechazoDelCentral):
        asyncio.run(ticket_prestado(cliente.para_servicio(sin_conexion=SinConexion), None, servicio="wsfex")())
    # Con `rechazo=`, el motivo que queda es el del central y no el genérico del motor.
    with pytest.raises(RechazoDelCentral, match="no presta el ticket de ARCA del servicio wsfex"):
        asyncio.run(ticket_prestado(
            cliente.para_servicio(sin_conexion=SinConexion, rechazo=RechazoDelCentral), None, servicio="wsfex")())

    # Sin red: es un corte, SinConexion del motor.
    def sin_red(pedido, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(modulo, "urlopen", sin_red)
    sin_central = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio")
    with pytest.raises(SinConexion):
        asyncio.run(ticket_prestado(sin_central.para_servicio(sin_conexion=SinConexion), None, servicio="wscpe")())


def test_un_central_anterior_hace_que_el_motor_rechace_wscpe_sin_llamar_a_arca(monkeypatch, reloj):
    """Doble cinturón: aunque el cliente no lo detectara, ``ticket_prestado`` comprueba el servicio del ticket."""
    from libracore.nodo_offline import RechazoDelCentral, SinConexion, ticket_prestado

    _sin_arca_config(monkeypatch)
    monkeypatch.setattr(modulo, "urlopen", lambda pedido, timeout=None: _Respuesta(json.dumps(_ticket(None)).encode()))
    cliente = ClienteDeTicket("https://central.ejemplo", "node-1", "secreto-ficticio", reloj=reloj["fn"])
    with pytest.raises(RechazoDelCentral):
        asyncio.run(ticket_prestado(cliente.para_servicio(sin_conexion=SinConexion), None, servicio="wscpe")())
