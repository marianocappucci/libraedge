import sqlite3

import pytest

from libraedge.db.repository import SqliteNodeRepository
from libraedge.db.schema import init_schema
from libraedge.sync.api import create_sync_app
from libraedge.sync.receiver import SyncReceiver


def _operation_payload(node_id="node-1", sequence=1):
    return {
        "operation_id": f"{node_id}:{sequence}", "node_id": node_id, "sequence": sequence,
        "operation_type": "sale.confirmed", "aggregate_type": "sale",
        "aggregate_id": "sale-1", "occurred_at": "2026-07-25T18:30:00Z",
        "schema_version": 1, "payload": {"total": "100.00"},
    }


@pytest.fixture
def client_and_secret():
    from fastapi.testclient import TestClient

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conn)
    node_repo = SqliteNodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")
    receiver = SyncReceiver(conn)
    app = create_sync_app(receiver, node_repo)
    return TestClient(app), secret


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


def test_push_for_deactivated_node_is_rejected():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conn)
    node_repo = SqliteNodeRepository(conn)
    secret2 = node_repo.register_node("node-2", branch_id="branch-1")
    node_repo.deactivate_node("node-2")
    receiver = SyncReceiver(conn)
    from fastapi.testclient import TestClient
    deactivated_client = TestClient(create_sync_app(receiver, node_repo))

    response = deactivated_client.post(
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
