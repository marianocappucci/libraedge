"""El router para montar dentro del producto.

Fase 5. `create_sync_app` sirve para un proceso dedicado; el central de la
familia es una app FastAPI que ya existe, y meterle una conexion fija adentro
seria un defecto, no una simplificacion.
"""

import contextlib
import sqlite3

import pytest

from libraedge.db.changelog import init_changelog_schema, sembrar
from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.sync.api import create_sync_router


def _payload(node_id="node-1", sequence=1):
    return {
        "operation_id": f"{node_id}:{sequence}", "node_id": node_id,
        "sequence": sequence, "operation_type": "pedido.cobrado",
        "aggregate_type": "venta", "aggregate_id": f"{node_id}:venta:{sequence}",
        "occurred_at": "2026-08-30T10:00:00Z", "schema_version": 1,
        "payload": {"total": "16000.00"},
    }


@pytest.fixture
def central():
    """Un central como el del producto: una base y una fabrica de conexiones."""
    conexion = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conexion)
    init_changelog_schema(conexion)
    conexion.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " nombre TEXT NOT NULL, precio NUMERIC)"
    )
    conexion.commit()
    yield conexion
    conexion.close()


def _montar(central, handler=None):
    """Monta el router en una app aparte, como lo haria el producto.

    La fabrica cuenta cuantas veces se la llamo: es lo que distingue este router
    de `create_sync_app`.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    aperturas = []

    def abrir_conexion():
        aperturas.append(1)
        return contextlib.nullcontext(central)

    app = FastAPI()
    app.include_router(create_sync_router(abrir_conexion, operation_handler=handler))
    secreto = NodeRepository(central).register_node("node-1", branch_id="b1")
    return TestClient(app), secreto, aperturas


def test_el_router_abre_una_conexion_POR_REQUEST(central):
    """🔴 La razon de existir de esta variante.

    `create_sync_app` recibe la conexion **al construirse** y la usa para toda la
    vida del proceso. Dentro de un servidor web eso es una conexion compartida
    entre requests concurrentes, y ademas una que envejece: si la base se cae, la
    app queda con una conexion muerta hasta que alguien la reinicie.
    """
    cliente, secreto, aperturas = _montar(central)
    cabeceras = {"Authorization": f"Bearer {secreto}"}

    cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras)
    cliente.post("/sync/v1/push", json=_payload(sequence=2), headers=cabeceras)
    cliente.get("/sync/v1/pull", params={"node_id": "node-1"}, headers=cabeceras)

    assert len(aperturas) == 3, "cada request tiene que abrir la suya"


def test_el_router_acepta_un_push_y_llama_al_handler_del_producto(central):
    aplicadas = []
    cliente, secreto, _ = _montar(central, handler=aplicadas.append)

    respuesta = cliente.post(
        "/sync/v1/push", json=_payload(),
        headers={"Authorization": f"Bearer {secreto}"},
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["result"] == "accepted"
    assert len(aplicadas) == 1
    assert aplicadas[0].payload["total"] == "16000.00"


def test_el_router_deduplica_igual_que_la_app(central):
    aplicadas = []
    cliente, secreto, _ = _montar(central, handler=aplicadas.append)
    cabeceras = {"Authorization": f"Bearer {secreto}"}

    primera = cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras)
    segunda = cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras)

    assert primera.json()["result"] == "accepted"
    assert segunda.json()["result"] == "duplicate"
    assert len(aplicadas) == 1, "el handler del producto corre una sola vez"


def test_el_router_sirve_la_bajada(central):
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("Mila", "8500"))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    cliente, secreto, _ = _montar(central)
    respuesta = cliente.get(
        "/sync/v1/pull", params={"node_id": "node-1"},
        headers={"Authorization": f"Bearer {secreto}"},
    )

    assert respuesta.status_code == 200, respuesta.text
    cambios = respuesta.json()["changes"]
    assert len(cambios) == 1
    assert cambios[0]["payload"]["nombre"] == "Mila"


@pytest.mark.parametrize("ruta,metodo", [("/sync/v1/push", "post"), ("/sync/v1/pull", "get")])
def test_las_dos_rutas_del_router_exigen_el_secreto(central, ruta, metodo):
    """El gate va en las dos, y se prueba en las dos.

    Probar solo el push dejaria la bajada abierta: es de lectura, pero de todo el
    catalogo, los precios y los clientes.
    """
    cliente, _secreto, _ = _montar(central)
    llamada = getattr(cliente, metodo)
    respuesta = (
        llamada(ruta, json=_payload()) if metodo == "post"
        else llamada(ruta, params={"node_id": "node-1"})
    )
    assert respuesta.status_code == 401


def test_el_router_rechaza_el_secreto_de_otro_nodo_en_la_bajada(central):
    cliente, secreto, _ = _montar(central)
    respuesta = cliente.get(
        "/sync/v1/pull", params={"node_id": "node-99"},
        headers={"Authorization": f"Bearer {secreto}"},
    )
    assert respuesta.status_code == 401


def test_el_router_rechaza_un_secreto_invalido_en_la_subida(central):
    """🔴 Este test faltaba, y lo encontro la matriz de mutaciones.

    El test parametrizado de arriba manda **sin cabecera**, y eso lo corta
    `_secreto()` antes de llegar a verificar nada: sacar la verificacion del
    push dejaba la suite en verde. O sea que cualquiera con un token cualquiera
    podia inyectar operaciones que el `operation_handler` del producto
    materializa como datos de dominio reales -- que es exactamente el agujero
    que la autenticacion por nodo vino a tapar en su momento.
    """
    aplicadas = []
    cliente, _secreto_real, _ = _montar(central, handler=aplicadas.append)

    respuesta = cliente.post(
        "/sync/v1/push", json=_payload(),
        headers={"Authorization": "Bearer un-token-cualquiera"},
    )

    assert respuesta.status_code == 401
    assert aplicadas == [], "el handler del producto no puede haber corrido"


def test_el_router_rechaza_el_secreto_de_otro_nodo_en_la_subida(central):
    """Suplantacion: secreto valido de node-1, diciendo ser node-99."""
    aplicadas = []
    cliente, secreto, _ = _montar(central, handler=aplicadas.append)

    respuesta = cliente.post(
        "/sync/v1/push", json=_payload(node_id="node-99"),
        headers={"Authorization": f"Bearer {secreto}"},
    )

    assert respuesta.status_code == 401
    assert aplicadas == []


def test_el_router_valida_la_forma_antes_de_tocar_la_base(central):
    """Un push incompleto no tiene por que costar una conexion."""
    cliente, secreto, aperturas = _montar(central)
    incompleto = _payload()
    del incompleto["sequence"]

    respuesta = cliente.post(
        "/sync/v1/push", json=incompleto,
        headers={"Authorization": f"Bearer {secreto}"},
    )

    assert respuesta.status_code == 422
    assert "sequence" in respuesta.json()["detail"]["missing"]
    assert aperturas == [], "no hacia falta abrir la base para saber que falta un campo"
