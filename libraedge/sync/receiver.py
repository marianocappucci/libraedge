"""Generic central-side idempotent receiver."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from libraedge.domain.sync import OutboxOperation
from libraedge.sync.worker import PushResult


@dataclass
class SyncReceiver:
    conn: sqlite3.Connection
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
