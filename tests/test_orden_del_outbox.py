"""El orden causal del outbox (v0.17, ADR-020): una venta no sube antes que lo que ella necesita.

Hasta v0.16 el worker hacia `continue` ante un fallo de transporte: con la red intermitente la operacion que fallaba
(un `cliente.alta`, un `turno.abierto`) quedaba para el ciclo siguiente y la venta que dependia de ella subia primero.
Corre contra los dos motores (fixture `conn`).
"""

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus
from libraedge.sync.worker import OutboxWorker, PushResult


def operacion(sequence, tipo="pedido.cobrado"):
    return OutboxOperation(
        operation_id=f"node-1:{sequence}", node_id="node-1", sequence=sequence,
        operation_type=tipo, aggregate_type="venta" if tipo == "pedido.cobrado" else "cliente",
        aggregate_id=f"node-1:{sequence}", occurred_at="2026-10-21T10:00:00Z",
        schema_version=1, payload={"n": sequence},
    )


class TransporteGuionado:
    """Contesta lo que diga el guion para cada `operation_id` (por defecto, `accepted`) y anota lo que le llega.

    El guion es una lista por operacion, que se va consumiendo: `[ConnectionError, "accepted"]` falla la primera vez
    que se la manda y la acepta la segunda."""

    def __init__(self, guion=None):
        self.guion = {clave: list(valor) for clave, valor in (guion or {}).items()}
        self.enviadas = []

    def push(self, op):
        self.enviadas.append(op.operation_id)
        pasos = self.guion.get(op.operation_id)
        paso = pasos.pop(0) if pasos else "accepted"
        if isinstance(paso, Exception):
            raise paso
        if paso == "rejected":
            return PushResult("rejected", "el cliente no existe")
        return PushResult(paso)


def _estados(repo, n):
    return [repo.get_operation(f"node-1:{i}").status for i in range(1, n + 1)]


def test_si_falla_la_primera_la_venta_no_la_pasa(conn):
    """El caso del defecto: `cliente.alta` falla por la red y la venta que lo usa NO sale antes."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(operacion(1, "cliente.alta"))
    repo.enqueue_operation(operacion(2))
    repo.enqueue_operation(operacion(3))
    transporte = TransporteGuionado({"node-1:1": [ConnectionError("sin red")]})

    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1"], "al fallar la primera, la tanda se corta: nada posterior sale"
    assert (resultado.procesadas, resultado.fallidas, resultado.confirmadas) == (1, 1, 0)
    assert resultado.hubo_falla_de_transporte is True
    assert "sin red" in resultado.ultimo_error
    assert _estados(repo, 3) == [
        SyncOperationStatus.RETRYABLE_ERROR, SyncOperationStatus.PENDING, SyncOperationStatus.PENDING]
    # Las que no se intentaron no gastan un intento: no se las mando.
    assert [repo.get_operation(f"node-1:{i}").attempts for i in (1, 2, 3)] == [1, 0, 0]


def test_al_volver_la_red_salen_en_orden_de_secuencia(conn):
    repo = NodeRepository(conn)
    for n, tipo in ((1, "turno.abierto"), (2, "cliente.alta"), (3, "pedido.cobrado")):
        repo.enqueue_operation(operacion(n, tipo))
    transporte = TransporteGuionado({"node-1:1": [ConnectionError("sin red")]})
    worker = OutboxWorker(repo, transporte)

    worker.run_once()          # falla la 1 y se corta
    resultado = worker.run_once()   # vuelve la red

    assert transporte.enviadas == ["node-1:1", "node-1:1", "node-1:2", "node-1:3"]
    assert resultado.confirmadas == 3 and resultado.hubo_falla_de_transporte is False
    assert set(_estados(repo, 3)) == {SyncOperationStatus.ACKNOWLEDGED}


def test_si_falla_la_del_medio_se_confirman_las_anteriores_y_se_cortan_las_posteriores(conn):
    repo = NodeRepository(conn)
    for n in (1, 2, 3, 4):
        repo.enqueue_operation(operacion(n))
    transporte = TransporteGuionado({"node-1:2": [TimeoutError("lento")]})

    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2"]
    assert (resultado.procesadas, resultado.confirmadas, resultado.fallidas) == (2, 1, 1)
    assert _estados(repo, 4) == [
        SyncOperationStatus.ACKNOWLEDGED, SyncOperationStatus.RETRYABLE_ERROR,
        SyncOperationStatus.PENDING, SyncOperationStatus.PENDING]


def test_una_retryable_vieja_sale_antes_que_las_pendientes_nuevas(conn):
    """Lo que fallo en un ciclo y lo que se encolo despues salen mezclados en el orden de secuencia, no por estado."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(operacion(1, "cliente.alta"))
    OutboxWorker(repo, TransporteGuionado({"node-1:1": [ConnectionError("x")]})).run_once()
    repo.enqueue_operation(operacion(2))   # se cobro otra venta con el nodo sin red

    transporte = TransporteGuionado()
    OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2"]


def test_un_rechazo_definitivo_no_traba_la_cola(conn):
    """`rejected` es una respuesta del central, no una falla: va a revision manual y la tanda sigue."""
    repo = NodeRepository(conn)
    for n in (1, 2, 3):
        repo.enqueue_operation(operacion(n))
    transporte = TransporteGuionado({"node-1:1": ["rejected"]})

    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2", "node-1:3"]
    assert (resultado.procesadas, resultado.rechazadas, resultado.confirmadas) == (3, 1, 2)
    assert resultado.hubo_falla_de_transporte is False, "un rechazo no es un corte: el nodo sigue en linea"
    assert _estados(repo, 3) == [
        SyncOperationStatus.MANUAL_REVIEW, SyncOperationStatus.ACKNOWLEDGED, SyncOperationStatus.ACKNOWLEDGED]
    assert [o.operation_id for o in repo.listar_en_revision()] == ["node-1:1"]
    assert repo.get_operation("node-1:1").last_error == "el cliente no existe"


def test_un_rechazo_seguido_de_un_fallo_corta_en_el_fallo(conn):
    repo = NodeRepository(conn)
    for n in (1, 2, 3):
        repo.enqueue_operation(operacion(n))
    transporte = TransporteGuionado({"node-1:1": ["rejected"], "node-1:2": [ConnectionError("x")]})

    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2"]
    assert (resultado.rechazadas, resultado.fallidas) == (1, 1)
    assert _estados(repo, 3) == [
        SyncOperationStatus.MANUAL_REVIEW, SyncOperationStatus.RETRYABLE_ERROR, SyncOperationStatus.PENDING]


# ── el 422 del central es un rechazo, no un corte ───────────────────────────

def _http_error(codigo, cuerpo=b'{"detail": "invalid operation"}'):
    import io
    from urllib.error import HTTPError

    return HTTPError("https://central.example/sync/v1/push", codigo, "x", {}, io.BytesIO(cuerpo))


def test_un_422_del_central_va_a_revision_y_no_traba_la_cola(conn, monkeypatch):
    """Sin esto, una operacion que el central nunca va a aceptar cortaria la tanda en cada ciclo, para siempre."""
    from libraedge.sync import http as http_module

    repo = NodeRepository(conn)
    for n in (1, 2):
        repo.enqueue_operation(operacion(n))
    enviadas = []

    def urlopen(request, timeout):
        import json

        operacion_id = json.loads(request.data)["operation_id"]
        enviadas.append(operacion_id)
        if operacion_id == "node-1:1":
            raise _http_error(422)

        class Respuesta:
            def read(self):
                return b'{"result": "accepted", "error": null}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Respuesta()

    monkeypatch.setattr(http_module, "urlopen", urlopen)

    resultado = OutboxWorker(repo, http_module.HttpSyncTransport("https://central.example", "s")).run_once()

    assert enviadas == ["node-1:1", "node-1:2"]
    assert (resultado.rechazadas, resultado.confirmadas, resultado.fallidas) == (1, 1, 0)
    assert _estados(repo, 2) == [SyncOperationStatus.MANUAL_REVIEW, SyncOperationStatus.ACKNOWLEDGED]
    assert "422" in repo.get_operation("node-1:1").last_error


@pytest.mark.parametrize("codigo", [401, 404, 500, 502, 503])
def test_otros_errores_http_siguen_siendo_reintentables(conn, monkeypatch, codigo):
    from libraedge.sync import http as http_module

    repo = NodeRepository(conn)
    repo.enqueue_operation(operacion(1))

    def urlopen(request, timeout):
        raise _http_error(codigo)

    monkeypatch.setattr(http_module, "urlopen", urlopen)

    resultado = OutboxWorker(repo, http_module.HttpSyncTransport("https://central.example", "s")).run_once()

    assert resultado.fallidas == 1 and resultado.rechazadas == 0
    assert _estados(repo, 1) == [SyncOperationStatus.RETRYABLE_ERROR]
