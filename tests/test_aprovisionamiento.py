"""El aprovisionamiento del central, generico (ADR-008).

Nacio en `restolibra/scripts/nodo_offline.py`; aca se prueba con un producto de
juguete: `branches` como las de LibraCommerce, dos tablas de referencia (una con
clave compuesta) y la guarda del adicional `offline` de libracore con un
`plans` de mentira. Contra los dos motores salvo `publicar`, que es del central
y por lo tanto PostgreSQL.
"""

import sqlite3
import types
from datetime import UTC, datetime, timedelta

import pytest

from libraedge import aprovisionamiento as ap
from libraedge.db.changelog import init_changelog_schema, listar_cambios, tablas_publicadas
from libraedge.db.repository import NodeRepository
from libraedge.identidad import (
    IdentidadEnConflicto,
    nodo_actual,
    prefijo_actual,
    variable_del_producto,
)


def _sucursales(conn):
    return [{"id": f[0], "nombre": f[1], "activa": bool(f[2])}
            for f in conn.execute("SELECT id, name, active FROM branches ORDER BY id").fetchall()]


PLANS = types.SimpleNamespace(ADICIONALES={
    "offline": {"label": "Modo offline", "precio": None, "modulos": {"offline"},
                "unidad": "sucursal", "limite": "sucursales_offline"},
})

#: El que carga el CLI por `modulo:objeto`. Sin guarda: la del adicional se prueba aparte.
APROV = ap.Aprovisionamiento(
    prefijo="juguete",
    tablas_de_referencia=["categorias", "productos", ("party_roles", "party_id+role"), "no_existe"],
    listar_sucursales=_sucursales,
    verificar_alta=ap.sin_guarda,
)


def _aprov(**cambios):
    datos = {"prefijo": "juguete", "tablas_de_referencia": ["categorias", "productos"],
             "listar_sucursales": _sucursales, "verificar_alta": ap.sin_guarda}
    datos.update(cambios)
    return ap.Aprovisionamiento(**datos)


def _crear_producto(conn, sucursales=(("1", "Centro", 1),)):
    init_changelog_schema(conn)
    conn.execute("CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL)")
    conn.execute("CREATE TABLE categorias (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
    conn.execute("CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL,"
                 " categoria_id INTEGER REFERENCES categorias(id))")
    conn.execute("CREATE TABLE party_roles (party_id INTEGER NOT NULL, role TEXT NOT NULL,"
                 " PRIMARY KEY (party_id, role))")
    conn.execute("CREATE TABLE sin_clave (nombre TEXT)")
    conn.execute("CREATE TABLE modulos (modulo TEXT PRIMARY KEY, habilitado INTEGER NOT NULL)")
    for id_, nombre, activa in sucursales:
        conn.execute("INSERT INTO branches (id, name, active) VALUES (?, ?, ?)", (int(id_), nombre, activa))
    conn.execute("INSERT INTO categorias (id, nombre) VALUES (1, 'Bebidas')")
    conn.execute("INSERT INTO productos (id, nombre, categoria_id) VALUES (1, 'Agua', 1)")
    conn.execute("INSERT INTO party_roles (party_id, role) VALUES (7, 'cliente')")
    conn.commit()
    return conn


@pytest.fixture
def producto(conn):
    return _crear_producto(conn)


# ── la costura ──────────────────────────────────────────────────────────────

def test_las_tablas_se_declaran_de_varias_formas():
    tablas = ap.normalizar_tablas(
        ["b", ("a", "id"), ap.Tabla("c", ("x", "y")), ("d", "p+q"), ("e", ("m", "n"))])
    assert tablas == (ap.Tabla("a", ("id",)), ap.Tabla("b", None), ap.Tabla("c", ("x", "y")),
                      ap.Tabla("d", ("p", "q")), ap.Tabla("e", ("m", "n")))
    assert ap.normalizar_tablas({"z": None, "y": "id"}) == (ap.Tabla("y", ("id",)), ap.Tabla("z"))


@pytest.mark.parametrize("malas, error", [
    (["a", ("a", "id")], "dos veces"),
    ([], "vacias"),
    (["a; DROP TABLE x"], "identificador"),
])
def test_las_tablas_mal_declaradas_fallan_al_construir(malas, error):
    with pytest.raises(ValueError, match=error):
        _aprov(tablas_de_referencia=malas)


def test_el_prefijo_se_valida_y_se_normaliza():
    assert _aprov(prefijo=" restolibra ").prefijo == "RESTOLIBRA"
    with pytest.raises(ValueError, match="prefijo"):
        _aprov(prefijo="resto-libra")


def test_cargar_aprovisionamiento():
    cargado = ap.cargar_aprovisionamiento("tests.test_aprovisionamiento:APROV")
    assert cargado.prefijo == "JUGUETE" and cargado.nombres == APROV.nombres
    assert isinstance(ap.cargar_aprovisionamiento("tests.test_aprovisionamiento:_aprov"),
                      ap.Aprovisionamiento)
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="modulo:objeto"):
        ap.cargar_aprovisionamiento("sin_dos_puntos")
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="no es un Aprovisionamiento"):
        ap.cargar_aprovisionamiento("tests.test_aprovisionamiento:PLANS")


# ── el catalogo de la base ──────────────────────────────────────────────────

def test_la_clave_se_lee_de_la_base(producto):
    assert ap.clave_primaria(producto, "productos") == ("id",)
    assert ap.clave_primaria(producto, "party_roles") == ("party_id", "role")
    assert ap.clave_primaria(producto, "sin_clave") is None
    assert ap.tabla_existe(producto, "productos")
    assert not ap.tabla_existe(producto, "no_existe")


def test_el_plan_ordena_saltea_e_informa(producto):
    aprov = _aprov(tablas_de_referencia=[
        "productos", "categorias", ("party_roles", "party_id+role"), "sin_clave",
        ("branches", "codigo"), "no_existe"])
    plan = ap.plan_de_publicacion(producto, aprov)
    nombres = [t for t, _ in plan.publicables]
    assert nombres.index("categorias") < nombres.index("productos"), "el padre se siembra antes"
    assert dict(plan.publicables)["party_roles"] == ("party_id", "role")
    assert plan.faltantes == ("no_existe",)
    salteadas = dict(plan.salteadas)
    assert "sin clave primaria" in salteadas["sin_clave"]
    assert "declarada es codigo" in salteadas["branches"]


def test_una_dependencia_circular_no_cuelga(conn, motor):
    if motor == "sqlite":
        conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES b(id))")
        conn.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id))")
    else:
        conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER)")
        conn.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id))")
        conn.execute("ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b(id)")
    conn.commit()
    assert ap.orden_de_siembra(conn, ["b", "a"]) == ["a", "b"]


def test_el_espejo_y_su_variable(producto):
    espejo = ap.espejo_del_nodo(producto, APROV)
    assert espejo == {"categorias": ("id",), "productos": ("id",), "party_roles": ("party_id", "role")}
    assert ap.formato_tablas_espejo(espejo) == "categorias:id,party_roles:party_id+role,productos:id"


# ── publicar ────────────────────────────────────────────────────────────────

def test_publicar_es_del_central_postgresql(producto, motor):
    if motor != "sqlite":
        pytest.skip("en PostgreSQL publica: lo prueba el test siguiente")
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="PostgreSQL"):
        ap.publicar(producto, APROV)


def test_publicar_instala_siembra_y_converge(producto, motor):
    if motor == "sqlite":
        pytest.skip("el trigger del changelog es PostgreSQL: el central siempre lo es")
    lineas = []
    assert ap.publicar(producto, APROV, salida=lineas.append) == 0
    assert set(tablas_publicadas(producto)) == {"categorias", "productos", "party_roles"}
    sembrados = listar_cambios(producto)
    assert [c.table_name for c in sembrados][:2] == ["categorias", "party_roles"]
    assert {c.table_name: c.row_id for c in sembrados}["party_roles"] == '["7", "cliente"]'
    assert any("no_existe" in linea for linea in lineas)

    # Idempotente: no vuelve a sembrar lo que ya tiene cambios.
    lineas.clear()
    assert ap.publicar(producto, APROV, salida=lineas.append) == 0
    assert len(listar_cambios(producto)) == len(sembrados)
    assert any("ya tenia 1 cambios" in linea for linea in lineas)

    # El trigger captura despues de publicar.
    producto.execute("UPDATE productos SET nombre = 'Agua con gas' WHERE id = 1")
    producto.commit()
    assert listar_cambios(producto)[-1].payload["nombre"] == "Agua con gas"

    # Converge: lo que sale de la lista pierde el trigger.
    lineas.clear()
    assert ap.publicar(producto, _aprov(tablas_de_referencia=["categorias"]), salida=lineas.append) == 0
    assert tablas_publicadas(producto) == ("categorias",)
    assert any("productos: trigger RETIRADO" in linea for linea in lineas)


def test_publicar_sale_1_si_queda_un_agujero(producto, motor):
    if motor == "sqlite":
        pytest.skip("el trigger del changelog es PostgreSQL")
    lineas = []
    aprov = _aprov(tablas_de_referencia=["categorias", "sin_clave"])
    assert ap.publicar(producto, aprov, salida=lineas.append) == 1
    assert tablas_publicadas(producto) == ("categorias",)
    assert any("INCOMPLETA" in linea for linea in lineas)


# ── sucursales y alta ───────────────────────────────────────────────────────

@pytest.fixture
def tres_sucursales(conn):
    return _crear_producto(conn, (("1", "Centro", 1), ("2", "Norte", 1), ("3", "Vieja", 0)))


def test_registrar_con_una_sola_sucursal_la_usa(producto):
    lineas = []
    alta = ap.registrar(producto, APROV, "node-1", salida=lineas.append)
    assert alta.sucursal.id == "1" and "unica sucursal activa" in lineas[0]
    assert alta.prefijo == "S01"
    assert NodeRepository(producto).verify_node_secret("node-1", alta.secreto)
    assert alta.entorno_del_nodo() == [
        "LIBRAEDGE_NODE_ID=node-1", f"LIBRAEDGE_NODE_SECRET={alta.secreto}", "LIBRAEDGE_PREFIJO=S01",
        "LIBRAEDGE_TABLAS_ESPEJO=categorias:id,party_roles:party_id+role,productos:id"]


@pytest.mark.parametrize("pedida, id_", [("2", "2"), ("norte", "2"), ("  CENTRO ", "1")])
def test_la_sucursal_por_id_o_por_nombre(tres_sucursales, pedida, id_):
    assert ap.elegir_sucursal(tres_sucursales, APROV, pedida).id == id_


@pytest.mark.parametrize("pedida, error", [
    ("Sur", "no existe"), ("Vieja", "inactiva"), (None, "Falta --sucursal")])
def test_la_sucursal_invalida(tres_sucursales, pedida, error):
    with pytest.raises(ap.SucursalInvalida, match=error) as exc:
        ap.registrar(tres_sucursales, APROV, "node-1", pedida)
    assert "Norte" in str(exc.value), "el error lista las validas"
    assert ap.sucursales_con_nodo(tres_sucursales) == 0


def test_un_nombre_repetido_pide_el_id(conn):
    _crear_producto(conn, (("1", "Centro", 1), ("2", "centro", 1)))
    with pytest.raises(ap.SucursalInvalida, match="usar el id"):
        ap.elegir_sucursal(conn, APROV, "Centro")


def test_sin_sucursales_activas(conn):
    _crear_producto(conn, (("1", "Vieja", 0),))
    with pytest.raises(ap.SucursalInvalida, match="ninguna sucursal activa"):
        ap.elegir_sucursal(conn, APROV, None)


def test_el_menu_interactivo(tres_sucursales):
    respuestas = iter(["9", "x", "2"])
    lineas = []
    elegida = ap.elegir_sucursal(tres_sucursales, APROV, None, interactivo=True,
                                 entrada=lambda _: next(respuestas), salida=lineas.append)
    assert elegida.id == "2"
    assert sum("Opcion invalida" in linea for linea in lineas) == 2

    def cerrada(_):
        raise EOFError

    with pytest.raises(ap.SucursalInvalida, match="No se eligio"):
        ap.elegir_sucursal(tres_sucursales, APROV, None, interactivo=True, entrada=cerrada,
                           salida=lineas.append)


def test_un_segundo_nodo_en_la_sucursal_se_rechaza(tres_sucursales):
    ap.registrar(tres_sucursales, APROV, "node-1", "1")
    with pytest.raises(ap.SegundoNodoEnLaSucursal, match="'node-1'"):
        ap.registrar(tres_sucursales, APROV, "node-2", "1")


def test_el_prefijo_es_por_nodo_y_no_se_reusa(tres_sucursales):
    conn = tres_sucursales
    primero = ap.registrar(conn, APROV, "node-1", "2")
    assert primero.prefijo == "S02"
    # La misma PC con secreto nuevo conserva su prefijo: sigue su numeracion.
    otra_vez = ap.registrar(conn, APROV, "node-1", "2")
    assert otra_vez.prefijo == "S02" and otra_vez.secreto != primero.secreto
    assert not NodeRepository(conn).verify_node_secret("node-1", primero.secreto)

    # Se reemplaza la PC: el nodo nuevo NO hereda el prefijo del dado de baja.
    assert ap.dar_de_baja(conn, "node-1", salida=lambda _: None) == 0
    assert ap.registrar(conn, APROV, "node-2", "2").prefijo == "S02B"
    ap.dar_de_baja(conn, "node-2", salida=lambda _: None)
    assert ap.registrar(conn, APROV, "node-3", "Norte").prefijo == "S02C"
    assert ap.registrar(conn, APROV, "node-4", "1").prefijo == "S01"
    prefijos = [f[0] for f in conn.execute("SELECT prefijo FROM node_identity").fetchall()]
    assert len(prefijos) == len(set(prefijos))


def test_un_nodo_de_antes_del_prefijo_recibe_uno_al_re_registrarse(producto):
    NodeRepository(producto).register_node("viejo", branch_id="1")
    assert ap.registrar(producto, APROV, "viejo").prefijo == "S01"


@pytest.mark.parametrize("branch_id, base", [
    ("2", "S02"), ("117", "S117"), ("centro-2", "SCENTR"), ("ñ", "SX")])
def test_la_base_del_prefijo(branch_id, base):
    assert ap.prefijo_base(branch_id) == base
    assert len(base) <= ap.LARGO_DEL_PREFIJO - 2


def test_el_node_id_se_valida(producto):
    with pytest.raises(ap.ErrorDeAprovisionamiento, match="node_id no valido"):
        ap.registrar(producto, APROV, "nodo uno")


def test_una_guarda_del_producto_frena_el_alta(producto):
    def guarda(conn, node_id, branch_id):
        raise ap.AltaRechazada(f"no a {node_id} en {branch_id}")

    with pytest.raises(ap.AltaRechazada, match="no a node-1 en 1"):
        ap.registrar(producto, _aprov(verificar_alta=guarda), "node-1")
    assert ap.sucursales_con_nodo(producto) == 0


def test_la_guarda_de_libracore(tres_sucursales):
    conn = tres_sucursales
    contratadas = {"adicionales": {"offline": 1}}
    aprov = _aprov(verificar_alta=ap.guarda_offline_de_libracore(PLANS, cfg=lambda: contratadas))

    with pytest.raises(ap.AltaRechazada, match="no incluye «Modo offline»"):
        ap.registrar(conn, aprov, "node-1", "1")

    conn.execute("INSERT INTO modulos (modulo, habilitado) VALUES ('offline', 1)")
    conn.commit()
    ap.registrar(conn, aprov, "node-1", "1")
    with pytest.raises(ap.AltaRechazada, match="para 1 sucursal"):
        ap.registrar(conn, aprov, "node-2", "2")
    # Re-registrar en una sucursal que ya cuenta no suma, aunque este al tope.
    ap.registrar(conn, aprov, "node-1", "1")

    contratadas["adicionales"]["offline"] = 2
    ap.registrar(conn, aprov, "node-2", "2")
    assert ap.sucursales_con_nodo(conn) == 2


def test_la_guarda_de_libracore_por_defecto_importa_plans(producto, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "plans", PLANS)
    with pytest.raises(ap.AltaRechazada, match="no incluye"):
        ap.registrar(producto, _aprov(verificar_alta=ap.guarda_offline_de_libracore()), "node-1")


def test_sin_libracore_la_guarda_del_adicional_no_registra(producto, monkeypatch):
    """La guarda vieja, para un producto que todavia la pase (desde ADR-014 la de por defecto es `sin_guarda`)."""
    import sys

    monkeypatch.setitem(sys.modules, "libracore.adicionales", None)
    aprov = ap.Aprovisionamiento(prefijo="x", tablas_de_referencia=["productos"],
                                 listar_sucursales=_sucursales, verificar_alta=ap.guarda_offline_de_libracore())
    with pytest.raises(ap.AltaRechazada, match="libracore no esta instalado"):
        ap.registrar(producto, aprov, "node-1")


# ── baja, estado, sucursales y vigilancia ───────────────────────────────────

def test_dar_de_baja(producto):
    lineas = []
    assert ap.dar_de_baja(producto, "node-9", salida=lineas.append) == 1
    ap.registrar(producto, APROV, "node-1")
    assert ap.dar_de_baja(producto, "node-1", salida=lineas.append) == 0
    assert ap.dar_de_baja(producto, "node-1", salida=lineas.append) == 0
    assert "ya estaba dado de baja" in lineas[-1]
    assert ap.sucursales_con_nodo(producto) == 0


def test_estado_y_sucursales(tres_sucursales):
    conn = tres_sucursales
    lineas = []
    ap.estado(conn, salida=lineas.append)
    assert "  (ninguna — falta correr `publicar`)" in lineas
    ap.registrar(conn, APROV, "node-1", "2")
    NodeRepository(conn).register_node("huerfano", branch_id="Centro")  # texto libre, de antes
    lineas.clear()
    ap.estado(conn, salida=lineas.append)
    assert any("node-1 (sucursal 2, prefijo S02) — activo" in linea for linea in lineas)
    assert any("prefijo SIN PREFIJO" in linea and "NUNCA" in linea for linea in lineas)

    lineas.clear()
    assert ap.sucursales(conn, APROV, salida=lineas.append) == 0
    assert "     2  Norte — activa, nodo node-1" in lineas
    assert "     3  Vieja — INACTIVA, sin nodo" in lineas
    assert "  huerfano (sucursal 'Centro')" in lineas


def _visto(conn, node_id, cuando):
    conn.execute("UPDATE node_identity SET last_seen_at = ? WHERE node_id = ?", (cuando, node_id))
    conn.commit()


AHORA = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_vigilar(tres_sucursales):
    conn = tres_sucursales
    lineas = []
    assert ap.vigilar(conn, 15, salida=lineas.append, ahora=AHORA) == 0
    assert "no hay nada que vigilar" in lineas[-1]

    ap.registrar(conn, APROV, "node-1", "1")
    ap.registrar(conn, APROV, "node-2", "2")
    _visto(conn, "node-1", (AHORA - timedelta(minutes=3)).isoformat())
    # Sin zona: se lee como UTC (antes levantaba TypeError en el repositorio).
    _visto(conn, "node-2", (AHORA - timedelta(minutes=5)).replace(tzinfo=None).isoformat())
    lineas.clear()
    assert ap.vigilar(conn, 15, salida=lineas.append, ahora=AHORA) == 0
    assert lineas[-1] == "Los 2 nodos dieron senales dentro de los 15 minutos."

    _visto(conn, "node-2", (AHORA - timedelta(hours=3)).isoformat())
    lineas.clear()
    assert ap.vigilar(conn, 15, salida=lineas.append, ahora=AHORA) == 1
    assert any("node-2 (sucursal 2) — SIN NOTICIAS, visto hace 3.0 h" in linea for linea in lineas)
    assert ap.vigilar(conn, 10_000, salida=lambda _: None, ahora=AHORA) == 0

    _visto(conn, "node-2", "no es una fecha")
    assert ap.vigilar(conn, 10_000, salida=lambda _: None, ahora=AHORA) == 1


def test_vigilar_un_nodo_nunca_visto_esta_callado(producto):
    ap.registrar(producto, APROV, "node-1")
    lineas = []
    assert ap.vigilar(producto, 15, salida=lineas.append) == 1
    assert any("NUNCA" in linea for linea in lineas)


def test_vigilar_solo_revocados_no_es_falso_verde(producto):
    ap.registrar(producto, APROV, "node-1")
    ap.dar_de_baja(producto, "node-1", salida=lambda _: None)
    lineas = []
    assert ap.vigilar(producto, 15, salida=lineas.append) == 0
    assert lineas[-1] == "No hay nodos activos: todos los registrados estan revocados."


def test_hace_cuanto():
    assert ap.hace_cuanto(None) .startswith("NUNCA")
    assert ap.hace_cuanto("x").startswith("fecha ilegible")
    assert ap.hace_cuanto(AHORA.isoformat(), AHORA) == "recien"
    assert ap.hace_cuanto((AHORA - timedelta(minutes=30)).isoformat(), AHORA) == "hace 30 min"


# ── la identidad ────────────────────────────────────────────────────────────

def test_nodo_actual():
    assert nodo_actual(entorno={}) is None
    assert nodo_actual(entorno={"LIBRAEDGE_NODE_ID": " node-1 "}) == "node-1"
    assert nodo_actual(entorno={"LIBRAEDGE_NODE_ID": ""}) is None
    viejo = {"RESTOLIBRA_EDGE_NODE_ID": "node-1"}
    assert nodo_actual(entorno=viejo) is None, "sin prefijo no se mira la del producto"
    assert nodo_actual("restolibra", entorno=viejo) == "node-1"
    assert nodo_actual("restolibra", entorno={**viejo, "LIBRAEDGE_NODE_ID": "node-1"}) == "node-1"
    with pytest.raises(IdentidadEnConflicto, match="no coinciden"):
        nodo_actual("restolibra", entorno={**viejo, "LIBRAEDGE_NODE_ID": "node-2"})
    with pytest.raises(ValueError):
        variable_del_producto("")


def test_nodo_actual_lee_el_entorno(monkeypatch):
    monkeypatch.setenv("LIBRAEDGE_NODE_ID", "node-7")
    monkeypatch.setenv("LIBRAEDGE_PREFIJO", "S07")
    assert nodo_actual() == "node-7"
    assert prefijo_actual() == "S07"
    monkeypatch.delenv("LIBRAEDGE_PREFIJO")
    assert prefijo_actual() is None


# ── el CLI ──────────────────────────────────────────────────────────────────

@pytest.fixture
def conectar(producto, motor, tmp_path):
    """Una conexion NUEVA por llamada a la misma base: el CLI la cierra al terminar."""
    if motor == "sqlite":
        ruta = tmp_path / "central.db"
        destino = sqlite3.connect(ruta)
        producto.backup(destino)
        destino.close()
        return lambda base: sqlite3.connect(ruta)
    from libracore.db import core

    return lambda base: core.get_connection()


def test_el_cli_aprovisiona(conectar, motor, capsys, monkeypatch):
    from libraedge.central import main

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    monkeypatch.setattr(ap, "es_interactivo", lambda: False)

    if motor == "postgres":
        assert main(["nodo", "publicar"], conectar=conectar) == 0
        assert "Publicadas 3 de 3" in capsys.readouterr().out
    else:
        assert main(["nodo", "publicar"], conectar=conectar) == 1
        assert "PostgreSQL" in capsys.readouterr().err

    assert main(["nodo", "registrar", "node-1"], conectar=conectar) == 0
    salida = capsys.readouterr().out
    assert "prefijo S01" in salida and "LIBRAEDGE_PREFIJO=S01" in salida
    assert "LIBRAEDGE_TABLAS_ESPEJO=categorias:id,party_roles:party_id+role,productos:id" in salida

    assert main(["nodo", "registrar", "node-2", "--sucursal", "Sur"], conectar=conectar) == 1
    assert "ERROR: La sucursal 'Sur' no existe" in capsys.readouterr().err

    assert main(["nodo", "sucursales"], conectar=conectar) == 0
    assert "Centro — activa, nodo node-1" in capsys.readouterr().out
    assert main(["nodo", "estado"], conectar=conectar) == 0
    assert "node-1 (sucursal 1, prefijo S01)" in capsys.readouterr().out
    assert main(["nodo", "vigilar", "--umbral", "5"], conectar=conectar) == 1
    assert "SIN NOTICIAS" in capsys.readouterr().out
    assert main(["nodo", "dar-de-baja", "node-1"], conectar=conectar) == 0
    assert main(["nodo", "vigilar"], conectar=conectar) == 0


def test_el_cli_sin_aprovisionamiento(conectar, capsys, monkeypatch):
    from libraedge.central import main

    monkeypatch.delenv("LIBRAEDGE_APROVISIONAMIENTO", raising=False)
    monkeypatch.delenv("LIBRAEDGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("JUGUETE_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="Falta el aprovisionamiento"):
        main(["--base", "x", "nodo", "publicar"], conectar=conectar)
    with pytest.raises(SystemExit, match="no es 'modulo:objeto'"):
        main(["--base", "x", "nodo", "--aprovisionamiento", "malo", "sucursales"], conectar=conectar)
    # Lo que no es del producto anda sin el aprovisionamiento.
    assert main(["--base", "x", "nodo", "vigilar"], conectar=conectar) == 0
    # La base del producto: <PREFIJO>_DATABASE_URL.
    with pytest.raises(SystemExit, match="JUGUETE_DATABASE_URL"):
        main(["nodo", "--aprovisionamiento", "tests.test_aprovisionamiento:APROV", "sucursales"],
             conectar=conectar)
    monkeypatch.setenv("JUGUETE_DATABASE_URL", "x")
    assert main(["nodo", "--aprovisionamiento", "tests.test_aprovisionamiento:APROV", "sucursales"],
                conectar=conectar) == 0


def test_el_cli_usa_el_conectar_del_producto(conectar, capsys, monkeypatch):
    from libraedge.central import main

    monkeypatch.delenv("LIBRAEDGE_DATABASE_URL", raising=False)
    # El objeto que carga el CLI (pytest importa este archivo con otro nombre de modulo).
    cargado = ap.cargar_aprovisionamiento("tests.test_aprovisionamiento:APROV")
    monkeypatch.setattr(cargado, "conectar", lambda: conectar(None))
    assert main(["nodo", "--aprovisionamiento", "tests.test_aprovisionamiento:APROV", "sucursales"],
                conectar=lambda base: pytest.fail("no tenia que usar la base")) == 0
    assert "Centro" in capsys.readouterr().out


def test_una_base_de_antes_suma_la_columna_del_prefijo():
    from libraedge.db.schema import init_schema

    vieja = sqlite3.connect(":memory:")
    vieja.execute("""CREATE TABLE node_identity (node_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL,
        installed_at TEXT NOT NULL, schema_version INTEGER NOT NULL, last_server_cursor TEXT,
        active INTEGER NOT NULL DEFAULT 1, secret_hash TEXT NOT NULL DEFAULT '')""")
    vieja.execute("INSERT INTO node_identity (node_id, branch_id, installed_at, schema_version)"
                  " VALUES ('a', '1', 'x', 1), ('b', '1', 'x', 1)")
    init_schema(vieja)
    init_schema(vieja)  # idempotente
    assert [f[0] for f in vieja.execute("SELECT prefijo FROM node_identity")] == [None, None]
    vieja.execute("UPDATE node_identity SET prefijo = 'S01' WHERE node_id = 'a'")
    with pytest.raises(sqlite3.IntegrityError):
        vieja.execute("UPDATE node_identity SET prefijo = 'S01' WHERE node_id = 'b'")


def test_registrar_con_carga_escribe_el_archivo_del_instalador(conectar, capsys, monkeypatch, tmp_path):
    """ADR-010: con --carga el secreto no se imprime; va a un archivo 600 con la URL y el emisor."""
    import json
    import stat

    from libraedge.central import main
    from libraedge.instalador import leer_carga

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ap, "es_interactivo", lambda: False)
    (tmp_path / "config.json").write_text(json.dumps({
        "empresa_nombre": "Comercio de Prueba S.A.", "empresa_cuit": "30-99999904-9",
        "empresa_iva_condition": "Responsable Inscripto", "mp_access_token": "APP-USR-no-viaja"}), encoding="utf-8")
    ruta = tmp_path / "carga-nodo.json"

    assert main(["nodo", "registrar", "node-1", "--carga", str(ruta), "--central-url",
                 "https://juguete.ejemplo.com.ar/"], conectar=conectar) == 0
    salida = capsys.readouterr().out
    assert "LIBRAEDGE_NODE_SECRET" not in salida and "prefijo S01" in salida and str(ruta) in salida
    assert stat.S_IMODE(ruta.stat().st_mode) == 0o600
    carga = leer_carga(str(ruta), "juguete")
    assert carga.entorno["LIBRAEDGE_CENTRAL_URL"] == "https://juguete.ejemplo.com.ar"
    assert carga.entorno["LIBRAEDGE_PREFIJO"] == "S01"
    assert carga.entorno["LIBRAEDGE_TABLAS_ESPEJO"] == "categorias:id,party_roles:party_id+role,productos:id"
    assert dict(carga.sucursal) == {"id": "1", "nombre": "Centro"}
    assert carga.emisor == {"empresa_nombre": "Comercio de Prueba S.A.", "empresa_cuit": "30-99999904-9",
                            "empresa_iva_condition": "Responsable Inscripto"}
    assert carga.entorno["LIBRAEDGE_NODE_SECRET"] not in salida

    # No pisa una carga que ya esta, y lo dice ANTES de registrar (un alta emite un secreto nuevo).
    assert main(["nodo", "registrar", "node-1", "--carga", str(ruta), "--central-url",
                 "https://juguete.ejemplo.com.ar"], conectar=conectar) == 1
    assert "ya existe" in capsys.readouterr().err
    assert leer_carga(str(ruta), "juguete").entorno == carga.entorno


@pytest.mark.parametrize("argumentos, error", [
    (["--carga", "{carga}"], "--central-url"),
    (["--carga", "{carga}", "--central-url", "http://juguete.com.ar"], "https"),
    (["--central-url", "https://juguete.com.ar"], "van con --carga"),
    (["--carga", "{carga}", "--central-url", "https://j.com.ar", "--config-emisor", "{cuit_malo}"], "CUIT"),
])
def test_registrar_con_carga_valida_antes_de_dar_el_alta(conectar, capsys, monkeypatch, tmp_path, argumentos, error):
    import json

    from libraedge.central import main

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ap, "es_interactivo", lambda: False)
    cuit_malo = tmp_path / "malo.json"
    cuit_malo.write_text(json.dumps({"empresa_cuit": "30-99999904-1"}), encoding="utf-8")
    argumentos = [a.format(carga=tmp_path / "c.json", cuit_malo=cuit_malo) for a in argumentos]
    assert main(["nodo", "registrar", "node-1", *argumentos], conectar=conectar) == 1
    assert error in capsys.readouterr().err
    assert not (tmp_path / "c.json").exists()
    assert main(["nodo", "estado"], conectar=conectar) == 0
    assert "node-1" not in capsys.readouterr().out  # no se registro nada


def test_registrar_con_carga_sin_config_del_central_avisa(conectar, capsys, monkeypatch, tmp_path):
    from libraedge.central import main
    from libraedge.instalador import leer_carga

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ap, "es_interactivo", lambda: False)
    ruta = tmp_path / "carga.json"
    assert main(["nodo", "registrar", "node-1", "--carga", str(ruta), "--central-url", "https://j.com.ar"],
                conectar=conectar) == 0
    assert "no va a poder facturar" in capsys.readouterr().out
    assert leer_carga(str(ruta), "juguete").emisor == {}


def test_registrar_con_carga_avisa_el_emisor_incompleto_y_un_fallo_al_escribir(conectar, capsys, monkeypatch,
                                                                                tmp_path):
    import json

    from libraedge import central
    from libraedge.central import main

    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", "x")
    monkeypatch.setenv("LIBRAEDGE_APROVISIONAMIENTO", "tests.test_aprovisionamiento:APROV")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ap, "es_interactivo", lambda: False)
    (tmp_path / "config.json").write_text(json.dumps({"empresa_nombre": "Comercio de Prueba S.A."}),
                                          encoding="utf-8")
    assert main(["nodo", "registrar", "node-1", "--carga", str(tmp_path / "a.json"), "--central-url",
                 "https://j.com.ar"], conectar=conectar) == 0
    assert "no tiene empresa_cuit, empresa_iva_condition" in capsys.readouterr().out

    def falla(ruta, carga):
        raise OSError("disco lleno")

    monkeypatch.setattr(central, "escribir_carga", falla)
    assert main(["nodo", "registrar", "node-1", "--carga", str(tmp_path / "b.json"), "--central-url",
                 "https://j.com.ar"], conectar=conectar) == 1
    error = capsys.readouterr().err
    assert "quedo registrado" in error and "mismo node_id" in error
