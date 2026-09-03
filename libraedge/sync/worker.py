"""Durable local outbox worker, independent from HTTP transport."""

from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Protocol

from libraedge.domain.sync import OutboxOperation


@dataclass(frozen=True)
class PushResult:
    result: str  # accepted, duplicate, rejected
    error: str | None = None


class SyncTransport(Protocol):
    def push(self, operation: OutboxOperation) -> PushResult: ...


@dataclass(frozen=True)
class ResultadoOutbox:
    """Que paso al drenar la cola.

    🔴 **Antes esto era un `int` con la cuenta de procesadas, y esa cuenta no
    distingue exito de fracaso**: el worker atrapa los errores de transporte a
    proposito --los convierte en reintentos, que es lo que lo hace durable-- y
    despues los contaba igual que a los confirmados. Un nodo que mirara ese
    numero se declararia en linea con la cola entera atascada. Se encontro al
    escribir el ciclo del nodo, en la Fase 4.
    """

    procesadas: int = 0
    confirmadas: int = 0
    fallidas: int = 0
    rechazadas: int = 0
    ultimo_error: str | None = None

    @property
    def hubo_falla_de_transporte(self) -> bool:
        """Si algo no pudo salir. Es lo que define si el nodo esta en linea."""
        return self.fallidas > 0


class OutboxWorker:
    def __init__(self, repository, transport: SyncTransport):
        self.repository = repository
        self.transport = transport

    def run_once(self, limit=100) -> ResultadoOutbox:
        # Antes de nada, rescatar lo que una corrida anterior dejó en `sending`
        # al morirse. Sin esto esas operaciones no las vuelve a mirar nadie: la
        # venta que el nodo cobró no llega al central y nada falla. Ver el
        # docstring de `reclamar_operaciones_colgadas`.
        self.repository.reclamar_operaciones_colgadas()

        procesadas = confirmadas = fallidas = rechazadas = 0
        ultimo_error = None
        for operation in self.repository.list_pending_operations(limit):
            self.repository.mark_operation_sending(operation.operation_id)
            try:
                result = self.transport.push(operation)
            except Exception as exc:
                self.repository.retry_operation(
                    operation.operation_id, str(exc), self._now()
                )
                procesadas += 1
                fallidas += 1
                ultimo_error = f"{type(exc).__name__}: {exc}"
                continue
            if result.result in {"accepted", "duplicate"}:
                self.repository.acknowledge_operation(operation.operation_id, self._now())
                confirmadas += 1
            else:
                self.repository.mark_operation_manual_review(
                    operation.operation_id, result.error or "operación rechazada"
                )
                rechazadas += 1
                ultimo_error = result.error or "operación rechazada"
            procesadas += 1
        return ResultadoOutbox(
            procesadas=procesadas, confirmadas=confirmadas, fallidas=fallidas,
            rechazadas=rechazadas, ultimo_error=ultimo_error,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
