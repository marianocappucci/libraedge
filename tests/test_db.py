"""El repositorio de nodo y outbox, contra los dos motores.

Cada test que pide la fixture `repo` corre dos veces -- `[sqlite]` y
`[postgres]` -- y el id dice cual fallo. Ver `tests/conftest.py` para por que
SQLite se conserva como parametro y por que la conexion de PostgreSQL es la de
LibraCore y no una de psycopg cruda.
"""

import sqlite3

import pytest

from libraedge.db.schema import init_schema
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus


def operation(node_id="node-1", sequence=1, operation_id=None):
    return OutboxOperation(
        operation_id=operation_id or f"{node_id}:{sequence}", node_id=node_id, sequence=sequence,
        operation_type="sale.confirmed", aggregate_type="sale",
        aggregate_id="sale-1", occurred_at="2026-07-25T18:30:00Z",
        schema_version=1, payload={"total": "100.00"},
    )


def _tablas(conn, motor) -> set[str]:
    if motor == "sqlite":
        consulta = "SELECT name FROM sqlite_master WHERE type='table'"
    else:
        consulta = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    return {row[0] for row in conn.execute(consulta).fetchall()}


TABLAS = {"node_identity", "local_sequences", "sync_outbox", "sync_inbox"}


def test_schema_creates_node_and_outbox_tables(conn, motor):
    assert TABLAS <= _tablas(conn, motor)


def test_init_schema_is_idempotent(conn, motor):
    """Se corre en cada arranque del nodo, no una sola vez en la instalacion."""
    init_schema(conn)
    conn.commit()
    assert TABLAS <= _tablas(conn, motor)


def test_next_sequence_increments_per_name(repo):
    assert repo.next_sequence() == 1
    assert repo.next_sequence() == 2
    assert repo.next_sequence("other") == 1


def test_enqueue_and_list_pending_then_acknowledge(repo):
    saved = repo.enqueue_operation(operation())
    assert saved.status == SyncOperationStatus.PENDING
    assert len(repo.list_pending_operations()) == 1
    ack = repo.acknowledge_operation(saved.operation_id, "2026-07-25T18:31:00Z")
    assert ack.status == SyncOperationStatus.ACKNOWLEDGED
    assert repo.list_pending_operations() == ()


def test_enqueue_is_idempotent_by_operation_id(repo):
    first = repo.enqueue_operation(operation())
    second = repo.enqueue_operation(operation())
    assert first == second
    assert len(repo.list_pending_operations()) == 1


def test_enqueue_rejects_same_node_sequence_for_different_operation(repo):
    """El indice unico (node_id, sequence) rechaza una secuencia repetida.

    Se afirma sobre `sqlite3.IntegrityError` en los DOS motores a proposito:
    contra PostgreSQL, la capa de LibraCore re-lanza la `UniqueViolation` de
    psycopg como su equivalente de `sqlite3`. Si esa traduccion dejara de
    funcionar, este test se pone rojo -- que es justo lo que tiene que pasar,
    porque hay un `except sqlite3.IntegrityError` en `sync/receiver.py` que
    depende de ella.
    """
    repo.enqueue_operation(operation())
    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue_operation(operation(operation_id="node-1:999"))


def test_repo_sigue_usable_despues_de_una_secuencia_rechazada(repo):
    """El error no puede dejar la conexion inservible.

    Esta es la diferencia entre motores que un test solo-SQLite no puede ver:
    en PostgreSQL una sentencia fallida **aborta la transaccion**, y todo lo que
    venga despues muere con "current transaction is aborted" hasta que alguien
    haga rollback. En SQLite no pasa nada y el mismo codigo anda.

    Es exactamente el caso que el contrato de la familia manda auditar caso por
    caso. Aca importa de verdad: el nodo encola dentro de la transaccion del
    producto, asi que una operacion rechazada que deje la conexion abortada se
    lleva puesta la venta que la estaba abriendo.
    """
    repo.enqueue_operation(operation())
    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue_operation(operation(operation_id="node-1:999"))

    # La conexion tiene que seguir sirviendo: se lee y se escribe de nuevo.
    assert len(repo.list_pending_operations()) == 1
    otra = repo.enqueue_operation(operation(sequence=2, operation_id="node-1:2"))
    assert otra.status == SyncOperationStatus.PENDING
    assert len(repo.list_pending_operations()) == 2


def test_created_at_es_iso_utc_en_los_dos_motores(repo):
    """La marca de encolado no puede depender del reloj del servidor.

    Con el `DEFAULT CURRENT_TIMESTAMP` de la tabla, SQLite escribia
    `2026-08-30 02:44:13` (UTC, sin offset) y PostgreSQL
    `2026-08-29 23:44:14.017409-03` (hora de la sesion, con offset): el mismo
    instante en tres formas distintas, en una columna TEXT que se lee como
    string. En un nodo se mezclarian las dos formas en la misma tabla.
    """
    from datetime import datetime, timezone

    guardada = repo.enqueue_operation(operation())
    marca = datetime.fromisoformat(guardada.created_at)
    assert marca.tzinfo is not None, "sin zona, no se puede comparar entre nodos"
    assert marca.utcoffset() == timezone.utc.utcoffset(None)

    ahora = datetime.now(timezone.utc)
    assert abs((ahora - marca).total_seconds()) < 60


def test_worker_flow_marks_sending_then_retries_on_transport_error(repo):
    from libraedge.sync.worker import OutboxWorker

    repo.enqueue_operation(operation())

    class FailingTransport:
        def push(self, op):
            raise ConnectionError("offline")

    processed = OutboxWorker(repo, FailingTransport()).run_once()
    assert processed == 1
    pending = repo.list_pending_operations()
    assert len(pending) == 1
    assert pending[0].status == SyncOperationStatus.RETRYABLE_ERROR
    assert pending[0].attempts == 1


def test_register_node_issues_a_secret_that_verifies(repo):
    secret = repo.register_node("node-1", branch_id="branch-1")
    assert secret
    assert repo.verify_node_secret("node-1", secret) is True


def test_verify_node_secret_rejects_wrong_secret(repo):
    repo.register_node("node-1", branch_id="branch-1")
    assert repo.verify_node_secret("node-1", "not-the-real-secret") is False


def test_verify_node_secret_rejects_unknown_node(repo):
    assert repo.verify_node_secret("never-registered", "anything") is False


def test_deactivate_node_rejects_its_secret(repo):
    secret = repo.register_node("node-1", branch_id="branch-1")
    repo.deactivate_node("node-1")
    assert repo.verify_node_secret("node-1", secret) is False


def test_re_registering_a_node_issues_a_new_secret_and_reactivates_it(repo):
    old_secret = repo.register_node("node-1", branch_id="branch-1")
    repo.deactivate_node("node-1")
    new_secret = repo.register_node("node-1", branch_id="branch-1")
    assert new_secret != old_secret
    assert repo.verify_node_secret("node-1", old_secret) is False
    assert repo.verify_node_secret("node-1", new_secret) is True


def test_worker_flow_acknowledges_accepted_operation(repo):
    from libraedge.sync.worker import OutboxWorker, PushResult

    repo.enqueue_operation(operation())

    class AcceptingTransport:
        def push(self, op):
            return PushResult("accepted")

    processed = OutboxWorker(repo, AcceptingTransport()).run_once()
    assert processed == 1
    assert repo.list_pending_operations() == ()
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED
