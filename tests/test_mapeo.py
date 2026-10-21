"""El id provisorio y el mapeo (v0.18, ADR-021): lo que el nodo se inventa y el central reescribe.

Contra los dos motores (fixture `conn`): el `UPDATE ... RETURNING` del contador y las claves compuestas del mapeo no se
escriben igual en ninguno de los dos.
"""

import pytest

from libraedge.db.repository import NodeRepository
from libraedge.mapeo import (
    MapeoEnConflicto,
    SinMapeo,
    anotar_mapeo,
    es_provisorio,
    init_mapeo_schema,
    siguiente_id_provisorio,
    traducir_id,
)

NODO = "S02-pc1"


# ── el id provisorio ────────────────────────────────────────────────────────

def test_los_ids_provisorios_son_negativos_y_bajan_desde_menos_uno(conn):
    assert [siguiente_id_provisorio(conn) for _ in range(4)] == [-1, -2, -3, -4]


def test_un_id_provisorio_no_se_repite_aunque_la_transaccion_siguiente_empiece_de_cero(conn):
    assert siguiente_id_provisorio(conn) == -1
    conn.commit()
    assert siguiente_id_provisorio(conn) == -2
    conn.commit()
    assert siguiente_id_provisorio(conn) == -3


def test_si_la_transaccion_se_deshace_el_numero_se_deshace_con_ella(conn):
    """Un id provisorio que nunca llego a existir no deja un hueco ni se pierde: el alta y su numero viven juntos."""
    assert siguiente_id_provisorio(conn) == -1
    conn.commit()
    assert siguiente_id_provisorio(conn) == -2
    conn.rollback()
    assert siguiente_id_provisorio(conn) == -2


def test_la_secuencia_provisoria_no_toca_la_del_outbox(conn):
    """Son dos contadores del mismo nodo: sacar un id provisorio no gasta un numero de operacion."""
    repo = NodeRepository(conn)
    assert repo.next_sequence(commit=False) == 1
    assert siguiente_id_provisorio(conn) == -1
    assert repo.next_sequence(commit=False) == 2
    assert siguiente_id_provisorio(conn) == -2


def test_es_provisorio():
    assert es_provisorio(-1) and es_provisorio(-250)
    assert not es_provisorio(0) and not es_provisorio(57) and not es_provisorio(None)
    assert not es_provisorio("-1") and not es_provisorio(True)


# ── el mapeo ────────────────────────────────────────────────────────────────

def test_anotar_y_traducir(conn):
    assert anotar_mapeo(conn, NODO, "clients", -3, 57) is True
    conn.commit()
    assert traducir_id(conn, NODO, "clients", -3) == 57


def test_anotar_dos_veces_lo_mismo_es_un_no_op(conn):
    """El outbox entrega al menos una vez: reaplicar el alta no duplica ni falla."""
    assert anotar_mapeo(conn, NODO, "clients", -3, 57) is True
    assert anotar_mapeo(conn, NODO, "clients", -3, 57) is False
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM libraedge_mapeo").fetchone()[0] == 1


def test_un_mapeo_distinto_para_el_mismo_id_es_un_conflicto(conn):
    """Una PC reemplazada con el mismo `node_id` que arranco la secuencia de cero reusaria el -1: no se pisa."""
    anotar_mapeo(conn, NODO, "clients", -1, 57)
    with pytest.raises(MapeoEnConflicto, match="reuso un id provisorio"):
        anotar_mapeo(conn, NODO, "clients", -1, 58)
    assert traducir_id(conn, NODO, "clients", -1) == 57


def test_el_mapeo_es_por_nodo_y_por_tabla(conn):
    anotar_mapeo(conn, "S02-pc1", "clients", -1, 57)
    anotar_mapeo(conn, "S03-pc1", "clients", -1, 91)
    anotar_mapeo(conn, "S02-pc1", "parties", -1, 57)
    assert traducir_id(conn, "S02-pc1", "clients", -1) == 57
    assert traducir_id(conn, "S03-pc1", "clients", -1) == 91
    assert traducir_id(conn, "S02-pc1", "parties", -1) == 57


def test_dos_ids_del_nodo_pueden_ir_al_mismo_id_del_central(conn):
    """El central deduplico: dos altas del mismo nodo con el mismo CUIT son el mismo cliente."""
    anotar_mapeo(conn, NODO, "clients", -1, 57)
    anotar_mapeo(conn, NODO, "clients", -2, 57)
    assert traducir_id(conn, NODO, "clients", -2) == 57


def test_solo_se_anota_un_id_provisorio(conn):
    for malo in (0, 5, None):
        with pytest.raises(ValueError, match="provisorio"):
            anotar_mapeo(conn, NODO, "clients", malo, 57)


def test_la_tabla_del_mapeo_no_acepta_un_nombre_peligroso(conn):
    with pytest.raises(ValueError, match="identificador"):
        anotar_mapeo(conn, NODO, "clients; DROP TABLE x", -1, 57)


# ── traducir_id ─────────────────────────────────────────────────────────────

def test_none_y_los_ids_del_central_pasan_sin_tocar(conn):
    """Un id no negativo ya es del central (el nodo uso una fila espejada): no hay nada que traducir ni que buscar."""
    assert traducir_id(conn, NODO, "clients", None) is None
    assert traducir_id(conn, NODO, "clients", 57) == 57
    assert traducir_id(conn, NODO, "clients", 0) == 0


def test_un_negativo_sin_mapeo_se_rechaza_con_un_mensaje_claro(conn):
    with pytest.raises(SinMapeo) as exc:
        traducir_id(conn, NODO, "clients", -9)
    assert isinstance(exc.value, ValueError), "el receptor lo manda a revision manual y no traba la cola"
    mensaje = str(exc.value)
    assert "-9" in mensaje and "clients" in mensaje and NODO in mensaje and "alta" in mensaje


def test_exigir_false_devuelve_none_en_vez_de_levantar(conn):
    assert traducir_id(conn, NODO, "clients", -9, exigir=False) is None


def test_init_mapeo_schema_es_idempotente(conn):
    init_mapeo_schema(conn)
    init_mapeo_schema(conn)
    anotar_mapeo(conn, NODO, "clients", -1, 57)
    init_mapeo_schema(conn)
    assert traducir_id(conn, NODO, "clients", -1) == 57
