"""Fase 6: que pasa cuando algo se corta en el peor momento.

El analisis original pedia esto explicitamente: *"la aceptacion debe incluir
pruebas de desastre y recuperacion, no solo una demostracion de desconectar el
cable"*. Desconectar el cable es el caso facil --ya lo cubre `test_nodo.py`--;
los que importan son los cortes **a mitad** de algo.

La pregunta que se repite en cada test es la misma: **si el proceso muere justo
aca, se pierde una venta?** Una venta que el nodo cobro y el central nunca ve no
da error en ningun lado: aparece en el arqueo, dias despues.
"""

import sqlite3

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus
from libraedge.sync.worker import OutboxWorker, PushResult


def operacion(sequence=1):
    return OutboxOperation(
        operation_id=f"node-1:{sequence}", node_id="node-1", sequence=sequence,
        operation_type="pedido.cobrado", aggregate_type="venta",
        aggregate_id=f"node-1:venta:{sequence}", occurred_at="2026-08-30T10:00:00Z",
        schema_version=1, payload={"total": "16000.00"},
    )


class TransporteQueAcepta:
    def __init__(self):
        self.enviadas = []

    def push(self, op):
        self.enviadas.append(op.operation_id)
        return PushResult("accepted")


class TransporteQueMuereDespuesDeEntregar:
    """El central recibio y aplico; el nodo se murio antes de anotarlo.

    Es la ventana mas peligrosa de todo el mecanismo: del lado del central la
    venta existe, del lado del nodo sigue figurando como no enviada.
    """

    def __init__(self, receptor_registro):
        self.registro = receptor_registro

    def push(self, op):
        self.registro.append(op.operation_id)   # el central YA la aplico
        raise KeyboardInterrupt("se corto la luz")


# ── El corte a mitad del envio ───────────────────────────────────────────

def test_una_operacion_colgada_en_enviando_se_vuelve_a_intentar(repo):
    """🔴 El defecto que esta fase encontro.

    `mark_operation_sending()` la pasa a `sending` **antes** de hablar con el
    central. Si el proceso muere ahi --corte de luz, la PC que alguien apago, el
    servicio reiniciado-- la operacion queda en `sending`, y
    `list_pending_operations()` sólo mira `pending` y `retryable_error`.

    O sea que **ningun worker la vuelve a mirar nunca**. La venta que el nodo
    cobro no llega al central y nada falla: se descubre en el arqueo.
    """
    repo.enqueue_operation(operacion())
    # Se simula el crash: quedo marcada como enviando y nadie la confirmo.
    repo.mark_operation_sending("node-1:1")

    # El nodo arranca de nuevo.
    transporte = TransporteQueAcepta()
    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1"], (
        "la operacion quedo colgada en 'sending' y ningun worker la reintenta"
    )
    assert resultado.confirmadas == 1
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED


def test_el_reclamo_no_toca_las_ya_confirmadas(repo):
    """Un cero esperado necesita que el positivo se vea: el test de arriba
    reintenta una, y este confirma que no reintenta las que ya salieron."""
    repo.enqueue_operation(operacion())
    OutboxWorker(repo, TransporteQueAcepta()).run_once()

    transporte = TransporteQueAcepta()
    resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == []
    assert resultado.procesadas == 0


def test_si_el_central_la_aplico_y_el_nodo_murio_no_se_duplica(repo):
    """La otra mitad de la misma ventana, y por que el reintento es seguro.

    El central aplico la venta y el nodo se murio sin anotarlo. Al reintentar,
    el central responde `duplicate` --dedup por `operation_id`-- y el nodo la da
    por confirmada. El efecto en el dominio ocurre **exactamente una vez**, que
    es lo que hace que reintentar a ciegas sea la estrategia correcta.
    """
    aplicadas = []
    repo.enqueue_operation(operacion())

    with pytest.raises(KeyboardInterrupt):
        OutboxWorker(repo, TransporteQueMuereDespuesDeEntregar(aplicadas)).run_once()

    assert aplicadas == ["node-1:1"], "el central la aplico antes del corte"

    class CentralQueYaLaTiene:
        def push(self, op):
            aplicadas.append(op.operation_id)
            return PushResult("duplicate")

    OutboxWorker(repo, CentralQueYaLaTiene()).run_once()

    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED
    # Se mando dos veces, pero el central la materializo una sola: el segundo
    # push volvio 'duplicate' sin tocar el dominio.
    assert aplicadas == ["node-1:1", "node-1:1"]


def test_el_orden_se_respeta_despues_de_un_corte(repo):
    """Las operaciones salen por secuencia aunque se hayan colgado desordenadas.

    El central aplica ventas: si llegaran fuera de orden, un anulacion podria
    materializarse antes que la venta que anula.
    """
    for n in (1, 2, 3):
        repo.enqueue_operation(operacion(sequence=n))
    repo.mark_operation_sending("node-1:2")   # la del medio quedo colgada

    transporte = TransporteQueAcepta()
    OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2", "node-1:3"]


# ── El corte a mitad de la bajada ────────────────────────────────────────

def test_la_bajada_se_retoma_donde_quedo(repo, conn):
    """Un corte en el medio del espejo no puede saltear un cambio.

    El nodo aplica y **despues** avanza el cursor, así que lo que quedó a medias
    se vuelve a pedir. Reaplicar es inofensivo --son upserts-- y saltear no.
    """
    from libraedge.domain.sync import ReferenceChange, ReferenceOperation
    from libraedge.sync.pull import MirrorApplier, PullWorker

    conn.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT)")
    conn.commit()
    repo.register_node("node-1", branch_id="b1")

    cambios = [
        ReferenceChange(cursor=c, table_name="productos", row_id=str(c),
                        operation=ReferenceOperation.UPSERT,
                        payload={"id": c, "nombre": f"p{c}"})
        for c in (1, 2, 3, 4)
    ]

    class TransporteQueSeCorta:
        """Entrega los cambios y muere en el tercero."""

        def __init__(self):
            self.rondas = 0

        def pull(self, cursor=0, limit=500):
            self.rondas += 1
            return tuple(c for c in cambios if c.cursor > cursor)

    class AplicadorQueMuere:
        def __init__(self, real, morir_en):
            self.real = real
            self.morir_en = morir_en

        def aplicar(self, cambio):
            if cambio.cursor == self.morir_en:
                raise KeyboardInterrupt("se corto la luz")
            self.real.aplicar(cambio)

    real = MirrorApplier(conn, {"productos": "id"})
    with pytest.raises(KeyboardInterrupt):
        PullWorker(repo, TransporteQueSeCorta(), AplicadorQueMuere(real, 3)).run_once("node-1")
    conn.commit()

    assert repo.get_server_cursor("node-1") == 2, "el cursor no puede pasar lo aplicado"

    # El nodo arranca de nuevo, con el aplicador sano.
    aplicados = PullWorker(repo, TransporteQueSeCorta(), real).run_once("node-1")
    conn.commit()

    assert aplicados == 2, "tiene que retomar en el 3, no rehacer todo ni saltearlo"
    assert conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 4


# ── El nodo que se pierde entero ─────────────────────────────────────────

def test_lo_que_se_pierde_si_el_disco_del_nodo_muere():
    """Lo unico que existe en un solo lugar es el outbox sin confirmar.

    Este test no verifica codigo: **fija el alcance del desastre**, que es lo que
    hay que saber antes de prometerle continuidad a un cliente. Todo lo demas es
    recuperable porque el central lo tiene o lo puede volver a mandar.
    """
    nodo = sqlite3.connect(":memory:")
    from libraedge.db.schema import init_schema

    init_schema(nodo)
    repo = NodeRepository(nodo)
    repo.register_node("node-1", branch_id="b1")
    repo.enqueue_operation(operacion(sequence=1))
    OutboxWorker(repo, TransporteQueAcepta()).run_once()   # esta ya salio
    repo.enqueue_operation(operacion(sequence=2))          # esta no

    sin_confirmar = [op.operation_id for op in repo.list_pending_operations()]
    assert sin_confirmar == ["node-1:2"], (
        "lo unico irrecuperable si el disco muere son las operaciones que el "
        "central todavia no confirmo"
    )

    # Un nodo nuevo arranca de cero y vuelve a bajar todo: cursor 0.
    reemplazo = sqlite3.connect(":memory:")
    init_schema(reemplazo)
    repo_nuevo = NodeRepository(reemplazo)
    repo_nuevo.register_node("node-1", branch_id="b1")
    assert repo_nuevo.get_server_cursor("node-1") == 0, (
        "el nodo de reemplazo tiene que rehacer el espejo desde el principio"
    )
    assert repo_nuevo.list_pending_operations() == ()

    nodo.close()
    reemplazo.close()


# ── El reloj del nodo ────────────────────────────────────────────────────

def test_el_reloj_desfasado_del_nodo_no_afecta_el_orden(repo):
    """🔴 El reloj de una PC de cliente no es confiable, y no tiene por que serlo.

    Lo que ordena la subida es la **secuencia local**, no `occurred_at`. Si el
    orden dependiera de la hora, una PC con el reloj atrasado mandaria sus
    ventas al pasado y el central las aplicaria fuera de orden.
    """
    ayer = OutboxOperation(
        operation_id="node-1:1", node_id="node-1", sequence=1,
        operation_type="pedido.cobrado", aggregate_type="venta",
        aggregate_id="node-1:venta:1", occurred_at="2020-01-01T00:00:00Z",
        schema_version=1, payload={"total": "1"},
    )
    en_el_futuro = OutboxOperation(
        operation_id="node-1:2", node_id="node-1", sequence=2,
        operation_type="pedido.cobrado", aggregate_type="venta",
        aggregate_id="node-1:venta:2", occurred_at="2099-12-31T23:59:59Z",
        schema_version=1, payload={"total": "2"},
    )
    repo.enqueue_operation(en_el_futuro)
    repo.enqueue_operation(ayer)

    transporte = TransporteQueAcepta()
    OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:1", "node-1:2"], (
        "el orden sale de la secuencia, no del reloj del nodo"
    )
