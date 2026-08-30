"""El endpoint de recepcion central, contra los dos motores.

Estos tests son del lado del **servidor central**, que corre PostgreSQL en
produccion desde siempre -- hasta la Fase 1 del nodo espejo (2026-08-29) se
ejercitaban solo sobre SQLite, o sea contra un motor que ahi no corre en ningun
lado. Ahora usan la fixture `conn` de `conftest.py`, asi que cada uno corre dos
veces y el id dice cual fallo.
"""

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.sync.api import create_sync_app
from libraedge.sync.receiver import SyncReceiver


def _operation_payload(node_id="node-1", sequence=1):
    return {
        "operation_id": f"{node_id}:{sequence}", "node_id": node_id, "sequence": sequence,
        "operation_type": "sale.confirmed", "aggregate_type": "sale",
        "aggregate_id": "sale-1", "occurred_at": "2026-07-25T18:30:00Z",
        "schema_version": 1, "payload": {"total": "100.00"},
    }


def _client(conn, node_repo):
    from fastapi.testclient import TestClient

    return TestClient(create_sync_app(SyncReceiver(conn), node_repo))


@pytest.fixture
def client_and_secret(conn):
    node_repo = NodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")
    return _client(conn, node_repo), secret


def test_push_without_authorization_header_is_rejected(client_and_secret):
    client, _secret = client_and_secret
    response = client.post("/sync/v1/push", json=_operation_payload())
    assert response.status_code == 401


def test_push_with_wrong_secret_is_rejected(client_and_secret):
    client, _secret = client_and_secret
    response = client.post(
        "/sync/v1/push", json=_operation_payload(),
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401


def test_push_with_correct_secret_is_accepted(client_and_secret):
    client, secret = client_and_secret
    response = client.post(
        "/sync/v1/push", json=_operation_payload(),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "accepted"


def test_pushing_the_same_operation_twice_is_a_duplicate(client_and_secret):
    """La recepcion es idempotente: el reintento de un nodo no duplica la venta.

    Es el caso que hace segura la subida -- el worker reintenta cuando no supo
    si el push llego, y el central tiene que responder `duplicate` en vez de
    materializar la operacion dos veces.
    """
    client, secret = client_and_secret
    cabeceras = {"Authorization": f"Bearer {secret}"}
    primera = client.post("/sync/v1/push", json=_operation_payload(), headers=cabeceras)
    segunda = client.post("/sync/v1/push", json=_operation_payload(), headers=cabeceras)
    assert primera.json()["result"] == "accepted"
    assert segunda.json()["result"] == "duplicate"


def test_operation_handler_materializes_the_payload_once(conn):
    """El handler del producto corre una sola vez, aunque el nodo reintente."""
    node_repo = NodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")
    aplicadas = []
    receiver = SyncReceiver(conn, operation_handler=aplicadas.append)

    from fastapi.testclient import TestClient

    client = TestClient(create_sync_app(receiver, node_repo))
    cabeceras = {"Authorization": f"Bearer {secret}"}
    client.post("/sync/v1/push", json=_operation_payload(), headers=cabeceras)
    client.post("/sync/v1/push", json=_operation_payload(), headers=cabeceras)

    assert len(aplicadas) == 1
    assert aplicadas[0].payload == {"total": "100.00"}


def test_un_handler_que_no_entiende_la_operacion_la_rechaza(conn):
    """Si el producto no sabe aplicar la operacion, el central la rechaza.

    ⚠️ Este test **no** prueba el rollback de `accept()`, y no hay que leerlo
    asi: el handler falla antes de ejecutar una sola sentencia, con lo cual la
    transaccion nunca se aborta y el rollback es un no-op. Se verifico con una
    mutacion -- sacando ese `rollback()`, este test sigue verde. Lo que prueba
    es la traduccion del error del producto a un `rejected`.
    El rollback que si importa lo cubre el test de abajo.
    """
    node_repo = NodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")

    def handler_roto(operation):
        raise ValueError("este producto no conoce sale.confirmed")

    from fastapi.testclient import TestClient

    client = TestClient(
        create_sync_app(SyncReceiver(conn, operation_handler=handler_roto), node_repo)
    )
    rechazada = client.post(
        "/sync/v1/push", json=_operation_payload(),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert rechazada.json()["result"] == "rejected"


def test_un_handler_que_viola_una_constraint_deja_la_conexion_usable(conn):
    """El caso real de envenenamiento: el handler ejecuta SQL y ese SQL falla.

    Es lo que hace `apply_confirmed_sale_operation()` de LibraCommerce --
    inserta filas de dominio--, asi que una constraint violada ahi adentro es un
    escenario de produccion, no un invento del test.

    🔴 **Contra PostgreSQL, esa sentencia fallida aborta la transaccion entera.**
    Sin el `rollback()` de `accept()`, el push siguiente muere con "current
    transaction is aborted" aunque sea perfectamente valido: una operacion mala
    de un nodo dejaria al central sin poder recibir nada mas hasta que alguien
    reciclara la conexion. En SQLite no pasa, y por eso una suite solo-SQLite no
    podia verlo.
    """
    node_repo = NodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")

    def handler_que_duplica(operation):
        # Choca contra la PK de node_identity: el nodo ya esta registrado.
        conn.execute(
            """INSERT INTO node_identity
                (node_id, branch_id, installed_at, schema_version, secret_hash)
               VALUES (?, ?, ?, ?, ?)""",
            ("node-1", "branch-1", "2026-08-29T00:00:00Z", 1, "x"),
        )

    from fastapi.testclient import TestClient

    cabeceras = {"Authorization": f"Bearer {secret}"}
    roto = TestClient(
        create_sync_app(SyncReceiver(conn, operation_handler=handler_que_duplica), node_repo)
    )
    primera = roto.post("/sync/v1/push", json=_operation_payload(), headers=cabeceras)
    assert primera.status_code == 200, primera.text

    # La conexion sigue sirviendo: otro receptor sobre la misma conexion acepta.
    sano = TestClient(create_sync_app(SyncReceiver(conn), node_repo))
    aceptada = sano.post(
        "/sync/v1/push", json=_operation_payload(sequence=2), headers=cabeceras
    )
    assert aceptada.json()["result"] == "accepted", aceptada.text


def test_push_for_deactivated_node_is_rejected(conn):
    node_repo = NodeRepository(conn)
    secret2 = node_repo.register_node("node-2", branch_id="branch-1")
    node_repo.deactivate_node("node-2")

    response = _client(conn, node_repo).post(
        "/sync/v1/push", json=_operation_payload(node_id="node-2"),
        headers={"Authorization": f"Bearer {secret2}"},
    )
    assert response.status_code == 401


def test_push_with_secret_for_a_different_node_is_rejected(client_and_secret):
    client, secret = client_and_secret
    # secret is valid for "node-1"; claiming to be "node-99" with it must fail.
    response = client.post(
        "/sync/v1/push", json=_operation_payload(node_id="node-99"),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401


def test_http_transport_sends_bearer_token(monkeypatch):
    from libraedge.domain.sync import OutboxOperation
    from libraedge.sync import http as http_module

    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"result": "accepted", "error": null}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(http_module, "urlopen", fake_urlopen)

    transport = http_module.HttpSyncTransport("https://central.example", "the-node-secret")
    operation = OutboxOperation(
        operation_id="node-1:1", node_id="node-1", sequence=1,
        operation_type="sale.confirmed", aggregate_type="sale",
        aggregate_id="sale-1", occurred_at="2026-07-25T18:30:00Z",
        schema_version=1, payload={"total": "100.00"},
    )
    result = transport.push(operation)

    assert result.result == "accepted"
    assert captured["headers"]["Authorization"] == "Bearer the-node-secret"
