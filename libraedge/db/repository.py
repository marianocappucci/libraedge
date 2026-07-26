import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timezone

from libraedge.domain.sync import OutboxOperation, SyncOperationStatus


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class SqliteNodeRepository:
    """SQLite adapter for node identity and outbox persistence.

    Owns the durable state a LibraEdge node needs: local sequence
    generation and the outbox that ``OutboxWorker``/``SyncReceiver``
    operate on. Knows nothing about the domain of whichever product
    produced the operations.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def register_node(self, node_id: str, branch_id: str, schema_version: int = 1) -> str:
        """Create a node identity and return its plaintext secret.

        The secret is shown here once -- only its hash is persisted, same
        pattern as an API key: whoever provisions the edge node copies it
        into ``HttpSyncTransport`` immediately, there's no way to recover
        it later (re-registering, i.e. calling this again for the same
        node_id, issues a new secret and invalidates the old one).
        """
        secret = secrets.token_urlsafe(32)
        self._conn.execute(
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
        self._conn.execute(
            "UPDATE node_identity SET active = 0 WHERE node_id = ?", (node_id,)
        )
        self._conn.commit()

    def next_sequence(self, name: str = "operations") -> int:
        row = self._conn.execute(
            "SELECT next_value FROM local_sequences WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            sequence = 1
            self._conn.execute(
                "INSERT INTO local_sequences (name, next_value) VALUES (?, ?)",
                (name, 2),
            )
        else:
            sequence = row[0]
            self._conn.execute(
                "UPDATE local_sequences SET next_value = ? WHERE name = ?",
                (sequence + 1, name),
            )
        self._conn.commit()
        return sequence

    def enqueue_operation(self, operation: OutboxOperation) -> OutboxOperation:
        """Persist an outbox operation exactly once by operation_id."""
        self._conn.execute(
            """
            INSERT INTO sync_outbox
                (operation_id, node_id, sequence, operation_type, aggregate_type,
                 aggregate_id, occurred_at, schema_version, payload_json, status,
                 attempts, next_attempt_at, last_error, sent_at, acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
            """,
            (operation.operation_id, operation.node_id, operation.sequence,
             operation.operation_type, operation.aggregate_type, operation.aggregate_id,
             operation.occurred_at, operation.schema_version, operation.payload_json(),
             operation.status, operation.attempts, operation.next_attempt_at,
             operation.last_error, operation.sent_at, operation.acknowledged_at),
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
            (SyncOperationStatus.PENDING, SyncOperationStatus.RETRYABLE_ERROR, limit),
        ).fetchall()
        return tuple(self.get_operation(row[0]) for row in rows)

    def mark_operation_sending(self, operation_id: str) -> OutboxOperation | None:
        self._conn.execute(
            "UPDATE sync_outbox SET status = ?, attempts = attempts + 1 WHERE operation_id = ?",
            (SyncOperationStatus.SENDING, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def retry_operation(
        self, operation_id: str, error: str, next_attempt_at: str | None = None
    ) -> OutboxOperation | None:
        self._conn.execute(
            """UPDATE sync_outbox SET status = ?, last_error = ?, next_attempt_at = ?
             WHERE operation_id = ?""",
            (SyncOperationStatus.RETRYABLE_ERROR, error, next_attempt_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def mark_operation_manual_review(self, operation_id: str, error: str) -> OutboxOperation | None:
        self._conn.execute(
            "UPDATE sync_outbox SET status = ?, last_error = ? WHERE operation_id = ?",
            (SyncOperationStatus.MANUAL_REVIEW, error, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def acknowledge_operation(self, operation_id: str, acknowledged_at: str) -> OutboxOperation | None:
        self._conn.execute(
            "UPDATE sync_outbox SET status = ?, acknowledged_at = ? WHERE operation_id = ?",
            (SyncOperationStatus.ACKNOWLEDGED, acknowledged_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)
