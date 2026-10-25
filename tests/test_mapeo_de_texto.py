"""El id provisorio de texto y su mapeo (v0.23, ADR-026): libragenda tiene ids de texto (un uuid, un `p-042`).

El id provisorio es el texto `"-N"` y su mapeo vive en `libraedge_mapeo_texto`; el de enteros (`libraedge_mapeo`,
ADR-021) no cambia. Contra los dos motores (fixture `conn`): la diferencia entre comparar un entero con una columna
`BIGINT` y un texto con una `TEXT` es de PostgreSQL, que no convierte solo.
"""

import pytest

from libraedge.mapeo import (
    TABLA,
    TABLA_TEXTO,
    MapeoEnConflicto,
    SinMapeo,
    anotar_mapeo,
    es_provisorio,
    es_provisorio_de_texto,
    init_mapeo_schema,
    siguiente_id_provisorio,
    tabla_del_mapeo,
    traducir_id,
)

NODO = "S02-pc1"
UUID = "3f2b8c1e-5a6d-4c8e-9b7a-1d2e3f4a5b6c"


# ── el id provisorio de texto ───────────────────────────────────────────────

@pytest.mark.parametrize("id_", ["-1", "-2", "-250", "-999999999999999999"])
def test_un_menos_n_en_texto_es_un_id_provisorio(id_):
    assert es_provisorio(id_) and es_provisorio_de_texto(id_)
    assert tabla_del_mapeo(id_) == TABLA_TEXTO


@pytest.mark.parametrize("id_", [
    "", "-", "--1", "-0", "-01", "-1a", "- 1", " -1", "-1 ", "-1\n", "1", "57", "p-042", UUID,
    "-1000000000000000000",    # 19 cifras: ya no entra como entero
])
def test_lo_que_no_es_un_menos_n_no_es_provisorio(id_):
    assert not es_provisorio(id_) and not es_provisorio_de_texto(id_)
    assert tabla_del_mapeo(id_) == TABLA


def test_los_enteros_siguen_igual():
    assert es_provisorio(-1) and es_provisorio(-250)
    assert not es_provisorio(0) and not es_provisorio(57) and not es_provisorio(None) and not es_provisorio(True)
    assert not es_provisorio_de_texto(-1)
    assert tabla_del_mapeo(-1) == TABLA


def test_siguiente_id_provisorio_de_texto(conn):
    assert [siguiente_id_provisorio(conn, texto=True) for _ in range(3)] == ["-1", "-2", "-3"]


def test_la_secuencia_es_una_sola_para_las_dos_formas(conn):
    """Un numero no se reusa aunque cambie la forma: `-2` y `"-2"` serian el mismo id en el mapeo del central."""
    assert siguiente_id_provisorio(conn) == -1
    assert siguiente_id_provisorio(conn, texto=True) == "-2"
    assert siguiente_id_provisorio(conn) == -3
    assert es_provisorio(siguiente_id_provisorio(conn, texto=True))


def test_el_default_sigue_siendo_un_entero(conn):
    assert type(siguiente_id_provisorio(conn)) is int


# ── anotar y traducir un mapeo de texto ─────────────────────────────────────

def test_anotar_y_traducir_un_uuid(conn):
    assert anotar_mapeo(conn, NODO, "clients", "-3", UUID) is True
    conn.commit()
    assert traducir_id(conn, NODO, "clients", "-3") == UUID


def test_anotar_y_traducir_un_paciente_de_medlibra(conn):
    anotar_mapeo(conn, NODO, "patients", "-1", "p-042")
    conn.commit()
    resultado = traducir_id(conn, NODO, "patients", "-1")
    assert resultado == "p-042" and type(resultado) is str


def test_el_mapeo_de_texto_no_toca_el_de_enteros_y_al_reves(conn):
    anotar_mapeo(conn, NODO, "clients", "-1", UUID)
    anotar_mapeo(conn, NODO, "clientes", -1, 57)
    conn.commit()
    assert [tuple(f) for f in conn.execute(f"SELECT tabla, id_nodo, id_central FROM {TABLA_TEXTO}").fetchall()] == [
        ("clients", "-1", UUID)]
    assert [tuple(f) for f in conn.execute(f"SELECT tabla, id_nodo, id_central FROM {TABLA}").fetchall()] == [
        ("clientes", -1, 57)]


def test_el_entero_devuelve_un_entero_y_el_texto_un_texto(conn):
    """«La lectura de un mapeo entero tiene que seguir devolviendo int a quien lo pidio como int»."""
    anotar_mapeo(conn, NODO, "clientes", -3, 57)
    anotar_mapeo(conn, NODO, "clients", "-3", "c-57")
    assert traducir_id(conn, NODO, "clientes", -3) == 57 and type(traducir_id(conn, NODO, "clientes", -3)) is int
    assert traducir_id(conn, NODO, "clients", "-3") == "c-57"


def test_un_central_de_texto_que_se_anota_con_un_entero_se_guarda_como_texto(conn):
    anotar_mapeo(conn, NODO, "clients", "-1", 57)
    assert traducir_id(conn, NODO, "clients", "-1") == "57"


def test_anotar_dos_veces_lo_mismo_es_un_no_op(conn):
    assert anotar_mapeo(conn, NODO, "clients", "-3", UUID) is True
    assert anotar_mapeo(conn, NODO, "clients", "-3", UUID) is False
    conn.commit()
    assert conn.execute(f"SELECT COUNT(*) FROM {TABLA_TEXTO}").fetchone()[0] == 1


def test_un_mapeo_distinto_para_el_mismo_id_es_un_conflicto(conn):
    anotar_mapeo(conn, NODO, "clients", "-1", "c-57")
    with pytest.raises(MapeoEnConflicto, match="reuso un id provisorio"):
        anotar_mapeo(conn, NODO, "clients", "-1", "c-58")
    assert traducir_id(conn, NODO, "clients", "-1") == "c-57"


def test_el_mapeo_de_texto_es_por_nodo_y_por_tabla(conn):
    anotar_mapeo(conn, "S02-pc1", "clients", "-1", "c-1")
    anotar_mapeo(conn, "S03-pc1", "clients", "-1", "c-2")
    anotar_mapeo(conn, "S02-pc1", "patients", "-1", "p-001")
    assert traducir_id(conn, "S02-pc1", "clients", "-1") == "c-1"
    assert traducir_id(conn, "S03-pc1", "clients", "-1") == "c-2"
    assert traducir_id(conn, "S02-pc1", "patients", "-1") == "p-001"


def test_dos_ids_del_nodo_pueden_ir_al_mismo_id_del_central(conn):
    """El central deduplico por DNI: dos altas del mismo nodo son la misma ficha."""
    anotar_mapeo(conn, NODO, "patients", "-1", "p-042")
    anotar_mapeo(conn, NODO, "patients", "-2", "p-042")
    assert traducir_id(conn, NODO, "patients", "-2") == "p-042"


@pytest.mark.parametrize("malo", [None, "", "  ", True, "-5", -5, 1.5])
def test_el_id_del_central_de_un_mapeo_de_texto_tiene_que_ser_un_texto_no_provisorio(conn, malo):
    with pytest.raises(ValueError, match="id del central"):
        anotar_mapeo(conn, NODO, "clients", "-1", malo)
    assert conn.execute(f"SELECT COUNT(*) FROM {TABLA_TEXTO}").fetchone()[0] == 0


def test_solo_se_anota_un_id_provisorio_tambien_en_texto(conn):
    for malo in ("1", "p-042", UUID, "", "-0"):
        with pytest.raises(ValueError, match="provisorio"):
            anotar_mapeo(conn, NODO, "clients", malo, "c-1")


def test_la_tabla_del_mapeo_de_texto_no_acepta_un_nombre_peligroso(conn):
    with pytest.raises(ValueError, match="identificador"):
        anotar_mapeo(conn, NODO, "clients; DROP TABLE x", "-1", "c-1")
    with pytest.raises(ValueError, match="identificador"):
        traducir_id(conn, NODO, "clients; DROP TABLE x", "-1")


# ── traducir_id de texto ────────────────────────────────────────────────────

def test_los_ids_del_central_de_texto_pasan_sin_tocar(conn):
    """Un uuid o un `p-042` ya son del central: no hay nada que buscar (ni se les aplica un `int()`)."""
    assert traducir_id(conn, NODO, "clients", UUID) == UUID
    assert traducir_id(conn, NODO, "patients", "p-042") == "p-042"
    assert traducir_id(conn, NODO, "clients", None) is None


def test_un_menos_n_sin_mapeo_se_rechaza_con_un_mensaje_claro(conn):
    with pytest.raises(SinMapeo) as exc:
        traducir_id(conn, NODO, "clients", "-9")
    assert isinstance(exc.value, ValueError), "el receptor lo manda a revision manual y no traba la cola"
    mensaje = str(exc.value)
    assert "-9" in mensaje and "clients" in mensaje and NODO in mensaje and "alta" in mensaje


def test_exigir_false_devuelve_none_en_vez_de_levantar_tambien_en_texto(conn):
    assert traducir_id(conn, NODO, "clients", "-9", exigir=False) is None


def test_un_menos_n_en_texto_de_una_tabla_de_enteros_se_traduce_como_en_v0_22(conn):
    """v0.22 convertia cualquier texto con `int()`: quien pasaba `"-3"` (de un JSON, de una URL) recibia un entero."""
    anotar_mapeo(conn, NODO, "clientes", -3, 57)
    resultado = traducir_id(conn, NODO, "clientes", "-3")
    assert resultado == 57 and type(resultado) is int


# ── lo que los productos ya hacen con `libraedge_mapeo` ─────────────────────

def test_el_sql_directo_de_los_productos_sobre_libraedge_mapeo_sigue_andando(conn):
    """LibraCargo (`servicios/emisores.py`: `id_nodo = :id`) y LibraClub (`servicios/caja.py`: `id_central IN (...)`)
    consultan la tabla con SQL propio y parametros **enteros**. Si la tabla pasara a `TEXT`, PostgreSQL contesta
    `operator does not exist: text = smallint` (y LibraClub se traga el error en un `except Exception`). Por eso el
    mapeo de texto es otra tabla (ADR-026): las columnas de esta no cambian de tipo."""
    anotar_mapeo(conn, NODO, "terceros", -3, 57)
    anotar_mapeo(conn, NODO, "reservas", -1, 11)
    anotar_mapeo(conn, NODO, "reservas", -2, 12)
    anotar_mapeo(conn, NODO, "clients", "-1", UUID)       # una tabla de texto en la misma base no estorba
    conn.commit()

    fila = conn.execute(
        "SELECT id_central FROM libraedge_mapeo WHERE node_id = ? AND tabla = 'terceros' AND id_nodo = ?",
        (NODO, -3)).fetchone()
    assert fila[0] == 57 and type(fila[0]) is int

    filas = conn.execute(
        "SELECT id_central, id_nodo FROM libraedge_mapeo WHERE node_id = ? AND tabla = 'reservas' "
        "AND id_central IN (?, ?)", (NODO, 11, 12)).fetchall()
    assert sorted((a, b) for a, b in filas) == [(11, -1), (12, -2)]
    assert all(type(a) is int and type(b) is int for a, b in filas)


def test_las_columnas_de_libraedge_mapeo_siguen_siendo_enteras(conn, motor):
    if motor == "sqlite":
        tipos = {f[1]: f[2] for f in conn.execute(f"PRAGMA table_info({TABLA})").fetchall()}
        assert tipos["id_nodo"] == "BIGINT" and tipos["id_central"] == "BIGINT"
        return
    tipos = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?", (TABLA,)).fetchall())
    assert tipos["id_nodo"] == "bigint" and tipos["id_central"] == "bigint"
    tipos_texto = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?", (TABLA_TEXTO,)).fetchall())
    assert tipos_texto["id_nodo"] == "text" and tipos_texto["id_central"] == "text"


# ── la migracion: aditiva ───────────────────────────────────────────────────

def test_una_base_de_v0_22_sube_sin_tocar_sus_mapeos_enteros(conn, motor):
    """Una base que todavia no tiene `libraedge_mapeo_texto` (la de v0.22) la recibe con el arranque de siempre, y sus
    mapeos enteros quedan tal cual: mismas filas, mismo tipo."""
    conn.execute(f"DROP TABLE {TABLA_TEXTO}")
    conn.execute(f"INSERT INTO {TABLA} (node_id, tabla, id_nodo, id_central, creado_at) VALUES (?, ?, ?, ?, ?)",
                 (NODO, "clientes", -1, 57, "2026-10-21T12:00:00+00:00"))
    conn.commit()

    init_mapeo_schema(conn)
    conn.commit()

    fila = conn.execute(f"SELECT node_id, tabla, id_nodo, id_central, creado_at FROM {TABLA}").fetchall()
    assert [tuple(f) for f in fila] == [(NODO, "clientes", -1, 57, "2026-10-21T12:00:00+00:00")]
    assert conn.execute(f"SELECT COUNT(*) FROM {TABLA_TEXTO}").fetchone()[0] == 0
    assert traducir_id(conn, NODO, "clientes", -1) == 57
    anotar_mapeo(conn, NODO, "clients", "-1", UUID)
    assert traducir_id(conn, NODO, "clients", "-1") == UUID


def test_volver_a_v0_22_es_dejar_la_tabla_de_texto_sin_leer(conn):
    """v0.22 no nombra `libraedge_mapeo_texto`: la tabla de mas no le hace nada, y se saca con un `DROP`."""
    anotar_mapeo(conn, NODO, "clientes", -1, 57)
    conn.execute(f"DROP TABLE {TABLA_TEXTO}")
    conn.commit()
    assert traducir_id(conn, NODO, "clientes", -1) == 57


def test_init_mapeo_schema_es_idempotente_con_la_tabla_de_texto(conn):
    init_mapeo_schema(conn)
    anotar_mapeo(conn, NODO, "clients", "-1", UUID)
    init_mapeo_schema(conn)
    init_mapeo_schema(conn)
    assert traducir_id(conn, NODO, "clients", "-1") == UUID
