"""HTTP transport for the offline synchronization worker."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from libraedge.domain.sync import OutboxOperation
from libraedge.sync import esquema
from libraedge.sync.worker import PushResult


class HttpSyncTransport:
    def __init__(self, base_url: str, node_secret: str, timeout: float = 10.0,
                 version_esquema: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_secret = node_secret
        self.timeout = timeout
        #: La version del esquema de la base del nodo (ver `sync/esquema.py`).
        self.version_esquema = version_esquema

    def push(self, operation: OutboxOperation) -> PushResult:
        body = json.dumps({
            "operation_id": operation.operation_id,
            "node_id": operation.node_id,
            "sequence": operation.sequence,
            "operation_type": operation.operation_type,
            "aggregate_type": operation.aggregate_type,
            "aggregate_id": operation.aggregate_id,
            "occurred_at": operation.occurred_at,
            "schema_version": operation.schema_version,
            "payload": operation.payload,
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/sync/v1/push", data=body,
            headers={
                "Content-Type": "application/json",
                **esquema.cabeceras_del_nodo(self.node_secret, self.version_esquema),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except HTTPError as exc:
            # El 409 de esquema sale con un texto que dice que hacer; el worker
            # lo trata igual como reintento: la operacion espera en el outbox.
            esquema.relanzar_si_es_de_esquema(exc)
            if exc.code == 422:
                # El central entendio el pedido y dijo que la operacion no es valida: es un rechazo definitivo, no
                # una falla de transporte. Como excepcion se reintentaria para siempre y, desde v0.17 (ADR-020),
                # cortaria la tanda: todas las ventas de atras quedarian sin subir detras de una que no sube nunca.
                try:
                    detalle = exc.read().decode("utf-8", "replace")[:200]
                except OSError:
                    detalle = ""
                return PushResult("rejected", f"el central la rechazo por invalida (422) {detalle}".strip())
            raise
        return PushResult(data["result"], data.get("error"))
