"""El nodo se entera solo de que lo que quedo en revision ya se resolvio en el central (v0.25, ADR-028).

Hasta v0.22 una operacion que el central rechazaba quedaba en ``manual_review`` en el outbox del nodo hasta que alguien
corria ``libraedge-nodo revision reenviar <id>`` en esa PC, aunque en el central ya la hubieran reintentado o
descartado.

Tres capas:

1. **El central** (``POST /sync/v1/estado-operaciones``): solo lee, esta autenticada y no deja ver lo de otro nodo.
2. **El worker del nodo**: ritmo, tandas con rotacion, 404 de un central viejo, errores sin romper nada.
3. **El ciclo completo nodo <-> central** con el transporte HTTP de verdad contra el router, que es lo que cuenta:
   aplicada, descartada, la que sigue en revision, un central viejo, el limite por tanda y las operaciones nuevas.

Los de la capa 3 usan dos bases SQLite (la del nodo y la del central), porque son dos procesos. Los de repositorio y
consulta corren contra los dos motores (fixture ``conn``).
"""

import contextlib
import io
import json
import logging
import sqlite3
from urllib.error import HTTPError

import pytest

from libraedge.cli import _entero_blando, cierre_de_revision_del_nodo
from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus
from libraedge.nodo import Nodo
from libraedge.sync import cierre_de_revision as modulo
from libraedge.sync import revision as rev
from libraedge.sync.api import RUTAS_AUTENTICADAS_POR_NODO, create_sync_app, create_sync_router
from libraedge.sync.cierre_de_revision import (
    CentralSinConsultaDeEstado,
    CierreDeRevisionWorker,
    HttpCierreDeRevisionTransport,
)
from libraedge.sync.despacho import Despachador
from libraedge.sync.http import HttpSyncTransport
from libraedge.sync.receiver import SyncReceiver
from libraedge.sync.worker import OutboxWorker

CENTRAL = "https://central.ejemplo"
RECHAZO = "el cliente 77 no existe"


def _operacion(sequence, node_id="node-1", tipo="venta.cobrada"):
    return OutboxOperation(
        operation_id=f"{node_id}:{sequence}", node_id=node_id, sequence=sequence,
        operation_type=tipo, aggregate_type="venta", aggregate_id=f"{node_id}:venta:{sequence}",
        occurred_at="2026-10-26T10:00:00Z", schema_version=1, payload={"total": "16000.00"},
    )


class Producto:
    """El manejador del producto en el central: rechaza hasta que se lo arregla, y cuenta las veces que corre."""

    def __init__(self):
        self.arreglado = False
        self.llamadas: list[str] = []

    def aplicar(self, conexion, operacion):
        self.llamadas.append(operacion.operation_id)
        if not self.arreglado:
            raise ValueError(RECHAZO)

    def despachador(self) -> Despachador:
        despachador = Despachador()
        despachador.registrar("venta.cobrada", 1, self.aplicar)
        return despachador


class Reloj:
    """Un reloj monotonico a mano: el ritmo del worker se prueba sin dormir."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def pasar(self, segundos):
        self.t += segundos


# ── La ruta del central ─────────────────────────────────────────────────────


@pytest.fixture
def central():
    conexion = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conexion)
    yield conexion
    conexion.close()


def _montar(central, producto=None, version_esquema=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    producto = producto or Producto()
    app = FastAPI()
    app.include_router(create_sync_router(
        lambda: contextlib.nullcontext(central), despachador=producto.despachador(),
        version_esquema=version_esquema))
    repo = NodeRepository(central)
    secretos = {n: repo.register_node(n, branch_id="b1") for n in ("node-1", "node-2")}
    return TestClient(app), secretos, producto


def _consultar(cliente, secreto, ids, node_id="node-1", cabeceras=None):
    return cliente.post(modulo.RUTA, json={"node_id": node_id, "operation_ids": ids},
                        headers={"Authorization": f"Bearer {secreto}", **(cabeceras or {})})


def _push(cliente, secreto, operacion):
    return cliente.post("/sync/v1/push", json=operacion.como_dict(),
                        headers={"Authorization": f"Bearer {secreto}"})


def _filas_de_inbox(central):
    return central.execute(
        "SELECT operation_id, status, last_error, received_at, resolucion FROM sync_inbox ORDER BY operation_id"
    ).fetchall()


def test_la_ruta_esta_en_la_lista_de_rutas_del_nodo(central):
    """Los productos suman una fila «PUBLICA» por cada ruta de esa lista en su matriz de roles."""
    router = create_sync_router(lambda: contextlib.nullcontext(central))
    montadas = {(metodo, ruta.path) for ruta in router.routes for metodo in ruta.methods}
    assert ("POST", "/sync/v1/estado-operaciones") in RUTAS_AUTENTICADAS_POR_NODO
    assert set(RUTAS_AUTENTICADAS_POR_NODO) <= montadas


def test_contesta_el_estado_de_aplicada_en_revision_y_descartada(central):
    cliente, secretos, producto = _montar(central)
    for n in (1, 2, 3):
        assert _push(cliente, secretos["node-1"], _operacion(n)).json()["result"] == "rejected"
    producto.arreglado = True
    rev.reintentar(central, "node-1:1", producto.despachador(), "soporte")
    rev.descartar(central, "node-1:3", "era una prueba", "soporte")

    r = _consultar(cliente, secretos["node-1"], ["node-1:1", "node-1:2", "node-1:3", "node-1:99"])

    assert r.status_code == 200, r.text
    operaciones = r.json()["operaciones"]
    assert operaciones["node-1:1"]["estado"] == "applied"
    assert operaciones["node-1:2"] == {"estado": "manual_review", "motivo": RECHAZO}
    assert operaciones["node-1:3"] == {"estado": "discarded", "motivo": "era una prueba"}
    assert "node-1:99" not in operaciones, "lo que el central no conoce no figura"


def test_preguntar_solo_lee(central):
    """🔴 La razon de elegir una consulta y no reenviar por el push: no hay forma de que aplique algo."""
    cliente, secretos, producto = _montar(central)
    _push(cliente, secretos["node-1"], _operacion(1))
    producto.arreglado = True  # si preguntar reprocesara, ahora entraria
    antes = _filas_de_inbox(central)
    llamadas = list(producto.llamadas)

    for _ in range(3):
        r = _consultar(cliente, secretos["node-1"], ["node-1:1", "node-1:2"])
        assert r.json()["operaciones"]["node-1:1"]["estado"] == "manual_review"

    assert producto.llamadas == llamadas, "ningun manejador corrio"
    assert _filas_de_inbox(central) == antes, "ni una fila nueva ni una fila tocada (node-1:2 no se crea)"


def test_no_deja_ver_lo_de_otro_nodo(central):
    cliente, secretos, _ = _montar(central)
    _push(cliente, secretos["node-1"], _operacion(1))

    r = _consultar(cliente, secretos["node-2"], ["node-1:1"], node_id="node-2")

    assert r.status_code == 200
    assert r.json()["operaciones"] == {}


def test_exige_el_secreto_del_nodo(central):
    cliente, secretos, _ = _montar(central)
    cuerpo = {"node_id": "node-1", "operation_ids": ["node-1:1"]}

    assert cliente.post(modulo.RUTA, json=cuerpo).status_code == 401
    assert _consultar(cliente, "un-token-cualquiera", ["node-1:1"]).status_code == 401
    # Suplantacion: el secreto valido de node-2 diciendo ser node-1.
    assert _consultar(cliente, secretos["node-2"], ["node-1:1"]).status_code == 401


@pytest.mark.parametrize("cuerpo", [
    {}, {"node_id": "node-1"}, {"node_id": "node-1", "operation_ids": "node-1:1"},
    {"node_id": "node-1", "operation_ids": []}, {"node_id": "node-1", "operation_ids": [1, 2]},
    {"node_id": "node-1", "operation_ids": [""]},
    {"node_id": "node-1", "operation_ids": [f"node-1:{n}" for n in range(rev.MAXIMO_POR_CONSULTA + 1)]},
    {"operation_ids": ["node-1:1"]},
])
def test_valida_la_forma_y_el_tope_por_consulta(central, cuerpo):
    cliente, secretos, _ = _montar(central)
    r = cliente.post(modulo.RUTA, json=cuerpo, headers={"Authorization": f"Bearer {secretos['node-1']}"})
    assert r.status_code == 422


def test_el_tope_del_central_cubre_la_tanda_del_nodo():
    assert modulo.TANDA_POR_DEFECTO <= rev.MAXIMO_POR_CONSULTA


def test_el_esquema_viejo_es_409(central):
    cliente, secretos, _ = _montar(central, version_esquema="v2")
    assert _consultar(cliente, secretos["node-1"], ["node-1:1"]).status_code == 409
    r = _consultar(cliente, secretos["node-1"], ["node-1:1"], cabeceras={"X-LibraEdge-Esquema": "v2"})
    assert r.status_code == 200


def test_el_receptor_no_reprocesa_un_reenvio_en_ningun_estado(central):
    """Lo que hace hoy el receptor ante un `operation_id` que ya tiene, en cada estado: la evidencia del ADR-028.

    Reenviar por el push seria seguro para lo que esta en `sync_inbox`; lo que lo descarto es que no distingue una
    descartada de una en revision sin leer texto, que sube el payload entero cada vez y que una operacion que el
    central nunca guardo (un 422) se aplicaria por primera vez."""
    producto = Producto()
    receptor = SyncReceiver(central, despachador=producto.despachador())
    assert receptor.accept(_operacion(1)).result == "rejected"
    assert receptor.accept(_operacion(2)).result == "rejected"
    assert receptor.accept(_operacion(3)).result == "rejected"
    producto.arreglado = True
    rev.reintentar(central, "node-1:1", producto.despachador(), "soporte")
    rev.descartar(central, "node-1:3", "era una prueba", "soporte")
    llamadas = list(producto.llamadas)

    aplicada = receptor.accept(_operacion(1))
    en_revision = receptor.accept(_operacion(2))
    descartada = receptor.accept(_operacion(3))

    assert aplicada.result == "duplicate"
    assert en_revision.result == "rejected" and "revision manual" in en_revision.error
    assert descartada.result == "rejected" and "descartada en el central" in descartada.error
    assert producto.llamadas == llamadas, "ningun reenvio volvio a correr un manejador"
    assert len(_filas_de_inbox(central)) == 3, "ni una fila duplicada"
    # ...y lo que el central no tiene, un reenvio SI lo aplica (por eso preguntar no se hace con el push).
    assert receptor.accept(_operacion(4)).result == "accepted"


# ── El repositorio y la consulta, contra los dos motores ────────────────────


def test_ids_en_revision_pagina_en_orden_y_solo_toma_las_de_revision(conn):
    repo = NodeRepository(conn)
    for n in range(1, 6):
        repo.enqueue_operation(_operacion(n))
    for n in (1, 2, 3, 5):
        repo.mark_operation_manual_review(f"node-1:{n}", RECHAZO)
    repo.descartar_operacion("node-1:5", "no va")

    assert repo.ids_en_revision(None, 10) == ["node-1:1", "node-1:2", "node-1:3"]
    assert repo.ids_en_revision(None, 2) == ["node-1:1", "node-1:2"]
    assert repo.ids_en_revision("node-1:2", 2) == ["node-1:3"]
    assert repo.ids_en_revision("node-1:3", 2) == []
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.MANUAL_REVIEW, "solo lee"


def test_estados_de_operaciones_contra_los_dos_motores(conn):
    producto = Producto()
    receptor = SyncReceiver(conn, despachador=producto.despachador())
    producto.arreglado = True
    assert receptor.accept(_operacion(1)).result == "accepted"
    producto.arreglado = False
    assert receptor.accept(_operacion(2)).result == "rejected"
    assert receptor.accept(_operacion(3)).result == "rejected"
    assert receptor.accept(_operacion(4, node_id="node-2")).result == "rejected"
    rev.descartar(conn, "node-1:3", "era una prueba", "soporte")

    estados = rev.estados_de_operaciones(
        conn, "node-1", ["node-1:1", "node-1:2", "node-1:3", "node-2:4", "node-1:99", "node-1:2"])

    assert {k: v["estado"] for k, v in estados.items()} == {
        "node-1:1": "applied", "node-1:2": "manual_review", "node-1:3": "discarded"}
    assert estados["node-1:3"]["motivo"] == "era una prueba"
    assert rev.estados_de_operaciones(conn, "node-1", []) == {}


# ── El worker del nodo ──────────────────────────────────────────────────────


class ConsultaFalsa:
    """Un transporte de consultas que contesta lo que se le diga y anota lo que le preguntan."""

    def __init__(self, respuestas=None, error=None):
        self.respuestas = respuestas or {}
        self.error = error
        self.pedidos: list[list[str]] = []

    def consultar(self, ids):
        self.pedidos.append(list(ids))
        if self.error is not None:
            raise self.error
        return {i: self.respuestas[i] for i in ids if i in self.respuestas}


def _nodo_con_revision(conn, cantidad):
    repo = NodeRepository(conn)
    for n in range(1, cantidad + 1):
        repo.enqueue_operation(_operacion(n))
        repo.mark_operation_manual_review(f"node-1:{n}", RECHAZO)
    return repo


def test_el_ritmo_es_uno_por_ventana_y_cero_lo_apaga(conn):
    repo = _nodo_con_revision(conn, 1)
    reloj, consulta = Reloj(), ConsultaFalsa()
    worker = CierreDeRevisionWorker(repo, consulta, cada_segundos=300, reloj=reloj)

    assert worker.run_once().estado == modulo.CONSULTADA
    reloj.pasar(299)
    assert worker.run_once().estado == modulo.NO_TOCA
    reloj.pasar(2)
    assert worker.run_once().estado == modulo.CONSULTADA
    assert worker.run_once(forzar=True).estado == modulo.CONSULTADA
    assert len(consulta.pedidos) == 3

    apagado = CierreDeRevisionWorker(repo, consulta, cada_segundos=0, reloj=reloj)
    assert apagado.run_once().estado == modulo.APAGADA
    assert len(consulta.pedidos) == 3


def test_sin_nada_en_revision_no_hay_pedido(conn):
    consulta = ConsultaFalsa()
    worker = CierreDeRevisionWorker(NodeRepository(conn), consulta, reloj=Reloj())

    assert worker.run_once().estado == modulo.SIN_NADA
    assert consulta.pedidos == []


def test_las_tandas_rotan_y_recorren_todas_las_que_hay(conn):
    repo = _nodo_con_revision(conn, 7)
    reloj, consulta = Reloj(), ConsultaFalsa()
    worker = CierreDeRevisionWorker(repo, consulta, cada_segundos=60, tanda=3, reloj=reloj)

    for _ in range(4):
        worker.run_once()
        reloj.pasar(61)

    assert consulta.pedidos == [
        ["node-1:1", "node-1:2", "node-1:3"], ["node-1:4", "node-1:5", "node-1:6"], ["node-1:7"],
        ["node-1:1", "node-1:2", "node-1:3"]], "nunca mas de `tanda` por pedido; tras la ultima, vuelve a arrancar"


def test_si_la_tanda_falla_la_ventana_siguiente_repite_la_misma(conn):
    repo = _nodo_con_revision(conn, 4)
    reloj, consulta = Reloj(), ConsultaFalsa(error=TimeoutError("timed out"))
    worker = CierreDeRevisionWorker(repo, consulta, cada_segundos=60, tanda=2, reloj=reloj)

    primero = worker.run_once()
    reloj.pasar(61)
    consulta.error = None
    worker.run_once()

    assert primero.estado == modulo.ERROR and "TimeoutError" in primero.detalle
    assert consulta.pedidos[0] == consulta.pedidos[1] == ["node-1:1", "node-1:2"]


def test_un_error_consume_la_ventana_y_no_se_reintenta_en_el_ciclo_siguiente(conn):
    repo = _nodo_con_revision(conn, 1)
    reloj, consulta = Reloj(), ConsultaFalsa(error=OSError("sin red"))
    worker = CierreDeRevisionWorker(repo, consulta, cada_segundos=300, reloj=reloj)

    assert worker.run_once().estado == modulo.ERROR
    reloj.pasar(60)
    assert worker.run_once().estado == modulo.NO_TOCA
    assert len(consulta.pedidos) == 1, "ni una tormenta de pedidos contra un central caido"


def test_lo_que_contesta_el_central_se_aplica_y_el_resto_queda_como_esta(conn):
    repo = _nodo_con_revision(conn, 5)
    consulta = ConsultaFalsa({
        "node-1:1": {"estado": "applied", "motivo": "reintentada y aplicada"},
        "node-1:2": {"estado": "discarded", "motivo": "era una prueba"},
        "node-1:3": {"estado": "manual_review", "motivo": "sigue"},
        "node-1:4": {"estado": "algo-de-un-futuro", "motivo": None},
        # node-1:5: el central no la conoce
        "node-1:99": {"estado": "applied", "motivo": None},  # no se pregunto: no se le cree
    })
    worker = CierreDeRevisionWorker(repo, consulta, reloj=Reloj())

    r = worker.run_once()

    assert (r.consultadas, r.reenviadas, r.descartadas, r.siguen) == (5, 1, 1, 3)
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.PENDING
    descartada = repo.get_operation("node-1:2")
    assert descartada.status == SyncOperationStatus.DISCARDED
    assert "descartada en el central: era una prueba" in descartada.last_error
    assert f"rechazo del central: {RECHAZO}" in descartada.last_error, "no se pierde el rechazo original"
    for n in (3, 4, 5):
        assert repo.get_operation(f"node-1:{n}").status == SyncOperationStatus.MANUAL_REVIEW
    assert repo.get_operation("node-1:99") is None


def test_si_alguien_la_toco_en_el_nodo_mientras_se_preguntaba_no_se_pisa(conn):
    """El operador corrio `revision reenviar` o `descartar` a mano entre el pedido y la respuesta."""
    repo = _nodo_con_revision(conn, 2)

    class ConsultaLenta(ConsultaFalsa):
        def consultar(self, ids):
            repo.reenviar_operacion("node-1:1")
            repo.descartar_operacion("node-1:2", "a mano")
            return super().consultar(ids)

    consulta = ConsultaLenta({"node-1:1": {"estado": "discarded", "motivo": "x"},
                              "node-1:2": {"estado": "applied", "motivo": None}})
    r = CierreDeRevisionWorker(repo, consulta, reloj=Reloj()).run_once()

    assert r.estado == modulo.CONSULTADA and (r.reenviadas, r.descartadas) == (0, 0)
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.PENDING
    assert "a mano" in repo.get_operation("node-1:2").last_error


def test_un_central_sin_la_ruta_no_rompe_y_se_avisa_una_sola_vez(conn, caplog):
    repo = _nodo_con_revision(conn, 1)
    reloj = Reloj()
    consulta = ConsultaFalsa(error=CentralSinConsultaDeEstado("anterior a v0.25"))
    worker = CierreDeRevisionWorker(repo, consulta, cada_segundos=60, reloj=reloj)

    with caplog.at_level(logging.WARNING, logger=modulo.logger.name):
        primero = worker.run_once()
        reloj.pasar(61)
        segundo = worker.run_once()

    assert primero.estado == segundo.estado == modulo.CENTRAL_SIN_CONSULTA
    assert len(consulta.pedidos) == 2, "vuelve a probar: cuando el central se actualice, se entera solo"
    assert len([r for r in caplog.records if "revision reenviar" in r.getMessage()]) == 1
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.MANUAL_REVIEW


# ── El ciclo completo, nodo <-> central ─────────────────────────────────────


class _Respuesta:
    def __init__(self, cuerpo):
        self._cuerpo = cuerpo

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Mundo:
    """Un nodo (su base, su `Nodo`) y un central (su base, su router) hablandose por el HTTP de verdad.

    `urlopen` de los dos transportes del nodo se desvia al `TestClient` del central: es el mismo codigo que corre en
    una PC, con la forma de errores de urllib. `pedidos` anota cada llamada en orden."""

    def __init__(self, monkeypatch, central_viejo=False, tanda=50, cada_segundos=300):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.central = sqlite3.connect(":memory:", check_same_thread=False)
        init_schema(self.central)
        self.producto = Producto()
        repo_central = NodeRepository(self.central)
        self.secreto = repo_central.register_node("node-1", branch_id="b1")
        if central_viejo:
            # Sin la ruta nueva: `create_sync_app` solo tiene push y pull, como un central de v0.22.
            app = create_sync_app(SyncReceiver(self.central, despachador=self.producto.despachador()), repo_central)
        else:
            app = FastAPI()
            app.include_router(create_sync_router(
                lambda: contextlib.nullcontext(self.central), despachador=self.producto.despachador()))
        self.cliente = TestClient(app)

        self.nodo_db = sqlite3.connect(":memory:", check_same_thread=False)
        init_schema(self.nodo_db)
        self.repo = NodeRepository(self.nodo_db)
        self.reloj = Reloj()
        self.pedidos: list[tuple[str, dict]] = []
        self.falla_la_consulta: Exception | None = None
        self.worker = CierreDeRevisionWorker(
            self.repo, HttpCierreDeRevisionTransport(CENTRAL, "node-1", self.secreto),
            cada_segundos=cada_segundos, tanda=tanda, reloj=self.reloj)
        self.nodo = Nodo(
            self.repo, "node-1",
            outbox_worker=OutboxWorker(self.repo, HttpSyncTransport(CENTRAL, self.secreto)),
            cierre_de_revision_worker=self.worker)
        monkeypatch.setattr("libraedge.sync.http.urlopen", self._urlopen)
        monkeypatch.setattr("libraedge.sync.cierre_de_revision.urlopen", self._urlopen)

    def _urlopen(self, pedido, timeout=None):
        ruta = "/" + pedido.full_url.split("://", 1)[1].split("/", 1)[1]
        cuerpo = json.loads(pedido.data) if pedido.data else {}
        self.pedidos.append((ruta, cuerpo))
        if ruta == modulo.RUTA and self.falla_la_consulta is not None:
            raise self.falla_la_consulta
        respuesta = self.cliente.request(
            pedido.get_method(), ruta, content=pedido.data, headers=dict(pedido.header_items()))
        if respuesta.status_code >= 400:
            raise HTTPError(pedido.full_url, respuesta.status_code, "error", {}, io.BytesIO(respuesta.content))
        return _Respuesta(respuesta.content)

    def rutas(self):
        return [ruta for ruta, _ in self.pedidos]

    def pushes_de(self, operation_id):
        return [c for r, c in self.pedidos if r == "/sync/v1/push" and c["operation_id"] == operation_id]

    def consultas(self):
        return [c["operation_ids"] for r, c in self.pedidos if r == modulo.RUTA]

    def estado(self, operation_id):
        return self.repo.get_operation(operation_id).status

    def con_operaciones_en_revision(self, *numeros):
        """Las encola y hace un ciclo: el central las rechaza y quedan en `manual_review` de los dos lados."""
        for n in numeros:
            self.repo.enqueue_operation(_operacion(n))
        self.nodo.sincronizar()
        for n in numeros:
            assert self.estado(f"node-1:{n}") == SyncOperationStatus.MANUAL_REVIEW
        return self

    def siguiente_ventana(self):
        self.reloj.pasar(301)


@pytest.fixture
def mundo(monkeypatch):
    return Mundo(monkeypatch)


def test_la_aplicada_en_el_central_la_cierra_el_nodo_solo(mundo):
    mundo.con_operaciones_en_revision(1)
    assert mundo.producto.llamadas == ["node-1:1"]
    mundo.producto.arreglado = True
    assert rev.reintentar(mundo.central, "node-1:1", mundo.producto.despachador(), "soporte").result == "accepted"
    assert mundo.producto.llamadas == ["node-1:1", "node-1:1"]

    mundo.siguiente_ventana()
    en_el_ciclo = mundo.nodo.sincronizar()
    # En ese ciclo el nodo se entera y la reencola; el push de la reencolada sale en el ciclo siguiente.
    assert mundo.estado("node-1:1") == SyncOperationStatus.PENDING
    assert en_el_ciclo.pendientes == 1 and en_el_ciclo.en_linea
    final = mundo.nodo.sincronizar()

    operacion = mundo.repo.get_operation("node-1:1")
    assert operacion.status == SyncOperationStatus.ACKNOWLEDGED and operacion.acknowledged_at
    assert final.pendientes == 0 and final.en_linea
    assert mundo.repo.listar_en_revision() == ()
    assert mundo.producto.llamadas == ["node-1:1", "node-1:1"], "el reenvio fue `duplicate`: el manejador no corrio"
    assert len(_filas_de_inbox(mundo.central)) == 1, "sin filas duplicadas en sync_inbox"
    assert [f[1] for f in _filas_de_inbox(mundo.central)] == ["applied"]


def test_la_descartada_en_el_central_la_descarta_el_nodo_solo(mundo):
    mundo.con_operaciones_en_revision(1)
    rev.descartar(mundo.central, "node-1:1", "fue una prueba, no existe", "soporte")

    mundo.siguiente_ventana()
    mundo.nodo.sincronizar()

    operacion = mundo.repo.get_operation("node-1:1")
    assert operacion.status == SyncOperationStatus.DISCARDED
    assert "descartada en el central: fue una prueba, no existe" in operacion.last_error
    assert RECHAZO in operacion.last_error, "el rechazo original no se pierde"
    assert mundo.repo.listar_en_revision() == ()
    mundo.nodo.sincronizar()
    assert len(mundo.pushes_de("node-1:1")) == 1, "una descartada no vuelve a salir"
    assert mundo.producto.llamadas == ["node-1:1"]


def test_la_que_sigue_en_revision_se_queda_y_no_se_reaplica_ni_se_duplica(mundo):
    mundo.con_operaciones_en_revision(1)
    antes = _filas_de_inbox(mundo.central)
    mundo.producto.arreglado = True  # si preguntar la reprocesara, entraria

    for _ in range(3):
        mundo.siguiente_ventana()
        mundo.nodo.sincronizar()

    assert len(mundo.consultas()) == 4, "pregunto una vez por ventana (la primera, en el primer ciclo)"
    assert mundo.estado("node-1:1") == SyncOperationStatus.MANUAL_REVIEW
    assert mundo.repo.get_operation("node-1:1").last_error == f"{RECHAZO}", "queda como esta"
    assert len(mundo.pushes_de("node-1:1")) == 1, "no se volvio a subir"
    assert mundo.producto.llamadas == ["node-1:1"], "el central no la volvio a aplicar"
    assert _filas_de_inbox(mundo.central) == antes, "y su fila de sync_inbox no se movio"


def test_un_central_viejo_sin_la_ruta_sigue_como_hoy(monkeypatch, caplog):
    mundo = Mundo(monkeypatch, central_viejo=True)
    mundo.con_operaciones_en_revision(1)
    mundo.repo.enqueue_operation(_operacion(2, tipo="venta.cobrada"))
    mundo.producto.arreglado = True

    with caplog.at_level(logging.WARNING, logger=modulo.logger.name):
        mundo.siguiente_ventana()
        estado = mundo.nodo.sincronizar()
        mundo.siguiente_ventana()
        mundo.nodo.sincronizar()

    assert estado.en_linea and estado.ultimo_error is None, "un 404 no es estar fuera de linea"
    assert len(mundo.consultas()) == 3, "probo y recibio 404 en cada ventana"
    assert mundo.estado("node-1:1") == SyncOperationStatus.MANUAL_REVIEW, "sigue en revision, como hasta v0.22"
    assert mundo.estado("node-1:2") == SyncOperationStatus.ACKNOWLEDGED, "y las nuevas suben igual"
    assert len([r for r in caplog.records if "revision reenviar" in r.getMessage()]) == 1
    # El camino a mano sigue andando: se reintenta en el central y se reenvia desde el nodo.
    rev.reintentar(mundo.central, "node-1:1", mundo.producto.despachador(), "soporte")
    mundo.repo.reenviar_operacion("node-1:1")
    mundo.nodo.sincronizar()
    assert mundo.estado("node-1:1") == SyncOperationStatus.ACKNOWLEDGED


def test_el_limite_por_tanda_y_la_rotacion_en_el_ciclo_completo(monkeypatch):
    mundo = Mundo(monkeypatch, tanda=3)
    mundo.con_operaciones_en_revision(*range(1, 8))
    # La del final de la lista, la que una tanda fija de 3 no veria nunca.
    rev.descartar(mundo.central, "node-1:7", "era una prueba", "soporte")

    for _ in range(3):
        mundo.siguiente_ventana()
        mundo.nodo.sincronizar()

    consultas = mundo.consultas()
    assert max(len(c) for c in consultas) == 3, "ningun pedido pasa del limite de la tanda"
    assert {i for c in consultas for i in c} == {f"node-1:{n}" for n in range(1, 8)}, "se recorren todas"
    assert mundo.estado("node-1:7") == SyncOperationStatus.DISCARDED
    assert all(mundo.estado(f"node-1:{n}") == SyncOperationStatus.MANUAL_REVIEW for n in range(1, 7))


def test_las_operaciones_nuevas_suben_igual_y_antes_que_la_consulta(mundo):
    mundo.con_operaciones_en_revision(1)
    mundo.producto.arreglado = True
    mundo.repo.enqueue_operation(_operacion(2))
    mundo.pedidos.clear()

    mundo.siguiente_ventana()
    mundo.nodo.sincronizar()

    assert mundo.estado("node-1:2") == SyncOperationStatus.ACKNOWLEDGED
    assert mundo.rutas() == ["/sync/v1/push", modulo.RUTA], "primero sube lo nuevo, despues pregunta"


@pytest.mark.parametrize("falla", [TimeoutError("timed out"), OSError("Network is unreachable"),
                                   HTTPError("u", 500, "boom", {}, io.BytesIO(b""))])
def test_una_consulta_que_falla_no_frena_la_subida_ni_baja_al_nodo_de_linea(mundo, falla):
    mundo.con_operaciones_en_revision(1)
    mundo.producto.arreglado = True  # lo nuevo entra; lo que ya estaba en revision sigue ahi
    mundo.falla_la_consulta = falla

    for n in (2, 3):
        mundo.repo.enqueue_operation(_operacion(n))
        mundo.siguiente_ventana()
        estado = mundo.nodo.sincronizar()
        assert estado.en_linea and estado.ultimo_error is None
        assert mundo.estado(f"node-1:{n}") == SyncOperationStatus.ACKNOWLEDGED

    assert mundo.estado("node-1:1") == SyncOperationStatus.MANUAL_REVIEW


def test_si_el_central_no_contesta_el_push_no_se_pregunta(mundo, monkeypatch):
    """Sin central a la vista (el push fallo por transporte) el ciclo no gasta otro timeout en preguntar."""
    mundo.con_operaciones_en_revision(1)
    mundo.pedidos.clear()
    mundo.repo.enqueue_operation(_operacion(2))

    def sin_red(pedido, timeout=None):
        raise OSError("sin red")

    monkeypatch.setattr("libraedge.sync.http.urlopen", sin_red)
    mundo.siguiente_ventana()
    estado = mundo.nodo.sincronizar()

    assert not estado.en_linea
    assert mundo.consultas() == [], "no hubo pedido de estado"


# ── La configuracion del nodo ───────────────────────────────────────────────


def test_los_valores_por_defecto_y_los_que_no_sirven_no_frenan_el_arranque(conn, capsys):
    repo = NodeRepository(conn)

    por_defecto = cierre_de_revision_del_nodo(repo, "node-1", CENTRAL, "s", None, None, None)
    assert (por_defecto.cada_segundos, por_defecto.tanda) == (300, 50)

    ajustado = cierre_de_revision_del_nodo(repo, "node-1", CENTRAL, "s", None, "60", "10")
    assert (ajustado.cada_segundos, ajustado.tanda) == (60, 10)

    apagado = cierre_de_revision_del_nodo(repo, "node-1", CENTRAL, "s", None, "0", "")
    assert apagado.cada_segundos == 0 and apagado.run_once().estado == modulo.APAGADA
    assert capsys.readouterr().err == ""

    roto = cierre_de_revision_del_nodo(repo, "node-1", CENTRAL, "s", None, "cada tanto", "9999")
    assert (roto.cada_segundos, roto.tanda) == (300, 50)
    avisos = capsys.readouterr().err
    assert "LIBRAEDGE_REVISION_CADA_SEGUNDOS" in avisos and "LIBRAEDGE_REVISION_TANDA" in avisos


def test_entero_blando():
    assert _entero_blando("X", " 7 ", 3, 1, 10) == 7
    assert _entero_blando("X", "-1", 3, 0, 10) == 3
