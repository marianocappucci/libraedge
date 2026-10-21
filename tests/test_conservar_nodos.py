"""Conservar los nodos al recrear la base del central, y restaurarlos (ADR-022).

El caso que lo motivo: el reset nocturno de las demos borra la base y la siembra de nuevo. Se pierden `node_identity`
(la PC vinculada queda con 401) y `sync_changelog` (el changelog nuevo numera desde 1: un nodo con cursor 500 pide
`cursor > 500` y no baja nada, para siempre y sin error). Estos tests recrean la base de verdad (`DROP SCHEMA`) y miran
lo que ve el nodo: si su secreto sigue valiendo y si un `pull` con su cursor viejo trae la siembra nueva.

`conservar` anda en los dos motores; `restaurar` es del central y por lo tanto PostgreSQL.
"""

import json
import os
import stat

import pytest

from libraedge import aprovisionamiento as ap
from libraedge.db.changelog import init_changelog_schema, listar_cambios
from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.sync.api import create_sync_app
from libraedge.sync.receiver import SyncReceiver
from tests.test_aprovisionamiento import APROV, _crear_producto

NODO = "node-1"


def _solo_postgres(motor):
    if motor == "sqlite":
        pytest.skip("restaurar es del central: PostgreSQL")


def _recrear_la_base(conn, nombre_de_la_categoria="Gaseosas", con_producto=True):
    """Lo que hace el reset de la demo: `DROP SCHEMA`, el arranque reconstruye el esquema y se siembra otra vez."""
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    init_schema(conn)
    conn.commit()
    _crear_producto(conn)
    conn.execute("UPDATE categorias SET nombre = ? WHERE id = 1", (nombre_de_la_categoria,))
    if not con_producto:
        conn.execute("DELETE FROM productos")
    conn.commit()


def _central_con_un_nodo(conn, mucho_trafico=True):
    """El central 'de ayer': publicado, con un nodo registrado y el changelog adelantado. Devuelve (secreto, cursor)."""
    assert ap.publicar(conn, APROV, salida=lambda _: None) == 0
    alta = ap.registrar(conn, APROV, NODO, salida=lambda _: None)
    if mucho_trafico:
        for i in range(25):
            conn.execute("UPDATE productos SET nombre = ? WHERE id = 1", (f"Agua {i}",))
            conn.commit()
    return alta.secreto, listar_cambios(conn, limit=1000)[-1].cursor


def _cliente(conn):
    from fastapi.testclient import TestClient

    return TestClient(create_sync_app(SyncReceiver(conn), NodeRepository(conn), changelog_conn=conn))


def _pull(conn, secreto, cursor):
    respuesta = _cliente(conn).get("/sync/v1/pull", params={"node_id": NODO, "cursor": cursor},
                                    headers={"Authorization": f"Bearer {secreto}"})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# ── lo que importa: el nodo sigue funcionando despues del reset ─────────────

def test_el_nodo_sigue_autenticando_y_baja_la_siembra_nueva_con_su_cursor_viejo(conn, motor, tmp_path):
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    secreto, cursor_viejo = _central_con_un_nodo(producto)
    assert cursor_viejo > 25

    archivo = str(tmp_path / "nodos.json")
    assert ap.conservar(conn, archivo, salida=lambda _: None) == 0
    _recrear_la_base(conn)

    # Sin restaurar: la PC queda huerfana y, aunque volviera, el changelog nuevo no llega a su cursor.
    assert not NodeRepository(conn).verify_node_secret(NODO, secreto)

    lineas = []
    assert ap.restaurar(conn, APROV, archivo, salida=lineas.append) == 0, lineas

    assert NodeRepository(conn).verify_node_secret(NODO, secreto), "el secreto de la PC tiene que seguir valiendo"
    bajada = _pull(conn, secreto, cursor_viejo)
    assert bajada["changes"], "con el cursor viejo el nodo tiene que bajar la siembra nueva"
    assert all(c["cursor"] > cursor_viejo for c in bajada["changes"])
    assert bajada["cursor"] > cursor_viejo
    por_tabla = {c["table_name"]: c["payload"] for c in bajada["changes"]}
    assert set(por_tabla) == {"categorias", "productos", "party_roles"}
    assert por_tabla["categorias"]["nombre"] == "Gaseosas", "baja el estado NUEVO, no el de ayer"
    # Y los cambios posteriores siguen la misma numeracion.
    producto.execute("UPDATE productos SET nombre = 'Soda' WHERE id = 1")
    producto.commit()
    assert listar_cambios(conn, desde=bajada["cursor"])[-1].payload["nombre"] == "Soda"


def test_sin_subir_la_numeracion_el_nodo_no_baja_nada(conn, motor, tmp_path, monkeypatch):
    """El control del test anterior: es lo que pasaba antes de este cambio y lo que `restaurar` evita."""
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    secreto, cursor_viejo = _central_con_un_nodo(producto)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    _recrear_la_base(conn)
    monkeypatch.setattr(ap, "_subir_cursor_del_changelog", lambda conn, minimo: 0)
    assert ap.restaurar(conn, APROV, archivo, salida=lambda _: None) == 0
    assert _pull(conn, secreto, cursor_viejo)["changes"] == []


def test_restaurar_es_idempotente(conn, motor, tmp_path):
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    secreto, _ = _central_con_un_nodo(producto, mucho_trafico=False)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    _recrear_la_base(conn)

    assert ap.restaurar(conn, APROV, archivo, salida=lambda _: None) == 0
    cambios = len(listar_cambios(conn, limit=1000))
    lineas = []
    assert ap.restaurar(conn, APROV, archivo, salida=lineas.append) == 0
    assert conn.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 1
    assert len(listar_cambios(conn, limit=1000)) == cambios, "no vuelve a sembrar lo que ya esta"
    assert any("ya estaban: 1" in linea for linea in lineas)
    assert NodeRepository(conn).verify_node_secret(NODO, secreto)


def test_un_nodo_dado_de_baja_sigue_dado_de_baja(conn, motor, tmp_path):
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    secreto, _ = _central_con_un_nodo(producto, mucho_trafico=False)
    ap.dar_de_baja(producto, NODO, salida=lambda _: None)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    _recrear_la_base(conn)
    ap.restaurar(conn, APROV, archivo, salida=lambda _: None)
    assert conn.execute("SELECT active FROM node_identity WHERE node_id = ?", (NODO,)).fetchone()[0] == 0
    assert not NodeRepository(conn).verify_node_secret(NODO, secreto)


def test_los_cambios_que_la_base_nueva_ya_tenia_con_cursor_bajo_se_renumeran(conn, motor, tmp_path):
    """Si algo publico antes de restaurar, esas filas quedaron por debajo del cursor del nodo: nadie las bajaria."""
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    secreto, cursor_viejo = _central_con_un_nodo(producto)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    _recrear_la_base(conn)
    init_changelog_schema(conn)
    assert ap.publicar(conn, APROV, salida=lambda _: None) == 0
    antes = [(c.table_name, c.row_id) for c in listar_cambios(conn, limit=1000)]
    assert antes and max(c.cursor for c in listar_cambios(conn, limit=1000)) < cursor_viejo

    lineas = []
    assert ap.restaurar(conn, APROV, archivo, salida=lineas.append) == 0
    despues = listar_cambios(conn, limit=1000)
    assert [(c.table_name, c.row_id) for c in despues] == antes, "mismo contenido y mismo orden, sin duplicar"
    assert min(c.cursor for c in despues) > cursor_viejo
    assert any(f"{len(antes)} cambios que ya habia se renumeraron" in linea for linea in lineas)
    assert _pull(conn, secreto, cursor_viejo)["changes"]


def test_el_cursor_maximo_cuenta_la_secuencia_y_no_solo_la_ultima_fila(conn, motor, tmp_path):
    """Un changelog podado deja la secuencia por encima de su ultima fila; el nodo pudo haber llegado hasta ahi."""
    _solo_postgres(motor)
    producto = _crear_producto(conn)
    _, cursor_viejo = _central_con_un_nodo(producto)
    conn.execute("DELETE FROM sync_changelog WHERE cursor_id > 3")
    conn.commit()
    assert ap.cursor_maximo(conn) == cursor_viejo
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    assert json.load(open(archivo, encoding="utf-8"))["cursor_maximo"] == cursor_viejo


# ── el archivo ──────────────────────────────────────────────────────────────

def test_el_archivo_es_600_tiene_el_hash_y_no_el_secreto(conn, tmp_path):
    producto = _crear_producto(conn)
    secreto = ap.registrar(producto, APROV, NODO, salida=lambda _: None).secreto
    archivo = tmp_path / "nodos.json"
    lineas = []
    assert ap.conservar(conn, str(archivo), salida=lineas.append) == 0

    assert stat.S_IMODE(os.stat(archivo).st_mode) == 0o600
    texto = archivo.read_text(encoding="utf-8")
    assert secreto not in texto
    datos = json.loads(texto)
    assert datos["formato"] == 1 and datos["cursor_maximo"] == 0
    (nodo,) = datos["nodos"]
    assert nodo["node_id"] == NODO and nodo["prefijo"] == "S01" and nodo["secret_hash"]
    assert nodo["secret_hash"] == conn.execute("SELECT secret_hash FROM node_identity").fetchone()[0]
    assert set(nodo) == {"node_id", "branch_id", "installed_at", "schema_version", "last_server_cursor", "active",
                         "secret_hash", "last_seen_at", "prefijo"}
    # Lo que se imprime no tiene nada de eso.
    assert all(nodo["secret_hash"] not in linea and secreto not in linea for linea in lineas)


def test_sin_nodos_escribe_igual_un_archivo_valido(conn, motor, tmp_path):
    archivo = tmp_path / "nodos.json"
    assert ap.conservar(conn, str(archivo), salida=lambda _: None) == 0
    datos = json.loads(archivo.read_text(encoding="utf-8"))
    assert datos["nodos"] == [] and datos["formato"] == 1
    assert stat.S_IMODE(os.stat(archivo).st_mode) == 0o600


def test_conservar_no_pisa_un_archivo_que_existe(conn, tmp_path):
    archivo = tmp_path / "nodos.json"
    archivo.write_text("de la corrida anterior", encoding="utf-8")
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="ya existe"):
        ap.conservar(conn, str(archivo), salida=lambda _: None)
    assert archivo.read_text(encoding="utf-8") == "de la corrida anterior"


def test_restaurar_con_la_lista_vacia_no_hace_nada(conn, motor, tmp_path):
    _solo_postgres(motor)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    _recrear_la_base(conn)
    lineas = []
    assert ap.restaurar(conn, APROV, archivo, salida=lineas.append) == 0
    assert any("no hay nada que restaurar" in linea for linea in lineas)
    from libraedge.db.changelog import tablas_publicadas

    assert tablas_publicadas(conn) == (), "ni siquiera publica"


def test_restaurar_rechaza_un_archivo_que_no_existe_o_no_es_de_conservar(conn, motor, tmp_path):
    _solo_postgres(motor)
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="no existe"):
        ap.restaurar(conn, APROV, str(tmp_path / "no-esta.json"), salida=lambda _: None)
    ajeno = tmp_path / "ajeno.json"
    ajeno.write_text('{"cualquier": "cosa"}', encoding="utf-8")
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="no es un archivo de `nodo conservar`"):
        ap.restaurar(conn, APROV, str(ajeno), salida=lambda _: None)
    roto = tmp_path / "roto.json"
    roto.write_text("{", encoding="utf-8")
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="no se pudo leer"):
        ap.restaurar(conn, APROV, str(roto), salida=lambda _: None)


def test_restaurar_no_corre_en_sqlite(conn, motor, tmp_path):
    if motor != "sqlite":
        pytest.skip("el caso de PostgreSQL esta en los otros tests")
    producto = _crear_producto(conn)
    ap.registrar(producto, APROV, NODO, salida=lambda _: None)
    archivo = str(tmp_path / "nodos.json")
    ap.conservar(conn, archivo, salida=lambda _: None)
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="PostgreSQL"):
        ap.restaurar(conn, APROV, archivo, salida=lambda _: None)


# ── el CLI ──────────────────────────────────────────────────────────────────

def test_el_cli_conserva_y_restaura(conn, motor, tmp_path, capsys, monkeypatch):
    _solo_postgres(motor)
    from libracore.db import core

    from libraedge.central import main

    producto = _crear_producto(conn)
    secreto, cursor_viejo = _central_con_un_nodo(producto)
    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    conectar = lambda base: core.get_connection()  # noqa: E731 - el CLI la cierra: una por llamada
    archivo = str(tmp_path / "nodos.json")

    assert main(["nodo", "conservar", "--a", archivo], conectar=conectar) == 0
    salida = capsys.readouterr().out
    assert "Conservados 1 nodos" in salida and "secret" not in salida.lower()
    assert stat.S_IMODE(os.stat(archivo).st_mode) == 0o600
    assert main(["nodo", "conservar", "--a", archivo], conectar=conectar) == 1  # no pisa
    assert "ya existe" in capsys.readouterr().err

    _recrear_la_base(conn)
    assert main(["nodo", "restaurar", "--de", archivo], conectar=conectar) == 0
    assert "Nodos restaurados: 1" in capsys.readouterr().out
    assert NodeRepository(conn).verify_node_secret(NODO, secreto)
    assert _pull(conn, secreto, cursor_viejo)["changes"]


def test_el_cli_restaurar_de_un_archivo_que_no_existe_sale_distinto_de_0(conn, motor, tmp_path, capsys, monkeypatch):
    _solo_postgres(motor)
    from libracore.db import core

    from libraedge.central import main

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    assert main(["nodo", "restaurar", "--de", str(tmp_path / "no-esta.json")],
                conectar=lambda base: core.get_connection()) == 1
    assert "ERROR: no existe" in capsys.readouterr().err


def test_los_subcomandos_existen_para_el_script_del_reset(capsys):
    """El reset de las demos detecta el subcomando con `--help` (los productos pinean versiones sin el)."""
    from libraedge.central import main

    for subcomando in ("conservar", "restaurar"):
        with pytest.raises(SystemExit) as salida:
            main(["nodo", subcomando, "--help"])
        assert salida.value.code == 0
        capsys.readouterr()
