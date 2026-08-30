import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from libraedge.domain.sync import OutboxOperation, SyncOperationStatus


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class NodeRepository:
    """Node identity and outbox persistence, on SQLite or PostgreSQL.

    Owns the durable state a LibraEdge node needs: local sequence
    generation and the outbox that ``OutboxWorker``/``SyncReceiver``
    operate on. Knows nothing about the domain of whichever product
    produced the operations.

    ``conn`` is any DB-API connection, and there is deliberately no
    dual-engine layer in here: the SQL below is qmark and portable enough
    for both, and against PostgreSQL the caller passes LibraCore's
    connection -- the very one the product uses in the node, since these
    tables live in the product's own database so that
    ``enqueue_operation()`` can run inside the product's transaction.
    That wrapper already translates qmark to psycopg's format style and
    re-raises psycopg errors as their ``sqlite3`` equivalents.
    """

    def __init__(self, conn):
        self._conn = conn

    def _escribir(self, sql: str, params=()):
        """Ejecuta una sentencia dejando la conexión utilizable si falla.

        🔴 **Esto es una diferencia de comportamiento entre motores, no de
        nombres.** En PostgreSQL una sentencia fallida **aborta la transacción
        entera**: sin este rollback, quien atrape la excepción se queda con una
        conexión envenenada y todo lo que siga muere con *"current transaction
        is aborted"*. En SQLite no pasa nada, y por eso una suite que sólo
        corría sobre SQLite no podía verlo -- se encontró el 2026-08-29 al
        parametrizar esta suite sobre los dos motores (Fase 1 del nodo espejo).

        Importa de verdad acá: el nodo encola dentro de la transacción del
        producto, así que una operación rechazada que dejara la conexión
        abortada se llevaría puesta la venta que la estaba abriendo.

        La excepción se re-lanza igual: el rollback deja la conexión sana, no
        se traga el error.
        """
        try:
            return self._conn.execute(sql, params)
        except Exception:
            self._conn.rollback()
            raise

    def register_node(self, node_id: str, branch_id: str, schema_version: int = 1) -> str:
        """Create a node identity and return its plaintext secret.

        The secret is shown here once -- only its hash is persisted, same
        pattern as an API key: whoever provisions the edge node copies it
        into ``HttpSyncTransport`` immediately, there's no way to recover
        it later (re-registering, i.e. calling this again for the same
        node_id, issues a new secret and invalidates the old one).
        """
        secret = secrets.token_urlsafe(32)
        self._escribir(
            """INSERT INTO node_identity
                (node_id, branch_id, installed_at, schema_version, secret_hash)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                secret_hash = excluded.secret_hash, active = 1""",
            (node_id, branch_id, datetime.now(timezone.utc).isoformat(),
             schema_version, _hash_secret(secret)),
        )
        self._conn.commit()
        return secret

    def verify_node_secret(self, node_id: str, secret: str) -> bool:
        """True only for an active node whose secret hash matches."""
        row = self._conn.execute(
            "SELECT secret_hash, active FROM node_identity WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None or not row[1]:
            return False
        return hmac.compare_digest(row[0], _hash_secret(secret))

    def deactivate_node(self, node_id: str) -> None:
        """Revoke a node -- e.g. a stolen/decommissioned mini PC. Its
        secret stops verifying immediately; re-registering issues a new
        one and reactivates it."""
        self._escribir(
            "UPDATE node_identity SET active = 0 WHERE node_id = ?", (node_id,)
        )
        self._conn.commit()

    def next_sequence(self, name: str = "operations") -> int:
        row = self._conn.execute(
            "SELECT next_value FROM local_sequences WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            sequence = 1
            self._escribir(
                "INSERT INTO local_sequences (name, next_value) VALUES (?, ?)",
                (name, 2),
            )
        else:
            sequence = row[0]
            self._escribir(
                "UPDATE local_sequences SET next_value = ? WHERE name = ?",
                (sequence + 1, name),
            )
        self._conn.commit()
        return sequence

    def enqueue_operation(self, operation: OutboxOperation) -> OutboxOperation:
        """Persist an outbox operation exactly once by operation_id.

        ``created_at`` se estampa **acá, en Python y en UTC**, no con el
        ``DEFAULT CURRENT_TIMESTAMP`` de la tabla. Ese default es reloj del
        servidor y los dos motores lo escriben distinto: SQLite pone
        ``2026-08-30 02:44:13`` (UTC, sin offset, sin microsegundos) y
        PostgreSQL ``2026-08-29 23:44:14.017409-03`` (hora de la sesión, con
        offset). Es el mismo instante escrito de tres formas distintas en una
        columna TEXT que después se lee como string, y en un nodo se mezclarían
        las dos formas en la misma tabla.

        Con esto queda como ``sent_at`` y ``acknowledged_at``, que ya se
        estampaban así: ISO-8601 en UTC, igual en los dos motores. El default
        del DDL se conserva sólo como red por si alguien inserta a mano.
        """
        self._escribir(
            """
            INSERT INTO sync_outbox
                (operation_id, node_id, sequence, operation_type, aggregate_type,
                 aggregate_id, occurred_at, schema_version, payload_json, status,
                 attempts, next_attempt_at, last_error, created_at, sent_at,
                 acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
            """,
            (operation.operation_id, operation.node_id, operation.sequence,
             operation.operation_type, operation.aggregate_type, operation.aggregate_id,
             operation.occurred_at, operation.schema_version, operation.payload_json(),
             str(operation.status), operation.attempts, operation.next_attempt_at,
             operation.last_error,
             operation.created_at or datetime.now(timezone.utc).isoformat(),
             operation.sent_at, operation.acknowledged_at),
        )
        self._conn.commit()
        return self.get_operation(operation.operation_id)

    def get_operation(self, operation_id: str) -> OutboxOperation | None:
        row = self._conn.execute(
            """SELECT operation_id, node_id, sequence, operation_type, aggregate_type,
                    aggregate_id, occurred_at, schema_version, payload_json, status,
                    attempts, next_attempt_at, last_error, created_at, sent_at,
                    acknowledged_at
             FROM sync_outbox WHERE operation_id = ?""", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        return OutboxOperation(
            operation_id=row[0], node_id=row[1], sequence=row[2],
            operation_type=row[3], aggregate_type=row[4], aggregate_id=row[5],
            occurred_at=row[6], schema_version=row[7], payload=json.loads(row[8]),
            status=SyncOperationStatus(row[9]), attempts=row[10],
            next_attempt_at=row[11], last_error=row[12], created_at=row[13],
            sent_at=row[14], acknowledged_at=row[15],
        )

    def list_pending_operations(self, limit: int = 100) -> tuple[OutboxOperation, ...]:
        rows = self._conn.execute(
            """SELECT operation_id FROM sync_outbox
             WHERE status IN (?, ?)
             ORDER BY node_id, sequence LIMIT ?""",
            (str(SyncOperationStatus.PENDING), str(SyncOperationStatus.RETRYABLE_ERROR), limit),
        ).fetchall()
        return tuple(self.get_operation(row[0]) for row in rows)

    def mark_operation_sending(self, operation_id: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, attempts = attempts + 1 WHERE operation_id = ?",
            (str(SyncOperationStatus.SENDING), operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def retry_operation(
        self, operation_id: str, error: str, next_attempt_at: str | None = None
    ) -> OutboxOperation | None:
        self._escribir(
            """UPDATE sync_outbox SET status = ?, last_error = ?, next_attempt_at = ?
             WHERE operation_id = ?""",
            (str(SyncOperationStatus.RETRYABLE_ERROR), error, next_attempt_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def mark_operation_manual_review(self, operation_id: str, error: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, last_error = ? WHERE operation_id = ?",
            (str(SyncOperationStatus.MANUAL_REVIEW), error, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def acknowledge_operation(self, operation_id: str, acknowledged_at: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, acknowledged_at = ? WHERE operation_id = ?",
            (str(SyncOperationStatus.ACKNOWLEDGED), acknowledged_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)


#: Nombre anterior, de cuando este repositorio era solo-SQLite. Lo importa
#: LibraCommerce (tests/test_offline_sync.py), asi que se conserva: renombrar
#: sin alias rompia al unico consumidor que hay.
SqliteNodeRepository = NodeRepository
