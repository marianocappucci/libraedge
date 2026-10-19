"""La revision manual del central: listar, reintentar y descartar.

Una operacion que el central no pudo aplicar --sin manejador para su tipo y
version, o rechazada por el manejador del producto-- queda **entera** en
``sync_inbox`` con estado ``manual_review`` y el motivo. Esto es lo que se hace
con ella despues, y es generico: no sabe que es una venta.

- ``listar_en_revision``: lo que espera a alguien, con el motivo.
- ``reintentar``: la vuelve a pasar por el despachador (p. ej. despues de
  desplegar el manejador que faltaba, o de corregir el dato que chocaba). Si
  ahora entra, queda ``applied``; si no, sigue en revision con el motivo nuevo.
- ``descartar``: la cierra sin aplicarla, con **motivo y quien**. No se borra:
  la fila queda como constancia de que esa operacion existio y por que no se
  aplico.

Del lado del nodo la operacion quedo en ``manual_review`` en su outbox. Despues
de reintentarla aca, ``libraedge-nodo revision reenviar <id>`` la vuelve a
mandar y el central contesta ``duplicate``: el nodo la confirma y sale de su
bandeja.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from libraedge.domain.sync import InboxStatus, OutboxOperation
from libraedge.sync.receiver import SyncReceiver
from libraedge.sync.worker import PushResult


@dataclass(frozen=True)
class OperacionEnRevision:
    operation_id: str
    node_id: str | None
    operation_type: str | None
    schema_version: int | None
    estado: str
    motivo: str | None
    recibida_at: str | None
    resuelto_por: str | None = None
    resuelto_at: str | None = None
    resolucion: str | None = None


_COLUMNAS = ("operation_id, node_id, operation_type, schema_version, status,"
             " last_error, received_at, resuelto_por, resuelto_at, resolucion")


def _fila(fila) -> OperacionEnRevision:
    return OperacionEnRevision(
        operation_id=fila[0], node_id=fila[1], operation_type=fila[2],
        schema_version=fila[3], estado=fila[4], motivo=fila[5],
        recibida_at=None if fila[6] is None else str(fila[6]),
        resuelto_por=fila[7], resuelto_at=fila[8], resolucion=fila[9],
    )


def listar_en_revision(conn, node_id: str | None = None,
                       incluir_descartadas: bool = False) -> list[OperacionEnRevision]:
    """Las operaciones en revision manual, de la mas vieja a la mas nueva."""
    estados = [str(InboxStatus.MANUAL_REVIEW)]
    if incluir_descartadas:
        estados.append(str(InboxStatus.DISCARDED))
    marcas = ", ".join("?" for _ in estados)
    sql = f"SELECT {_COLUMNAS} FROM sync_inbox WHERE status IN ({marcas})"
    parametros: list = list(estados)
    if node_id is not None:
        sql += " AND node_id = ?"
        parametros.append(node_id)
    sql += " ORDER BY received_at, operation_id"
    return [_fila(fila) for fila in conn.execute(sql, tuple(parametros)).fetchall()]


def obtener(conn, operation_id: str) -> OperacionEnRevision | None:
    fila = conn.execute(
        f"SELECT {_COLUMNAS} FROM sync_inbox WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    return None if fila is None else _fila(fila)


def _operacion_guardada(conn, operation_id: str) -> OutboxOperation:
    fila = conn.execute(
        "SELECT status, operation_json FROM sync_inbox WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if fila is None:
        raise LookupError(f"no hay ninguna operacion {operation_id!r} en el central")
    if fila[0] != InboxStatus.MANUAL_REVIEW:
        raise ValueError(f"la operacion {operation_id!r} no esta en revision (esta {fila[0]})")
    if not fila[1]:
        raise ValueError(f"la operacion {operation_id!r} no tiene guardado su contenido")
    datos = json.loads(fila[1])
    return OutboxOperation(
        operation_id=datos["operation_id"], node_id=datos["node_id"],
        sequence=int(datos["sequence"]), operation_type=datos["operation_type"],
        aggregate_type=datos["aggregate_type"], aggregate_id=datos["aggregate_id"],
        occurred_at=datos["occurred_at"], schema_version=int(datos["schema_version"]),
        payload=datos["payload"],
    )


def _anotar(conn, operation_id: str, por: str, resolucion: str | None) -> None:
    conn.execute(
        "UPDATE sync_inbox SET resuelto_por = ?, resuelto_at = ?, resolucion = ?"
        " WHERE operation_id = ?",
        (por, datetime.now(UTC).isoformat(), resolucion, operation_id),
    )
    conn.commit()


def _exigir_quien(por: str) -> str:
    por = (por or "").strip()
    if not por:
        raise ValueError("hace falta decir quien resuelve la revision")
    return por


def reintentar(conn, operation_id: str, despachador, por: str) -> PushResult:
    """Vuelve a aplicar una operacion en revision con ``despachador``.

    ``despachador`` es un ``Despachador`` (o cualquier invocable
    ``(conexion, operacion)``, como el manejador unico de antes). Devuelve el
    ``PushResult``: ``accepted`` si entro, ``rejected`` con el motivo nuevo si no.
    """
    por = _exigir_quien(por)
    operacion = _operacion_guardada(conn, operation_id)
    from libraedge.sync.despacho import Despachador

    if isinstance(despachador, Despachador):
        receptor = SyncReceiver(conn, despachador=despachador)
    else:
        receptor = SyncReceiver(
            conn, operation_handler=lambda op: despachador(conn, op),
            supported_schema_version=operacion.schema_version,
        )
    resultado = receptor.aplicar(operacion)
    # Solo se anota quien la resolvio si se resolvio: un reintento que vuelve a
    # fallar deja el motivo nuevo en `last_error` y la operacion sigue abierta.
    if resultado.result == "accepted":
        _anotar(conn, operation_id, por, "reintentada y aplicada")
    return resultado


def descartar(conn, operation_id: str, motivo: str, por: str) -> None:
    """Cierra una operacion en revision sin aplicarla. Motivo y quien, obligatorios."""
    por = _exigir_quien(por)
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("hace falta un motivo para descartar una operacion")
    _operacion_guardada(conn, operation_id)
    conn.execute(
        "UPDATE sync_inbox SET status = ?, resuelto_por = ?, resuelto_at = ?, resolucion = ?"
        " WHERE operation_id = ? AND status = ?",
        (str(InboxStatus.DISCARDED), por, datetime.now(UTC).isoformat(), motivo,
         operation_id, str(InboxStatus.MANUAL_REVIEW)),
    )
    conn.commit()
