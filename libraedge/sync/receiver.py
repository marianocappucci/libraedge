"""Generic central-side idempotent receiver.

## Qué pasa con cada operación que llega

- **Ya estaba aplicada** (``sync_inbox`` en ``applied``): ``duplicate``. Es el
  reintento normal de un nodo que no supo si su push llegó.
- **Ya estaba en revisión o descartada**: ``rejected`` con el motivo guardado.
  No se reprocesa sola: reintentarla es una decisión de quien revisa
  (``libraedge.sync.revision.reintentar``).
- **Sin manejador** para su ``(operation_type, schema_version)``, o el manejador
  la rechazó: se guarda **entera** en ``sync_inbox`` como ``manual_review``, con
  el motivo, y el nodo recibe ``rejected`` (y la pasa a revisión de su lado).
- **Aplicada**: ``applied`` y ``accepted``.

🔴 Hasta v0.6 una operación de un tipo desconocido se marcaba ``applied``: el
manejador del producto filtraba con ``if operation_type != ...: return`` y ese
``return`` sin error era, para el receptor, un éxito. Ver ``despacho.py``.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from libraedge.domain.sync import InboxStatus, OutboxOperation
from libraedge.sync.despacho import Despachador, SinManejador
from libraedge.sync.worker import PushResult


def _ahora() -> str:
    return datetime.now(UTC).isoformat()


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
    #: La API de siempre: un manejador de **un** argumento y una sola version.
    #: Se conserva para no romper a quien la usa; lo nuevo es ``despachador``.
    operation_handler: Callable[[OutboxOperation], None] | None = None
    supported_schema_version: int = 1
    #: Desde v0.7: manejadores por ``(operation_type, schema_version)``, con
    #: firma ``(conexion, operacion)``. Si esta, manda sobre los dos de arriba.
    despachador: Despachador | None = None

    def accept(self, operation: OutboxOperation) -> PushResult:
        previo = self._en_inbox(operation.operation_id)
        if previo is not None:
            estado, motivo = previo
            if estado == InboxStatus.APPLIED:
                return PushResult("duplicate")
            if estado == InboxStatus.DISCARDED:
                return PushResult("rejected", f"descartada en el central: {motivo}")
            return PushResult("rejected", f"en revision manual en el central: {motivo}")
        return self.aplicar(operation)

    def aplicar(self, operation: OutboxOperation) -> PushResult:
        """Corre el manejador y deja la fila de ``sync_inbox`` que corresponda.

        Es ``accept`` sin la deduplicacion: lo usa tambien ``reintentar`` de la
        revision manual, sobre una operacion que ya tiene fila.
        """
        aplicar = self._aplicador(operation)
        if isinstance(aplicar, str):
            return self._a_revision(operation, aplicar)

        try:
            aplicar()
        except SinManejador as exc:
            self.conn.rollback()
            return self._a_revision(operation, str(exc))
        except sqlite3.IntegrityError as exc:
            # 🔴 Una restriccion violada **por el manejador** no es un duplicado
            # de la operacion: es un dato de dominio que choca (un numero de
            # venta repetido, por ejemplo). Hasta v0.6 esto salia `duplicate` y
            # el nodo la confirmaba: la venta se perdia pareciendo un reintento
            # bien deduplicado. Va a revision.
            self.conn.rollback()
            return self._a_revision(operation, f"el manejador violo una restriccion: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            self.conn.rollback()
            return self._a_revision(operation, str(exc) or type(exc).__name__)

        try:
            self._guardar(operation, InboxStatus.APPLIED, None)
            self.conn.commit()
        except sqlite3.IntegrityError:
            # La carrera de dos pushes del mismo `operation_id`: el otro llego
            # primero a esta fila. Lo que hizo este manejador se deshace.
            self.conn.rollback()
            return PushResult("duplicate")
        return PushResult("accepted")

    def _aplicador(self, operation: OutboxOperation):
        """Lo que hay que correr, o el motivo (texto) por el que no hay nada."""
        if self.despachador is not None:
            manejador = self.despachador.resolver(operation)
            if manejador is None:
                return self.despachador.motivo_sin_manejador(operation)
            return lambda: manejador(self.conn, operation)
        if operation.schema_version != self.supported_schema_version:
            return (f"schema incompatible: {operation.operation_type} "
                    f"v{operation.schema_version}, el central acepta "
                    f"v{self.supported_schema_version}")
        if self.operation_handler is None:
            return lambda: None
        return lambda: self.operation_handler(operation)

    def _en_inbox(self, operation_id: str) -> tuple[str, str | None] | None:
        fila = self.conn.execute(
            "SELECT status, COALESCE(resolucion, last_error) FROM sync_inbox"
            " WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if fila is None else (fila[0], fila[1])

    def _guardar(self, operation: OutboxOperation, estado: InboxStatus,
                 motivo: str | None) -> None:
        """Inserta la fila de ``sync_inbox``, o la actualiza si ya estaba en
        revision (el caso de ``reintentar``). Una fila ``applied`` no se toca:
        el ``WHERE`` del ``DO UPDATE`` es lo que convierte la carrera en un
        ``IntegrityError`` en vez de pisarla."""
        operacion_json = (
            None if estado == InboxStatus.APPLIED
            else json.dumps(operation.como_dict(), sort_keys=True, default=str)
        )
        aplicada = _ahora() if estado == InboxStatus.APPLIED else None
        cursor = self.conn.execute(
            """INSERT INTO sync_inbox
                   (operation_id, applied_at, status, last_error, node_id,
                    operation_type, schema_version, operation_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (operation_id) DO UPDATE SET
                   applied_at = excluded.applied_at, status = excluded.status,
                   last_error = excluded.last_error,
                   operation_json = COALESCE(excluded.operation_json, sync_inbox.operation_json)
               WHERE sync_inbox.status = ?""",
            (operation.operation_id, aplicada, str(estado), motivo,
             operation.node_id, operation.operation_type, operation.schema_version,
             operacion_json, str(InboxStatus.MANUAL_REVIEW)),
        )
        if cursor is not None and cursor.rowcount == 0:
            raise sqlite3.IntegrityError("la operacion ya no esta en revision")

    def _a_revision(self, operation: OutboxOperation, motivo: str) -> PushResult:
        """Guarda la operacion entera en revision manual y contesta ``rejected``.

        Si otro push del mismo ``operation_id`` ya la aplico mientras tanto --la
        carrera de dos reintentos simultaneos--, la fila ya esta y gana esa:
        ``duplicate``.
        """
        try:
            self._guardar(operation, InboxStatus.MANUAL_REVIEW, motivo)
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            previo = self._en_inbox(operation.operation_id)
            if previo is not None and previo[0] == InboxStatus.APPLIED:
                return PushResult("duplicate")
        return PushResult("rejected", motivo)
