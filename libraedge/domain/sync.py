"""Transport-neutral synchronization contracts.

Hay **dos direcciones y no son simetricas**, porque la autoridad tampoco lo es:

- **Subida** (`OutboxOperation`): el nodo publica los *eventos* que genero
  durante el corte --una venta, un cobro--, append-only. El central los aplica
  con el `operation_handler` que provee el producto.
- **Bajada** (`ReferenceChange`): el central publica el *estado* de los datos de
  referencia --un precio nuevo, un producto dado de baja--. El nodo los espeja y
  nunca los edita.

Esa asimetria es lo que hace que no haya merge ni resolucion de conflictos: no
existe una fila que los dos lados quieran escribir.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReferenceOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True)
class ReferenceChange:
    """Un cambio del central sobre un dato de referencia, para espejar.

    `cursor` es la posicion monotona del cambio en el changelog central. Es la
    marca que el nodo guarda, y **no es una fecha a proposito**: el reloj de una
    PC de cliente no es confiable, y dos transacciones pueden commitear fuera de
    orden de timestamp. Un entero que solo crece no tiene ninguno de los dos
    problemas.

    `payload` es la fila entera como diccionario, asi el nodo puede espejarla sin
    saber que significa. En un `delete` viene la fila que se borro, para poder
    identificarla.
    """

    cursor: int
    table_name: str
    row_id: str
    operation: ReferenceOperation
    payload: dict[str, Any] | None = None
    recorded_at: str | None = None


class SyncOperationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    ACKNOWLEDGED = "acknowledged"
    RETRYABLE_ERROR = "retryable_error"
    MANUAL_REVIEW = "manual_review"
    #: Terminal (v0.21, ADR-024): alguien decidio que el rechazo del central no se corrige y la cerro con motivo. No
    #: vuelve a salir, no cuenta como pendiente y no se puede reenviar. Es solo del outbox del nodo: no suma columnas.
    DISCARDED = "discarded"


class InboxStatus(StrEnum):
    """El estado de una operacion **en el central** (`sync_inbox.status`).

    `manual_review` y `discarded` existen desde v0.7: antes una operacion que el
    central no podia aplicar no dejaba rastro de su lado --solo el nodo sabia que
    se la habian rechazado-- y no habia con que revisarla ni reintentarla.
    """

    APPLIED = "applied"
    MANUAL_REVIEW = "manual_review"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class OutboxOperation:
    operation_id: str
    node_id: str
    sequence: int
    operation_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    schema_version: int
    payload: dict[str, Any]
    status: SyncOperationStatus = SyncOperationStatus.PENDING
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    sent_at: str | None = None
    acknowledged_at: str | None = None

    def payload_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))

    def como_dict(self) -> dict[str, Any]:
        """Los campos que viajan en un push (los mismos que `CAMPOS_DEL_PUSH`)."""
        return {
            "operation_id": self.operation_id, "node_id": self.node_id,
            "sequence": self.sequence, "operation_type": self.operation_type,
            "aggregate_type": self.aggregate_type, "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at, "schema_version": self.schema_version,
            "payload": self.payload,
        }
