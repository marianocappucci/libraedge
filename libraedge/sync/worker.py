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
        """Drena la cola **en orden de secuencia**. Una tanda, y se corta si el transporte falla.

        🔴 **El orden es parte de la correccion, no una prolijidad.** Una venta
        depende de lo que la precede en el nodo: el `cliente.alta` que crea al
        cliente, el `turno.abierto` que abre la caja. Si una de esas falla por
        la red y el worker sigue con la siguiente, la venta llega al central
        **antes** que lo que ella necesita y, en el mejor caso, termina en
        revision manual por una FK que se resolvia sola con solo esperar.

        Hasta v0.16 esto hacia `continue` ante un fallo, asi que con la red
        intermitente la cola se salteaba lo que fallaba y subia el resto.

        Que corta y que no:

        - **Un fallo de transporte o un error reintentable** (timeout, 5xx, 409 de
          esquema, cualquier excepcion del `push`): la operacion queda
          `retryable_error` y **se corta la tanda** (`break`). Las que siguen no
          se tocan: quedan `pending` y salen en el ciclo siguiente, despues de
          esta, porque `list_pending_operations` ordena por `(node_id, sequence)`.
          Si el central no contesta para una, no contesta para las demas: cortar
          ahorra ademas un timeout por cada operacion que quedaba en la tanda.
        - **Un rechazo definitivo del central** (`rejected`): la operacion pasa a
          `manual_review` y la tanda **sigue**. Es una respuesta, no una falla:
          el central ya la vio y no la va a aceptar tal cual, asi que esperarla
          no la arregla, y trabar la cola detras de ella dejaria **todas** las
          ventas posteriores del local sin subir hasta que alguien la resuelva
          a mano. Nada se pierde: queda guardada entera en el nodo (y en el
          central) para revisarla con `libraedge-nodo revision`.

        Lo que sigue a un rechazo y dependia de ella el central lo rechaza a su
        vez (el manejador no encuentra lo que la operacion necesita), y va tambien
        a revision. Al resolverlas, `reenviar_operacion` las devuelve a `pending`
        y salen otra vez por secuencia: hay que reenviar en el orden en que se
        crearon. Ver `DECISIONS.md`, ADR-020. Desde v0.25 (ADR-028) el nodo las
        reenvia solo cuando el central le dice que ya las aplico
        (`libraedge.sync.cierre_de_revision`); como para entonces el central ya
        las tiene, el orden de esos reenvios no importa: todas salen `duplicate`.

        Una operacion `discarded` (v0.21, ADR-024: soporte la cerro con motivo
        porque el rechazo no se corrige) no la ve este metodo: la cola es solo
        `pending` y `retryable_error`.
        """
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
                # Se corta: seguir subiria las posteriores antes que esta.
                break
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
