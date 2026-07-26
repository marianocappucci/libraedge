import sqlite3

from libraedge.db.repository import SqliteNodeRepository
from libraedge.db.schema import init_schema
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus


def _repo():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return SqliteNodeRepository(conn)


def operation(node_id="node-1", sequence=1, operation_id=None):
    return OutboxOperation(
        operation_id=operation_id or f"{node_id}:{sequence}", node_id=node_id, sequence=sequence,
        operation_type="sale.confirmed", aggregate_type="sale",
        aggregate_id="sale-1", occurred_at="2026-07-25T18:30:00Z",
        schema_version=1, payload={"total": "100.00"},
    )


def test_schema_creates_node_and_outbox_tables():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"node_identity", "local_sequences", "sync_outbox", "sync_inbox"} <= tables


def test_next_sequence_increments_per_name():
    repo = _repo()
    assert repo.next_sequence() == 1
    assert repo.next_sequence() == 2
    assert repo.next_sequence("other") == 1


def test_enqueue_and_list_pending_then_acknowledge():
    repo = _repo()
    saved = repo.enqueue_operation(operation())
    assert saved.status == SyncOperationStatus.PENDING
    assert len(repo.list_pending_operations()) == 1
    ack = repo.acknowledge_operation(saved.operation_id, "2026-07-25T18:31:00Z")
    assert ack.status == SyncOperationStatus.ACKNOWLEDGED
    assert repo.list_pending_operations() == ()


def test_enqueue_is_idempotent_by_operation_id():
    repo = _repo()
    first = repo.enqueue_operation(operation())
    second = repo.enqueue_operation(operation())
    assert first == second
    assert len(repo.list_pending_operations()) == 1


def test_enqueue_rejects_same_node_sequence_for_different_operation():
    repo = _repo()
    repo.enqueue_operation(operation())
    other = operation(operation_id="node-1:999")
    try:
        repo.enqueue_operation(other)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate node sequence must be rejected")


def test_worker_flow_marks_sending_then_retries_on_transport_error():
    from libraedge.sync.worker import OutboxWorker, PushResult

    repo = _repo()
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


def test_register_node_issues_a_secret_that_verifies():
    repo = _repo()
    secret = repo.register_node("node-1", branch_id="branch-1")
    assert secret
    assert repo.verify_node_secret("node-1", secret) is True


def test_verify_node_secret_rejects_wrong_secret():
    repo = _repo()
    repo.register_node("node-1", branch_id="branch-1")
    assert repo.verify_node_secret("node-1", "not-the-real-secret") is False


def test_verify_node_secret_rejects_unknown_node():
    repo = _repo()
    assert repo.verify_node_secret("never-registered", "anything") is False


def test_deactivate_node_rejects_its_secret():
    repo = _repo()
    secret = repo.register_node("node-1", branch_id="branch-1")
    repo.deactivate_node("node-1")
    assert repo.verify_node_secret("node-1", secret) is False


def test_re_registering_a_node_issues_a_new_secret_and_reactivates_it():
    repo = _repo()
    old_secret = repo.register_node("node-1", branch_id="branch-1")
    repo.deactivate_node("node-1")
    new_secret = repo.register_node("node-1", branch_id="branch-1")
    assert new_secret != old_secret
    assert repo.verify_node_secret("node-1", old_secret) is False
    assert repo.verify_node_secret("node-1", new_secret) is True


def test_worker_flow_acknowledges_accepted_operation():
    from libraedge.sync.worker import OutboxWorker, PushResult

    repo = _repo()
    repo.enqueue_operation(operation())

    class AcceptingTransport:
        def push(self, op):
            return PushResult("accepted")

    processed = OutboxWorker(repo, AcceptingTransport()).run_once()
    assert processed == 1
    assert repo.list_pending_operations() == ()
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED
