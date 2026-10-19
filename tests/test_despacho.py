"""Despacho por tipo y version, revision manual y control de esquema (v0.7).

Contra los dos motores (fixture `conn`), salvo lo que es del cable HTTP.
"""

import contextlib
import json
import sqlite3

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import InboxStatus, OutboxOperation, SyncOperationStatus
from libraedge.sync import esquema
from libraedge.sync import revision as rev
from libraedge.sync.despacho import Despachador, SinManejador
from libraedge.sync.receiver import SyncReceiver
from libraedge.sync.worker import OutboxWorker


def _op(sequence=1, tipo="venta.cobrada", version=1, node_id="node-1", payload=None):
    return OutboxOperation(
        operation_id=f"{node_id}:{sequence}", node_id=node_id, sequence=sequence,
        operation_type=tipo, aggregate_type="venta",
        aggregate_id=f"{node_id}:venta:{sequence}", occurred_at="2026-10-19T12:00:00Z",
        schema_version=version, payload=payload or {"total": "1500.00"},
    )


def _estado_inbox(conn, operation_id):
    fila = conn.execute(
        "SELECT status, last_error, operation_json FROM sync_inbox WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return None if fila is None else (fila[0], fila[1], fila[2])


# ── El despachador ──────────────────────────────────────────────────────────

def test_el_despachador_resuelve_por_tipo_y_version():
    despachador = Despachador()
    v1 = despachador.registrar("venta.cobrada", 1, lambda c, o: None)

    @despachador.manejador("venta.cobrada", 2)
    def v2(conexion, operacion):
        return None

    assert despachador.resolver(_op(version=1)) is v1
    assert despachador.resolver(_op(version=2)) is v2
    assert despachador.resolver(_op(version=3)) is None
    assert despachador.resolver(_op(tipo="venta.anulada")) is None
    assert despachador.versiones("venta.cobrada") == (1, 2)
    assert despachador.tipos() == ("venta.cobrada",)
    assert len(despachador) == 2


def test_registrar_dos_veces_el_mismo_par_es_un_error():
    """Pisar un manejador en silencio dejaria al primero muerto."""
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    with pytest.raises(ValueError, match="ya hay un manejador"):
        despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    with pytest.raises(ValueError):
        despachador.registrar("", 1, lambda c, o: None)


def test_el_despachador_llamado_directo_levanta_sin_manejador():
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    with pytest.raises(SinManejador, match="el central conoce v1"):
        despachador(None, _op(version=2))
    with pytest.raises(SinManejador, match="no conoce ese tipo"):
        despachador(None, _op(tipo="otra.cosa"))


# ── El receptor con despachador ─────────────────────────────────────────────

def test_una_operacion_de_tipo_desconocido_no_queda_applied(conn):
    """🔴 El agujero que cierra v0.7.

    Con el manejador unico, un tipo que el central no conocia volvia del
    manejador sin error --su `if operation_type != ...: return`-- y el receptor
    lo anotaba `applied`: el nodo la confirmaba y se perdia en silencio.
    """
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    receptor = SyncReceiver(conn, despachador=despachador)

    resultado = receptor.accept(_op(tipo="venta.anulada"))

    assert resultado.result == "rejected"
    assert "venta.anulada" in resultado.error
    estado, motivo, guardada = _estado_inbox(conn, "node-1:1")
    assert estado == InboxStatus.MANUAL_REVIEW
    assert "no conoce ese tipo" in motivo
    assert json.loads(guardada)["operation_type"] == "venta.anulada", (
        "el central tiene que guardar la operacion entera para poder reintentarla")


def test_conviven_dos_versiones_del_mismo_tipo(conn):
    aplicadas = []
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: aplicadas.append(("v1", o.sequence)))
    despachador.registrar("venta.cobrada", 2, lambda c, o: aplicadas.append(("v2", o.sequence)))
    receptor = SyncReceiver(conn, despachador=despachador)

    assert receptor.accept(_op(sequence=1, version=1)).result == "accepted"
    assert receptor.accept(_op(sequence=2, version=2)).result == "accepted"
    assert aplicadas == [("v1", 1), ("v2", 2)]

    rechazada = receptor.accept(_op(sequence=3, version=3))
    assert rechazada.result == "rejected"
    assert "el central conoce v1, v2" in rechazada.error


def test_el_manejador_recibe_la_conexion_del_receptor(conn):
    recibidas = []
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: recibidas.append(c))
    SyncReceiver(conn, despachador=despachador).accept(_op())
    assert recibidas == [conn]


def test_reenviar_una_operacion_en_revision_no_la_reprocesa(conn):
    """Un push repetido de algo en revision contesta `rejected` con el motivo:
    reintentarla es una decision de quien revisa, no del reintento del nodo."""
    llamadas = []

    def rechaza(conexion, operacion):
        llamadas.append(1)
        raise ValueError("el cliente 77 no existe")

    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, rechaza)
    receptor = SyncReceiver(conn, despachador=despachador)

    primera = receptor.accept(_op())
    segunda = receptor.accept(_op())

    assert primera.result == segunda.result == "rejected"
    assert "el cliente 77 no existe" in segunda.error
    assert "revision manual" in segunda.error
    assert llamadas == [1]


def test_una_restriccion_violada_por_el_manejador_va_a_revision_y_no_es_duplicate(conn):
    """🔴 Hasta v0.6 esto salia `duplicate` y el nodo la confirmaba.

    Un numero de venta repetido entre dos nodos no es un reintento: es un dato
    que choca. Y la conexion tiene que quedar usable --en PostgreSQL la sentencia
    fallida aborta la transaccion--.
    """
    conn.execute("CREATE TABLE ventas_demo (numero TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO ventas_demo (numero) VALUES (?)", ("V-1",))
    conn.commit()

    def inserta(conexion, operacion):
        conexion.execute("INSERT INTO ventas_demo (numero) VALUES (?)",
                         (operacion.payload["numero"],))

    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, inserta)
    receptor = SyncReceiver(conn, despachador=despachador)

    choque = receptor.accept(_op(payload={"numero": "V-1"}))
    assert choque.result == "rejected"
    assert "restriccion" in choque.error
    assert _estado_inbox(conn, "node-1:1")[0] == InboxStatus.MANUAL_REVIEW

    sana = receptor.accept(_op(sequence=2, payload={"numero": "V-2"}))
    assert sana.result == "accepted"


def test_la_carrera_con_otro_push_que_ya_la_aplico_es_duplicate(conn):
    """Dos reintentos simultaneos del mismo `operation_id`: si el otro la
    aplico mientras este fallaba, gana el aplicado y este contesta duplicate."""
    def aplicada_por_el_otro_y_choca(conexion, operacion):
        conexion.execute(
            "INSERT INTO sync_inbox (operation_id, applied_at, status) VALUES (?, ?, ?)",
            (operacion.operation_id, "2026-10-19T12:00:01Z", "applied"))
        conexion.commit()
        raise ValueError("choco con lo que el otro ya escribio")

    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, aplicada_por_el_otro_y_choca)
    resultado = SyncReceiver(conn, despachador=despachador).accept(_op())

    assert resultado.result == "duplicate"
    assert _estado_inbox(conn, "node-1:1")[0] == InboxStatus.APPLIED


# ── Compatibilidad con el manejador unico ───────────────────────────────────

def test_el_manejador_unico_sigue_funcionando(conn):
    aplicadas = []
    receptor = SyncReceiver(conn, operation_handler=aplicadas.append)
    assert receptor.accept(_op()).result == "accepted"
    assert receptor.accept(_op()).result == "duplicate"
    assert len(aplicadas) == 1
    assert _estado_inbox(conn, "node-1:1")[0] == InboxStatus.APPLIED


def test_con_el_manejador_unico_una_version_distinta_queda_en_revision(conn):
    receptor = SyncReceiver(conn, operation_handler=lambda op: None,
                            supported_schema_version=1)
    resultado = receptor.accept(_op(version=2))
    assert resultado.result == "rejected"
    assert "schema incompatible" in resultado.error
    assert _estado_inbox(conn, "node-1:1")[0] == InboxStatus.MANUAL_REVIEW


# ── El nodo lo ve ───────────────────────────────────────────────────────────

class _TransporteDirecto:
    """El push sin HTTP: derecho al receptor del central."""

    def __init__(self, receptor):
        self.receptor = receptor

    def push(self, operacion):
        return self.receptor.accept(operacion)


def test_el_nodo_pasa_a_revision_lo_que_el_central_no_conoce(conn):
    """El nodo y el central en la misma base, como en los tests del piloto."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(_op(sequence=1, tipo="venta.cobrada"))
    repo.enqueue_operation(_op(sequence=2, tipo="venta.anulada"))
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)

    resultado = OutboxWorker(repo, _TransporteDirecto(
        SyncReceiver(conn, despachador=despachador))).run_once()

    assert resultado.confirmadas == 1
    assert resultado.rechazadas == 1
    en_revision = repo.listar_en_revision()
    assert [op.operation_id for op in en_revision] == ["node-1:2"]
    assert "venta.anulada" in en_revision[0].last_error


# ── La revision manual en el central ────────────────────────────────────────

@pytest.fixture
def con_una_en_revision(conn):
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    receptor = SyncReceiver(conn, despachador=despachador)
    assert receptor.accept(_op(sequence=1, tipo="venta.anulada")).result == "rejected"
    assert receptor.accept(_op(sequence=1, node_id="node-2", tipo="venta.anulada")).result == "rejected"
    assert receptor.accept(_op(sequence=2)).result == "accepted"
    return conn


def test_listar_en_revision(con_una_en_revision):
    todas = rev.listar_en_revision(con_una_en_revision)
    assert sorted(op.operation_id for op in todas) == ["node-1:1", "node-2:1"]
    assert all(op.estado == InboxStatus.MANUAL_REVIEW for op in todas)
    assert all("venta.anulada" in op.motivo for op in todas)

    del_nodo = rev.listar_en_revision(con_una_en_revision, node_id="node-2")
    assert [op.operation_id for op in del_nodo] == ["node-2:1"]
    assert del_nodo[0].operation_type == "venta.anulada"
    assert del_nodo[0].schema_version == 1


def test_reintentar_con_el_manejador_nuevo_la_aplica(con_una_en_revision):
    conn = con_una_en_revision
    aplicadas = []
    despachador = Despachador()
    despachador.registrar("venta.anulada", 1, lambda c, o: aplicadas.append(o))

    resultado = rev.reintentar(conn, "node-1:1", despachador, por="soporte@ejemplo.test")

    assert resultado.result == "accepted"
    assert aplicadas[0].payload == {"total": "1500.00"}
    operacion = rev.obtener(conn, "node-1:1")
    assert operacion.estado == InboxStatus.APPLIED
    assert operacion.resuelto_por == "soporte@ejemplo.test"
    assert operacion.resuelto_at is not None
    # Y el nodo, al reenviarla, recibe duplicate: la confirma.
    receptor = SyncReceiver(conn, despachador=despachador)
    assert receptor.accept(_op(sequence=1, tipo="venta.anulada")).result == "duplicate"
    assert [op.operation_id for op in rev.listar_en_revision(conn)] == ["node-2:1"]


def test_reintentar_con_un_manejador_suelto(con_una_en_revision):
    """La API de antes: un invocable `(conexion, operacion)` en vez del despachador."""
    aplicadas = []
    resultado = rev.reintentar(con_una_en_revision, "node-1:1",
                               lambda c, o: aplicadas.append(o), por="soporte")
    assert resultado.result == "accepted"
    assert len(aplicadas) == 1


def test_un_reintento_que_vuelve_a_fallar_sigue_en_revision(con_una_en_revision):
    conn = con_una_en_revision

    def todavia_no(conexion, operacion):
        raise ValueError("la venta original no esta en el central")

    despachador = Despachador()
    despachador.registrar("venta.anulada", 1, todavia_no)
    resultado = rev.reintentar(conn, "node-1:1", despachador, por="soporte")

    assert resultado.result == "rejected"
    operacion = rev.obtener(conn, "node-1:1")
    assert operacion.estado == InboxStatus.MANUAL_REVIEW
    assert operacion.motivo == "la venta original no esta en el central"
    assert operacion.resuelto_por is None, "no se resolvio: no se anota quien"


def test_descartar_exige_motivo_y_quien(con_una_en_revision):
    conn = con_una_en_revision
    with pytest.raises(ValueError, match="motivo"):
        rev.descartar(conn, "node-1:1", motivo=" ", por="soporte")
    with pytest.raises(ValueError, match="quien"):
        rev.descartar(conn, "node-1:1", motivo="venta de prueba", por="")
    with pytest.raises(ValueError, match="quien"):
        rev.reintentar(conn, "node-1:1", Despachador(), por="")


def test_descartar_la_cierra_y_el_nodo_lo_ve(con_una_en_revision):
    conn = con_una_en_revision
    rev.descartar(conn, "node-1:1", motivo="venta de prueba en el local", por="soporte")

    assert [op.operation_id for op in rev.listar_en_revision(conn)] == ["node-2:1"]
    todas = rev.listar_en_revision(conn, incluir_descartadas=True)
    descartada = next(op for op in todas if op.operation_id == "node-1:1")
    assert descartada.estado == InboxStatus.DISCARDED
    assert descartada.resolucion == "venta de prueba en el local"
    assert descartada.resuelto_por == "soporte"

    repetida = SyncReceiver(conn, despachador=Despachador()).accept(
        _op(sequence=1, tipo="venta.anulada"))
    assert repetida.result == "rejected"
    assert "descartada en el central: venta de prueba en el local" in repetida.error

    with pytest.raises(ValueError, match="no esta en revision"):
        rev.reintentar(conn, "node-1:1", Despachador(), por="soporte")


def test_reintentar_o_descartar_algo_que_no_existe(conn):
    with pytest.raises(LookupError):
        rev.reintentar(conn, "node-9:1", Despachador(), por="soporte")
    with pytest.raises(LookupError):
        rev.descartar(conn, "node-9:1", motivo="x", por="soporte")
    assert rev.obtener(conn, "node-9:1") is None


def test_una_aplicada_no_se_puede_reintentar(con_una_en_revision):
    with pytest.raises(ValueError, match="no esta en revision"):
        rev.reintentar(con_una_en_revision, "node-1:2", Despachador(), por="soporte")


# ── El nodo: reenviar despues de resolver ───────────────────────────────────

def test_el_nodo_reenvia_y_confirma_despues_de_resolver_en_el_central(conn):
    """El circuito entero: rechazo, se resuelve en el central, el nodo reenvia."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(_op(tipo="venta.anulada"))
    despachador = Despachador()
    worker = OutboxWorker(repo, _TransporteDirecto(SyncReceiver(conn, despachador=despachador)))
    assert worker.run_once().rechazadas == 1

    despachador.registrar("venta.anulada", 1, lambda c, o: None)
    assert rev.reintentar(conn, "node-1:1", despachador, por="soporte").result == "accepted"

    repo.reenviar_operacion("node-1:1")
    assert worker.run_once().confirmadas == 1
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.ACKNOWLEDGED
    assert repo.listar_en_revision() == ()

    with pytest.raises(LookupError):
        repo.reenviar_operacion("node-1:1")


# ── La version del esquema ──────────────────────────────────────────────────

def test_version_de_alembic_lee_todas_las_cadenas(conn):
    assert esquema.version_de_alembic(conn) is None, "sin Alembic no hay version"
    conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
    conn.execute("CREATE TABLE alembic_version_producto (version_num TEXT NOT NULL)")
    conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", ("0027_motor",))
    conn.execute("INSERT INTO alembic_version_producto (version_num) VALUES (?)", ("0006_b",))
    conn.execute("INSERT INTO alembic_version_producto (version_num) VALUES (?)", ("0006_a",))
    conn.commit()
    assert esquema.version_de_alembic(conn) == (
        "alembic_version=0027_motor;alembic_version_producto=0006_a,0006_b")


def test_resolver_la_version():
    assert esquema.resolver(None, None) is None
    assert esquema.resolver("v7", None) == "v7"
    assert esquema.resolver(lambda c: f"de {c}", "la base") == "de la base"


def _central_sqlite():
    conexion = sqlite3.connect(":memory:", check_same_thread=False)
    from libraedge.db.changelog import init_changelog_schema
    from libraedge.db.schema import init_schema

    init_schema(conexion)
    init_changelog_schema(conexion)
    conexion.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
    conexion.execute("INSERT INTO alembic_version (version_num) VALUES ('0027')")
    conexion.commit()
    return conexion


def _payload(**cambios):
    return {**_op(**cambios).como_dict()}


def _router(conexion, **kwargs):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from libraedge.sync.api import create_sync_router

    app = FastAPI()
    app.include_router(create_sync_router(lambda: contextlib.nullcontext(conexion), **kwargs))
    secreto = NodeRepository(conexion).register_node("node-1", branch_id="b1")
    return TestClient(app), secreto


def test_el_router_con_despachador_rechaza_lo_desconocido():
    central = _central_sqlite()
    despachador = Despachador()
    despachador.registrar("venta.cobrada", 1, lambda c, o: None)
    cliente, secreto = _router(central, despachador=despachador)
    cabeceras = {"Authorization": f"Bearer {secreto}"}

    ok = cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras)
    desconocida = cliente.post("/sync/v1/push", json=_payload(sequence=2, tipo="x.y"),
                               headers=cabeceras)

    assert ok.json()["result"] == "accepted"
    assert desconocida.json()["result"] == "rejected"
    assert "x.y" in desconocida.json()["error"]


def test_el_router_no_acepta_despachador_y_manejador_a_la_vez():
    from libraedge.sync.api import create_sync_router

    with pytest.raises(ValueError, match="no los dos"):
        create_sync_router(lambda: None, operation_handler=lambda c, o: None,
                           despachador=Despachador())


@pytest.mark.parametrize("ruta,metodo", [("/sync/v1/push", "post"), ("/sync/v1/pull", "get")])
def test_un_nodo_con_otro_esquema_recibe_409_con_la_version_que_se_espera(ruta, metodo):
    central = _central_sqlite()
    cliente, secreto = _router(central, version_esquema=esquema.version_de_alembic)
    llamada = getattr(cliente, metodo)

    def pedir(version):
        cabeceras = {"Authorization": f"Bearer {secreto}"}
        if version is not None:
            cabeceras[esquema.CABECERA] = version
        if metodo == "post":
            return llamada(ruta, json=_payload(), headers=cabeceras)
        return llamada(ruta, params={"node_id": "node-1"}, headers=cabeceras)

    vieja = pedir("alembic_version=0026")
    assert vieja.status_code == 409
    detalle = vieja.json()["detail"]
    assert detalle["error"] == "esquema_incompatible"
    assert detalle["esperada"] == "alembic_version=0027"
    assert detalle["recibida"] == "alembic_version=0026"
    assert "actualizar" in detalle["mensaje"]

    sin_version = pedir(None)
    assert sin_version.status_code == 409
    assert sin_version.json()["detail"]["recibida"] is None

    assert pedir("alembic_version=0027").status_code == 200


def test_el_409_va_despues_de_autenticar():
    """A quien no es un nodo no se le cuenta en que version esta el central."""
    central = _central_sqlite()
    cliente, _ = _router(central, version_esquema="v1")
    respuesta = cliente.get("/sync/v1/pull", params={"node_id": "node-1"},
                            headers={"Authorization": "Bearer otro", esquema.CABECERA: "v0"})
    assert respuesta.status_code == 401


def test_un_central_sin_version_declarada_no_controla_nada():
    central = _central_sqlite()
    cliente, secreto = _router(central)
    respuesta = cliente.get("/sync/v1/pull", params={"node_id": "node-1"},
                            headers={"Authorization": f"Bearer {secreto}",
                                     esquema.CABECERA: "cualquiera"})
    assert respuesta.status_code == 200


def test_la_app_dedicada_tambien_controla_el_esquema():
    from fastapi.testclient import TestClient

    from libraedge.sync.api import create_sync_app

    central = _central_sqlite()
    repo = NodeRepository(central)
    secreto = repo.register_node("node-1", branch_id="b1")
    cliente = TestClient(create_sync_app(SyncReceiver(central), repo, central,
                                         version_esquema="v2"))
    cabeceras = {"Authorization": f"Bearer {secreto}", esquema.CABECERA: "v1"}
    assert cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras).status_code == 409
    assert cliente.get("/sync/v1/pull", params={"node_id": "node-1"},
                       headers=cabeceras).status_code == 409
    cabeceras[esquema.CABECERA] = "v2"
    assert cliente.post("/sync/v1/push", json=_payload(), headers=cabeceras).status_code == 200


# ── El 409 del lado del nodo ────────────────────────────────────────────────

def _http_error(codigo, cuerpo):
    import io
    from urllib.error import HTTPError

    return HTTPError("https://central.example/sync/v1/push", codigo, "Conflict", {},
                     io.BytesIO(json.dumps(cuerpo).encode("utf-8")))


def test_los_transportes_mandan_la_version_y_traducen_el_409(monkeypatch):
    from libraedge.sync import http as http_module
    from libraedge.sync import pull as pull_module

    vistas = []

    def urlopen_409(request, timeout):
        vistas.append(dict(request.header_items()))
        raise _http_error(409, {"detail": esquema.detalle_409("v2", "v1")})

    monkeypatch.setattr(http_module, "urlopen", urlopen_409)
    monkeypatch.setattr(pull_module, "urlopen", urlopen_409)

    subida = http_module.HttpSyncTransport("https://central.example", "s", version_esquema="v1")
    bajada = pull_module.HttpPullTransport("https://central.example", "node-1", "s",
                                           version_esquema="v1")
    with pytest.raises(esquema.EsquemaIncompatible) as subiendo:
        subida.push(_op())
    with pytest.raises(esquema.EsquemaIncompatible) as bajando:
        bajada.pull()

    assert subiendo.value.esperada == "v2"
    assert subiendo.value.recibida == "v1"
    assert "actualizar" in str(bajando.value)
    # `header_items()` capitaliza: "X-libraedge-esquema".
    assert all(v.get(esquema.CABECERA.capitalize()) == "v1" for v in vistas)


def test_otros_errores_http_pasan_tal_cual(monkeypatch):
    from urllib.error import HTTPError

    from libraedge.sync import http as http_module

    def urlopen_500(request, timeout):
        raise _http_error(500, {"detail": "boom"})

    def urlopen_409_ajeno(request, timeout):
        raise _http_error(409, {"detail": "otra cosa"})

    transporte = http_module.HttpSyncTransport("https://central.example", "s")
    for falso in (urlopen_500, urlopen_409_ajeno):
        monkeypatch.setattr(http_module, "urlopen", falso)
        with pytest.raises(HTTPError):
            transporte.push(_op())


def test_sin_version_el_transporte_no_manda_la_cabecera():
    assert esquema.cabeceras_del_nodo("s", None) == {"Authorization": "Bearer s"}


def test_un_409_de_esquema_deja_la_operacion_en_cola_y_no_en_revision(conn):
    """Las ventas esperan en el outbox a que el nodo se actualice: no se pierden
    ni van a revision manual."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(_op())

    class Incompatible:
        def push(self, operacion):
            raise esquema.EsquemaIncompatible("v2", "v1")

    resultado = OutboxWorker(repo, Incompatible()).run_once()

    assert resultado.fallidas == 1
    assert "EsquemaIncompatible" in resultado.ultimo_error
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.RETRYABLE_ERROR


# ── El CLI del central ──────────────────────────────────────────────────────

class _SinCerrar:
    """La conexion del test, que el CLI no puede cerrar."""

    def __init__(self, conexion):
        self._conexion = conexion

    def __getattr__(self, nombre):
        return getattr(self._conexion, nombre)

    def close(self):
        pass


#: El despachador que el CLI carga por `modulo:objeto`.
DESPACHADOR_DE_PRUEBA = Despachador()
DESPACHADOR_DE_PRUEBA.registrar("venta.anulada", 1, lambda c, o: None)


def test_el_cli_del_central_lista_reintenta_y_descarta(con_una_en_revision, capsys, monkeypatch):
    from libraedge.central import main

    conn = con_una_en_revision
    conectar = lambda base: _SinCerrar(conn)  # noqa: E731

    assert main(["--base", "x", "revision", "listar"], conectar=conectar) == 0
    lineas = [json.loads(linea) for linea in capsys.readouterr().out.splitlines()]
    assert sorted(linea["operation_id"] for linea in lineas) == ["node-1:1", "node-2:1"]

    assert main(["--base", "x", "revision", "reintentar", "node-1:1", "--por", "soporte",
                 "--despachador", "tests.test_despacho:DESPACHADOR_DE_PRUEBA"],
                conectar=conectar) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "accepted"

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    assert main(["revision", "descartar", "node-2:1", "--por", "soporte",
                 "--motivo", "venta de prueba"], conectar=conectar) == 0
    assert "descartada" in capsys.readouterr().out

    assert main(["revision", "listar", "--todas", "--nodo", "node-2"], conectar=conectar) == 0
    (linea,) = capsys.readouterr().out.splitlines()
    assert json.loads(linea)["estado"] == "discarded"

    # Lo que ya no esta en revision sale 1 con el motivo, sin traceback.
    assert main(["revision", "descartar", "node-2:1", "--por", "soporte",
                 "--motivo", "otra vez"], conectar=conectar) == 1
    assert "no esta en revision" in capsys.readouterr().err


def test_el_cli_del_central_pide_base_y_despachador(conn, monkeypatch):
    from libraedge.central import cargar_objeto, main

    monkeypatch.delenv("LIBRAEDGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LIBRAEDGE_DESPACHADOR", raising=False)
    with pytest.raises(SystemExit, match="Falta la base"):
        main(["revision", "listar"], conectar=lambda base: _SinCerrar(conn))
    with pytest.raises(SystemExit, match="Falta el despachador"):
        main(["--base", "x", "revision", "reintentar", "a:1", "--por", "yo"],
             conectar=lambda base: _SinCerrar(conn))
    with pytest.raises(SystemExit, match="modulo:objeto"):
        cargar_objeto("sin_dos_puntos")
