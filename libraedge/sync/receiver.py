"""Generic central-side idempotent receiver."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from libraedge.domain.sync import OutboxOperation
from libraedge.sync.worker import PushResult


@dataclass
class SyncReceiver:
    #: Cualquier conexion DB-API. El ``except sqlite3.IntegrityError`` de abajo
    #: sirve igual contra PostgreSQL porque la capa de LibraCore re-lanza los
    #: errores de psycopg como sus equivalentes de ``sqlite3``; y el
    #: ``rollback()`` que lo acompaña **no es decorativo ahi**: en PostgreSQL la
    #: sentencia fallida aborta la transaccion entera, asi que sin el la
    #: conexion queda inservible para el push siguiente. Lo cubre
    #: ``tests/test_sync_api.py``, en su parametro ``[postgres]``.
    conn: object
    operation_handler: Callable[[OutboxOperation], None] | None = None
    supported_schema_version: int = 1

    def accept(self, operation: OutboxOperation) -> PushResult:
        if operation.schema_version != self.supported_schema_version:
            return PushResult("rejected", "schema incompatible")
        if self.conn.execute(
            "SELECT 1 FROM sync_inbox WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone() is not None:
            return PushResult("duplicate")
        try:
            if self.operation_handler is not None:
                self.operation_handler(operation)
            self.conn.execute(
                """INSERT INTO sync_inbox (operation_id, applied_at, status)
                   VALUES (?, ?, 'applied')""",
                (operation.operation_id, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return PushResult("duplicate")
        except (KeyError, TypeError, ValueError) as exc:
            self.conn.rollback()
            return PushResult("rejected", str(exc))
        return PushResult("accepted")
