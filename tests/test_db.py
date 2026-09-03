"""El repositorio de nodo y outbox, contra los dos motores.

Cada test que pide la fixture `repo` corre dos veces -- `[sqlite]` y
`[postgres]` -- y el id dice cual fallo. Ver `tests/conftest.py` para por que
SQLite se conserva como parametro y por que la conexion de PostgreSQL es la de
LibraCore y no una de psycopg cruda.
"""

import sqlite3
from datetime import UTC

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
    assert marca.utcoffset() == UTC.utcoffset(None)

    ahora = datetime.now(UTC)
    assert abs((ahora - marca).total_seconds()) < 60


def test_encolar_sin_commit_no_publica_nada_todavia(repo, conn):
    """🔴 La propiedad que hace atomico al nodo.

    Un producto encola DENTRO de su transaccion: `cobrar_pedido()` de Restolibra
    corre pedido, venta, items, pagos, caja, stock, turno y mesa en una sola, y
    despues commitea. Si `enqueue_operation()` commiteara por su cuenta en el
    medio, publicaria esa venta a medio hacer.
    """
    repo.enqueue_operation(operation(), commit=False)
    # Dentro de la misma transaccion se ve...
    assert len(repo.list_pending_operations()) == 1
    # ...pero deshacerla se la lleva.
    conn.rollback()
    assert repo.list_pending_operations() == ()


def test_encolar_sin_commit_queda_cuando_el_producto_commitea(repo, conn):
    """La otra mitad: si la venta se confirma, la operacion queda con ella.

    Sin este par, el test de arriba pasaria igual con un `enqueue` que no
    escribe nada.
    """
    repo.enqueue_operation(operation(), commit=False)
    conn.commit()
    assert len(repo.list_pending_operations()) == 1


def test_encolar_sin_commit_no_deshace_lo_que_el_producto_hizo_antes(repo, conn, motor):
    """El enqueue no puede hacer rollback de una transaccion que no es suya.

    Es una frontera de correccion, no una preferencia: si el repositorio hiciera
    rollback por su cuenta ante un error, se llevaria puesto el trabajo del
    producto **antes de que el producto se entere**, y le sacaria la decision.

    ⚠️ **Se mide en SQLite porque es donde la diferencia es observable.** En
    PostgreSQL la sentencia fallida aborta la transaccion entera --es el motor,
    no este repositorio-- asi que ahi el resultado final es el mismo haga o no
    haga rollback. Que en el motor del nodo sea indistinguible no vuelve
    correcto que una biblioteca deshaga trabajo ajeno: el dia que un producto
    quiera atrapar el error y seguir sin la fila de outbox, tiene que poder.
    """
    if motor == "postgres":
        pytest.skip(
            "en PostgreSQL la sentencia fallida aborta la transaccion sola, "
            "asi que el rollback del repositorio no se puede distinguir"
        )
    conn.execute("INSERT INTO local_sequences (name, next_value) VALUES (?, ?)", ("marca", 1))
    repo.enqueue_operation(operation(), commit=False)
    with pytest.raises(sqlite3.IntegrityError):
        repo.enqueue_operation(operation(operation_id="node-1:999"), commit=False)

    # El producto decide seguir adelante con lo suyo. Si el repositorio hubiera
    # hecho rollback, esta fila ya no estaria.
    conn.commit()
    fila = conn.execute(
        "SELECT next_value FROM local_sequences WHERE name = ?", ("marca",)
    ).fetchone()
    assert fila is not None, "el repositorio deshizo trabajo del producto"


def test_next_sequence_sin_commit_se_deshace_con_la_venta(repo, conn):
    """Una secuencia reservada dentro de la transaccion del producto no deja
    hueco si la venta se cae."""
    primera = repo.next_sequence(commit=False)
    conn.rollback()
    assert repo.next_sequence(commit=False) == primera


def test_worker_flow_marks_sending_then_retries_on_transport_error(repo):
    from libraedge.sync.worker import OutboxWorker

    repo.enqueue_operation(operation())

    class FailingTransport:
        def push(self, op):
            raise ConnectionError("offline")

    resultado = OutboxWorker(repo, FailingTransport()).run_once()
    assert resultado.procesadas == 1
    assert resultado.fallidas == 1, "un fallo de transporte tiene que ser distinguible"
    assert resultado.hubo_falla_de_transporte is True
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

    resultado = OutboxWorker(repo, AcceptingTransport()).run_once()
    assert resultado.confirmadas == 1
    assert resultado.hubo_falla_de_transporte is False
    assert repo.list_pending_operations() == ()
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED
