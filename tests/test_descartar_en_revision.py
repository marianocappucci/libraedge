"""Descartar una operacion en revision del nodo (v0.21, ADR-024).

Hasta v0.20 lo que el central rechazaba quedaba en `manual_review` del outbox para siempre: `libraedge-nodo revision`
solo dejaba `listar` y `reenviar`. En LibraCommerce un movimiento de stock ahi frena la carga inicial de existencias
(`stock_en_revision`), y en la prueba real del 2026-10-22 hubo que tocar la base a mano. Corre contra los dos motores
(fixture `conn`), salvo el CLI, que va por el camino real del proceso (PostgreSQL).
"""

import json
from dataclasses import replace

import pytest

from libraedge.db.repository import NodeRepository, OperacionYaDescartada
from libraedge.domain.sync import OutboxOperation, SyncOperationStatus
from libraedge.nodo import Nodo, leer_estado
from libraedge.sync.worker import OutboxWorker, PushResult

RECHAZO = "sin stock en el deposito 7"


def operacion(sequence):
    return OutboxOperation(
        operation_id=f"node-1:{sequence}", node_id="node-1", sequence=sequence,
        operation_type="stock.movimiento", aggregate_type="stock",
        aggregate_id=f"node-1:{sequence}", occurred_at="2026-10-22T10:00:00Z",
        schema_version=1, payload={"n": sequence},
    )


class Transporte:
    """Acepta todo y anota lo que le llega."""

    def __init__(self):
        self.enviadas = []

    def push(self, op):
        self.enviadas.append(op.operation_id)
        return PushResult("accepted")


@pytest.fixture
def repo_con_una_en_revision(conn):
    repo = NodeRepository(conn)
    repo.enqueue_operation(operacion(1))
    repo.mark_operation_manual_review("node-1:1", RECHAZO)
    return repo


def test_descartar_una_en_revision_la_deja_discarded_con_el_motivo(repo_con_una_en_revision):
    repo = repo_con_una_en_revision

    descartada = repo.descartar_operacion(
        "node-1:1", "  fue una prueba, no existe en el central  ", "2026-10-22T11:00:00Z")

    assert descartada.status == SyncOperationStatus.DISCARDED == "discarded"
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.DISCARDED
    assert descartada.last_error == (
        "descartada el 2026-10-22T11:00:00Z: fue una prueba, no existe en el central"
        f" | rechazo del central: {RECHAZO}"), "motivo, momento y el rechazo original, sin perder ninguno"
    assert repo.listar_en_revision() == (), "ya no espera a nadie"


@pytest.mark.parametrize("motivo", ["", "   ", None])
def test_descartar_sin_motivo_se_rechaza_y_no_toca_nada(repo_con_una_en_revision, motivo):
    repo = repo_con_una_en_revision

    with pytest.raises(ValueError, match="motivo"):
        repo.descartar_operacion("node-1:1", motivo)

    operacion_guardada = repo.get_operation("node-1:1")
    assert operacion_guardada.status == SyncOperationStatus.MANUAL_REVIEW
    assert operacion_guardada.last_error == RECHAZO


@pytest.mark.parametrize("estado", [
    SyncOperationStatus.PENDING, SyncOperationStatus.SENDING,
    SyncOperationStatus.RETRYABLE_ERROR, SyncOperationStatus.ACKNOWLEDGED])
def test_solo_se_descarta_lo_que_esta_en_revision(conn, estado):
    """Una `pending` todavia puede salir sola y una `acknowledged` el central ya la tiene: no se tocan."""
    repo = NodeRepository(conn)
    repo.enqueue_operation(replace(operacion(1), status=estado))

    with pytest.raises(ValueError, match="solo se descarta una en manual_review") as error:
        repo.descartar_operacion("node-1:1", "no va")

    assert not isinstance(error.value, OperacionYaDescartada)
    assert repo.get_operation("node-1:1").status == estado


def test_descartar_una_que_no_existe(conn):
    with pytest.raises(LookupError, match="node-1:99"):
        NodeRepository(conn).descartar_operacion("node-1:99", "no va")


def test_descartar_dos_veces_avisa_y_no_pisa_el_motivo(repo_con_una_en_revision):
    repo = repo_con_una_en_revision
    primera = repo.descartar_operacion("node-1:1", "primer motivo", "2026-10-22T11:00:00Z")

    with pytest.raises(OperacionYaDescartada, match="ya estaba descartada"):
        repo.descartar_operacion("node-1:1", "otro motivo", "2026-10-22T12:00:00Z")

    assert repo.get_operation("node-1:1").last_error == primera.last_error


def test_una_descartada_no_se_reenvia(repo_con_una_en_revision):
    repo = repo_con_una_en_revision
    repo.descartar_operacion("node-1:1", "no va")

    with pytest.raises(LookupError):
        repo.reenviar_operacion("node-1:1")
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.DISCARDED


def test_el_worker_nunca_manda_una_descartada(conn):
    repo = NodeRepository(conn)
    for n in (1, 2):
        repo.enqueue_operation(operacion(n))
    repo.mark_operation_manual_review("node-1:1", RECHAZO)
    repo.descartar_operacion("node-1:1", "no va")
    transporte = Transporte()

    for _ in range(3):
        resultado = OutboxWorker(repo, transporte).run_once()

    assert transporte.enviadas == ["node-1:2"], "la descartada no sale ni en el primer ciclo ni en los siguientes"
    assert resultado.procesadas == 0
    assert repo.get_operation("node-1:1").status == SyncOperationStatus.DISCARDED
    assert repo.get_operation("node-1:1").attempts == 0


def test_el_estado_del_nodo_no_cuenta_las_descartadas(conn, tmp_path):
    repo = NodeRepository(conn)
    for n in (1, 2):
        repo.enqueue_operation(operacion(n))
    repo.mark_operation_manual_review("node-1:1", RECHAZO)
    nodo = Nodo(repo, "node-1", ruta_estado=str(tmp_path / "estado.json"))
    assert nodo.sincronizar().pendientes == 1, "la de revision no cuenta; queda la pending"

    repo.descartar_operacion("node-1:1", "no va")
    estado = nodo.sincronizar()

    assert estado.pendientes == 1, "descartar no suma ni resta: sigue la pending y nada mas"
    assert leer_estado(str(tmp_path / "estado.json")).pendientes == 1
    repo.mark_operation_manual_review("node-1:2", RECHAZO)
    repo.descartar_operacion("node-1:2", "tampoco va")
    assert nodo.sincronizar().pendientes == 0


def test_listar_descartadas(conn):
    repo = NodeRepository(conn)
    for n in (1, 2, 3):
        repo.enqueue_operation(operacion(n))
    repo.mark_operation_manual_review("node-1:1", RECHAZO)
    repo.mark_operation_manual_review("node-1:2", RECHAZO)
    repo.descartar_operacion("node-1:2", "no va")

    assert [o.operation_id for o in repo.listar_en_revision()] == ["node-1:1"]
    assert [o.operation_id for o in repo.listar_descartadas()] == ["node-1:2"]


# ── El CLI ──────────────────────────────────────────────────────────────────

def test_el_cli_del_nodo_descarta_y_lista(monkeypatch, capsys):
    """`libraedge-nodo revision listar|descartar`, por el camino real del proceso (PostgreSQL)."""
    from libracore.db import core

    from libraedge.cli import main
    from libraedge.db.schema import init_schema
    from tests.conftest import url_postgres

    url = url_postgres()
    core.configure(url)
    conexion = core.get_connection()
    try:
        conexion.execute("DROP SCHEMA public CASCADE")
        conexion.execute("CREATE SCHEMA public")
        conexion.commit()
        init_schema(conexion)
        repo = NodeRepository(conexion)
        for n in (1, 2, 3):
            repo.enqueue_operation(operacion(n))
        repo.mark_operation_manual_review("node-1:1", RECHAZO)
        repo.mark_operation_manual_review("node-1:2", RECHAZO)
    finally:
        conexion.close()
        core._db_path = None
        core._database_url = None

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", url)
    try:
        # Sin motivo, o con uno en blanco: sale 1 y no toca nada.
        with pytest.raises(SystemExit) as salida:
            main(["revision", "descartar", "node-1:1"])
        assert salida.value.code == 2, "argparse: --motivo es obligatorio"
        capsys.readouterr()
        assert main(["revision", "descartar", "node-1:1", "--motivo", "   "]) == 1
        assert "motivo" in capsys.readouterr().err

        # Una pending y una inexistente: error claro y sale 1.
        assert main(["revision", "descartar", "node-1:3", "--motivo", "no va"]) == 1
        assert "solo se descarta una en manual_review" in capsys.readouterr().err
        assert main(["revision", "descartar", "node-1:99", "--motivo", "no va"]) == 1
        assert "no hay ninguna operacion" in capsys.readouterr().err

        # La buena.
        assert main(["revision", "descartar", "node-1:1", "--motivo", "prueba del 22/10"]) == 0
        assert "node-1:1: descartada" in capsys.readouterr().out

        # Otra vez: aviso por stderr, sale 0 y el motivo no se pisa.
        assert main(["revision", "descartar", "node-1:1", "--motivo", "otro"]) == 0
        assert "ya estaba descartada" in capsys.readouterr().err

        # `listar` sin `--todas` solo muestra la que espera a alguien; con `--todas`, tambien la descartada.
        assert main(["revision", "listar"]) == 0
        (linea,) = capsys.readouterr().out.splitlines()
        assert json.loads(linea)["operation_id"] == "node-1:2"
        assert json.loads(linea)["estado"] == "manual_review"

        assert main(["revision", "listar", "--todas"]) == 0
        lineas = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
        assert [(x["operation_id"], x["estado"]) for x in lineas] == [
            ("node-1:2", "manual_review"), ("node-1:1", "discarded")]
        assert "prueba del 22/10" in lineas[1]["motivo"] and "otro" not in lineas[1]["motivo"]
        assert RECHAZO in lineas[1]["motivo"]

        # Una descartada tampoco se reenvia.
        assert main(["revision", "reenviar", "node-1:1"]) == 1
        assert "no hay ninguna operacion" in capsys.readouterr().err
    finally:
        core._db_path = None
        core._database_url = None
