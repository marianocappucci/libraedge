"""El código de vinculación (ADR-014): el nodo se activa con «dirección + código» en lugar del archivo.

Con el producto de juguete de `test_aprovisionamiento.py`, contra los dos motores. Lo que se cuida:

1. **El secreto no viaja con el código**: sale recién en el canje, en una respuesta sin caché; el código tampoco se
   guarda (sólo su hash) ni se loguea.
2. **Un solo uso, 15 minutos, anulable**: vencido, usado, anulado o inventado dan el mismo 404; pedir otro anula el
   anterior; la baja y el archivo de soporte también.
3. **Dos canjes simultáneos: uno solo gana** (la marca de usado es una sola sentencia).
4. **El límite de intentos** (por IP y global) corta antes de mirar el código y dice cuándo reintentar.
5. Sólo https, y un instalador de otro producto no gasta el código.
"""

import contextlib
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from libraedge import aprovisionamiento as ap
from libraedge import vinculacion as vinc
from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.instalador import carga_desde_dict
from libraedge.pantalla_del_central import create_canje_router, create_nodos_router, direccion_del_central
from tests.test_aprovisionamiento import _crear_producto, _sucursales
from tests.test_pantalla_del_central import CENTRAL, CUIT_FICTICIO

INICIO = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


class Reloj:
    """El reloj de los códigos, que el test adelanta a mano."""

    def __init__(self):
        self.ahora = INICIO

    def __call__(self):
        return self.ahora

    def adelantar(self, **cuanto):
        self.ahora += timedelta(**cuanto)


@pytest.fixture
def producto(conn):
    return _crear_producto(conn, sucursales=(("1", "Centro", 1), ("2", "Norte", 1)))


@pytest.fixture
def config(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"empresa_nombre": "Comercio de Prueba SA", "empresa_cuit": CUIT_FICTICIO,
                                "empresa_iva_condition": "RI"}), encoding="utf-8")
    return ruta


@pytest.fixture
def reloj():
    return Reloj()


def _app(conn, config, reloj, *, abrir=None, **canje):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    aprov = ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias", "productos"],
                                 listar_sucursales=_sucursales)
    abrir = abrir or (lambda: contextlib.nullcontext(conn))
    entorno = {"LIBRAEDGE_CENTRAL_URL": CENTRAL}
    app = FastAPI()
    app.include_router(create_nodos_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno,
                                           ahora=reloj))
    canje.setdefault("ip_del_request", lambda r: r.headers.get("x-prueba-ip", "198.51.100.7"))
    app.include_router(create_canje_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno,
                                           ahora=reloj, **canje))
    return TestClient(app, base_url="https://central.prueba")


@pytest.fixture
def cliente(producto, config, reloj):
    return _app(producto, config, reloj)


def _alta(cliente, sucursal="1"):
    r = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": sucursal})
    assert r.status_code == 200, r.text
    return r.json()


def _canjear(cliente, codigo, producto="juguete", **cabeceras):
    return cliente.post("/sync/v1/vinculacion", json={"codigo": codigo, "producto": producto}, headers=cabeceras)


# ── el código ───────────────────────────────────────────────────────────────

def test_el_codigo_es_de_8_simbolos_sin_caracteres_confusos():
    codigos = {vinc.generar_codigo() for _ in range(300)}
    assert len(codigos) == 300
    for codigo in codigos:
        assert len(codigo) == 9 and codigo[4] == "-"
        assert set(codigo.replace("-", "")) <= set(vinc.ALFABETO)
    assert not set("01ILO") & set(vinc.ALFABETO) and len(vinc.ALFABETO) == 31


def test_normalizar_acepta_minusculas_y_separadores_y_nada_mas():
    assert vinc.normalizar_codigo(" k7q4-m2xp ") == "K7Q4M2XP"
    assert vinc.normalizar_codigo("K7Q4 M2XP") == "K7Q4M2XP"
    # Una O, un 0, un 1 o una I no se «corrigen»: no son del alfabeto.
    for malo in ("K7Q4-M2X0", "K7Q4-M2XO", "K7Q4-M2X1", "K7Q4-M2XI", "K7Q-4M2", "", "K7Q4-M2XPA"):
        assert vinc.normalizar_codigo(malo) is None
    assert vinc.hash_del_codigo("k7q4-m2xp") == vinc.hash_del_codigo("K7Q4M2XP")
    with pytest.raises(ValueError):
        vinc.hash_del_codigo("no")


# ── emitir ──────────────────────────────────────────────────────────────────

def test_el_alta_con_codigo_registra_el_nodo_y_no_trae_el_secreto(cliente, producto):
    r = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "2"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    datos = r.json()
    assert datos["direccion"] == "demo.juguete.com.ar" and datos["central_url"] == CENTRAL
    assert vinc.normalizar_codigo(datos["codigo"]) and datos["node_id"].startswith("nodo-2-")
    assert datos["prefijo"] == "S02" and datos["sucursal"] == {"id": "2", "nombre": "Norte"}
    assert datos["vence"] == (INICIO + timedelta(minutes=15)).isoformat() and datos["vinculacion_minutos"] == 15
    assert "SECRET" not in r.text and "secret" not in r.text
    # En la base: el nodo y el hash del código, nunca el código.
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 1
    guardado = producto.execute("SELECT codigo_hash FROM libraedge_vinculacion").fetchall()
    assert [f[0] for f in guardado] == [vinc.hash_del_codigo(datos["codigo"])]
    # El estado dice que hay un código pendiente, sin mostrarlo.
    estado = cliente.get("/api/nodos-offline")
    [nodo] = estado.json()["nodos"]
    assert nodo["vinculacion"] == {"vence": datos["vence"]}
    assert datos["codigo"] not in estado.text


def test_las_guardas_del_alta_con_codigo_son_las_de_siempre(cliente, producto, config, reloj):
    assert cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "99"}).status_code == 422
    _alta(cliente, "1")
    r = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"})
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "sucursal_con_nodo"
    assert producto.execute("SELECT COUNT(*) FROM libraedge_vinculacion").fetchone()[0] == 1


# ── canjear ─────────────────────────────────────────────────────────────────

def test_el_canje_devuelve_la_carga_que_acepta_el_instalador(cliente, producto, caplog):
    caplog.set_level(logging.DEBUG)
    datos = _alta(cliente, "2")
    r = _canjear(cliente, datos["codigo"].lower())  # como lo tipea cualquiera
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    carga = carga_desde_dict(r.json(), "juguete")
    assert carga.entorno["LIBRAEDGE_NODE_ID"] == datos["node_id"]
    assert carga.entorno["LIBRAEDGE_CENTRAL_URL"] == CENTRAL and carga.entorno["LIBRAEDGE_PREFIJO"] == "S02"
    assert carga.emisor["empresa_cuit"] == CUIT_FICTICIO and carga.sucursal == {"id": "2", "nombre": "Norte"}
    secreto = carga.entorno["LIBRAEDGE_NODE_SECRET"]
    assert NodeRepository(producto).verify_node_secret(datos["node_id"], secreto)
    # El código ya no está pendiente.
    assert cliente.get("/api/nodos-offline").json()["nodos"][0]["vinculacion"] is None
    # 🔴 Ni el código ni el secreto en los logs.
    assert caplog.records and datos["codigo"] not in caplog.text and secreto not in caplog.text
    assert vinc.normalizar_codigo(datos["codigo"]) not in caplog.text


def test_un_codigo_sirve_una_sola_vez(cliente):
    datos = _alta(cliente)
    assert _canjear(cliente, datos["codigo"]).status_code == 200
    r = _canjear(cliente, datos["codigo"])
    assert r.status_code == 404
    assert r.json()["detail"] == {"codigo": "codigo_invalido", "mensaje": vinc.MENSAJE_CODIGO_INVALIDO}


def test_un_codigo_vencido_no_sirve_y_da_el_mismo_error(cliente, reloj):
    datos = _alta(cliente)
    reloj.adelantar(minutes=15)
    r = _canjear(cliente, datos["codigo"])
    assert r.status_code == 404 and r.json()["detail"]["codigo"] == "codigo_invalido"
    assert cliente.get("/api/nodos-offline").json()["nodos"][0]["vinculacion"] is None


def test_un_codigo_inventado_o_mal_formado_da_el_mismo_error(cliente):
    _alta(cliente)
    for codigo in ("ABCD-EFGH", "nada", ""):
        r = _canjear(cliente, codigo)
        assert r.status_code == 404 and r.json()["detail"]["codigo"] == "codigo_invalido"


def test_pedir_otro_anula_el_anterior_y_el_secreto_cambia_recien_al_canjear(cliente, producto):
    primero = _alta(cliente)
    node_id = primero["node_id"]
    carga = _canjear(cliente, primero["codigo"]).json()
    secreto_viejo = carga["entorno"]["LIBRAEDGE_NODE_SECRET"]

    segundo = cliente.post(f"/api/nodos-offline/{node_id}/vinculacion").json()
    tercero = cliente.post(f"/api/nodos-offline/{node_id}/vinculacion").json()
    assert segundo["codigo"] != tercero["codigo"] and tercero["prefijo"] == primero["prefijo"]
    assert tercero["sucursal"] == {"id": "1", "nombre": "Centro"}
    repo = NodeRepository(producto)
    assert repo.verify_node_secret(node_id, secreto_viejo), "el nodo instalado sigue andando hasta el canje"
    assert _canjear(cliente, segundo["codigo"]).status_code == 404, "pedir otro anula el anterior"
    nueva = _canjear(cliente, tercero["codigo"]).json()
    assert repo.verify_node_secret(node_id, nueva["entorno"]["LIBRAEDGE_NODE_SECRET"])
    assert not repo.verify_node_secret(node_id, secreto_viejo)


def test_anular_el_codigo(cliente):
    datos = _alta(cliente)
    r = cliente.delete(f"/api/nodos-offline/{datos['node_id']}/vinculacion")
    assert r.json() == {"node_id": datos["node_id"], "anulados": 1}
    assert _canjear(cliente, datos["codigo"]).status_code == 404
    assert cliente.delete(f"/api/nodos-offline/{datos['node_id']}/vinculacion").json()["anulados"] == 0
    assert cliente.delete("/api/nodos-offline/no-existe/vinculacion").status_code == 404


def test_la_baja_anula_el_codigo_y_no_se_puede_pedir_otro(cliente):
    datos = _alta(cliente)
    assert cliente.delete(f"/api/nodos-offline/{datos['node_id']}").status_code == 200
    assert _canjear(cliente, datos["codigo"]).status_code == 404
    r = cliente.post(f"/api/nodos-offline/{datos['node_id']}/vinculacion")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "nodo_dado_de_baja"
    assert cliente.post("/api/nodos-offline/no-existe/vinculacion").status_code == 404


def test_el_archivo_de_soporte_anula_el_codigo_pendiente(cliente, producto):
    datos = _alta(cliente)
    archivo = cliente.post(f"/api/nodos-offline/{datos['node_id']}/carga")
    assert archivo.status_code == 200
    secreto = archivo.json()["entorno"]["LIBRAEDGE_NODE_SECRET"]
    assert _canjear(cliente, datos["codigo"]).status_code == 404, "el código ya no puede pisar el secreto del archivo"
    assert NodeRepository(producto).verify_node_secret(datos["node_id"], secreto)


def test_un_nodo_dado_de_baja_por_afuera_no_gasta_el_codigo(cliente, producto):
    """Una carrera con la baja (el CLI no anula códigos): el canje no le devuelve un secreto a un nodo dado de baja."""
    datos = _alta(cliente)
    NodeRepository(producto).deactivate_node(datos["node_id"])
    assert _canjear(cliente, datos["codigo"]).status_code == 404
    assert producto.execute("SELECT usado_at FROM libraedge_vinculacion").fetchone()[0] is None


def test_un_instalador_de_otro_producto_no_gasta_el_codigo(cliente):
    datos = _alta(cliente)
    r = _canjear(cliente, datos["codigo"], producto="otroproducto")
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "otro_producto"
    assert _canjear(cliente, datos["codigo"]).status_code == 200


def test_solo_https(producto, config, reloj):
    datos = _alta(_app(producto, config, reloj), "1")
    from fastapi.testclient import TestClient

    cliente = _app(producto, config, reloj)
    por_http = TestClient(cliente.app, base_url="http://central.prueba")
    r = _canjear(por_http, datos["codigo"])
    assert r.status_code == 403 and r.json()["detail"]["codigo"] == "solo_https"
    # Detrás del proxy que termina TLS, lo dice X-Forwarded-Proto.
    assert _canjear(por_http, datos["codigo"], **{"x-forwarded-proto": "https"}).status_code == 200


def test_si_la_guarda_del_producto_rechaza_el_codigo_no_se_gasta(producto, config, reloj):
    datos = _alta(_app(producto, config, reloj))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def guarda(conn, node_id, branch_id):
        raise ap.AltaRechazada("No.")

    aprov = ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias", "productos"],
                                 listar_sucursales=_sucursales, verificar_alta=guarda)
    app = FastAPI()
    app.include_router(create_canje_router(aprov, lambda: contextlib.nullcontext(producto),
                                           ruta_config=lambda: str(config), ahora=reloj,
                                           entorno={"LIBRAEDGE_CENTRAL_URL": CENTRAL}))
    r = _canjear(TestClient(app, base_url="https://central.prueba"), datos["codigo"])
    assert r.status_code == 403 and r.json()["detail"]["codigo"] == "alta_rechazada"
    assert _canjear(_app(producto, config, reloj), datos["codigo"]).status_code == 200


def test_sin_url_publica_o_con_el_cuit_roto_no_se_gasta_el_codigo(producto, config, reloj, tmp_path):
    datos = _alta(_app(producto, config, reloj))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    aprov = ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias"], listar_sucursales=_sucursales)
    roto = tmp_path / "roto.json"
    roto.write_text(json.dumps({"empresa_cuit": "20-12345678-0"}), encoding="utf-8")
    for entorno, ruta in (({}, config), ({"LIBRAEDGE_CENTRAL_URL": CENTRAL}, roto)):
        app = FastAPI()
        app.include_router(create_canje_router(aprov, lambda: contextlib.nullcontext(producto),
                                               ruta_config=lambda r=ruta: str(r), ahora=reloj, entorno=entorno))
        r = _canjear(TestClient(app, base_url="https://interno:8000"), datos["codigo"])
        assert r.status_code == 503, r.text
    assert _canjear(_app(producto, config, reloj), datos["codigo"]).status_code == 200


# ── el límite de intentos ───────────────────────────────────────────────────

def test_el_limite_por_ip_corta_antes_de_mirar_el_codigo(cliente, reloj, producto):
    datos = _alta(cliente)
    for _ in range(vinc.MAXIMO_POR_IP):
        assert _canjear(cliente, "ABCD-EFGH").status_code == 404
    r = _canjear(cliente, datos["codigo"])  # el bueno, pero desde la IP bloqueada
    assert r.status_code == 429 and r.json()["detail"]["codigo"] == "demasiados_intentos"
    assert 1 <= int(r.headers["retry-after"]) <= 15 * 60 + 1
    # El bloqueado no suma intentos (no se extiende solo) y el código no se gastó.
    assert producto.execute("SELECT COUNT(*) FROM libraedge_canje_fallido").fetchone()[0] == vinc.MAXIMO_POR_IP
    # Otra IP sí puede.
    assert _canjear(cliente, datos["codigo"], **{"x-prueba-ip": "203.0.113.9"}).status_code == 200


def test_la_ventana_del_limite_se_libera_sola(cliente, reloj):
    for _ in range(vinc.MAXIMO_POR_IP):
        _canjear(cliente, "ABCD-EFGH")
    assert _canjear(cliente, "ABCD-EFGH").status_code == 429
    reloj.adelantar(minutes=15, seconds=1)
    datos = _alta(cliente)
    assert _canjear(cliente, datos["codigo"]).status_code == 200


def test_el_limite_global_vale_aunque_cambie_la_ip(producto, config, reloj):
    cliente = _app(producto, config, reloj, maximo_por_ip=100, maximo_global=5)
    datos = _alta(cliente)
    for i in range(5):
        assert _canjear(cliente, "ABCD-EFGH", **{"x-prueba-ip": f"203.0.113.{i}"}).status_code == 404
    r = _canjear(cliente, datos["codigo"], **{"x-prueba-ip": "198.51.100.200"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_un_canje_bueno_no_cuenta_como_fallido(cliente, producto):
    _canjear(cliente, _alta(cliente, "1")["codigo"])
    _canjear(cliente, _alta(cliente, "2")["codigo"])
    assert producto.execute("SELECT COUNT(*) FROM libraedge_canje_fallido").fetchone()[0] == 0


def test_la_entropia_y_el_limite_hacen_impracticable_adivinar():
    """La cuenta del docstring de `libraedge.vinculacion`: con el tope global, en la vida de un código entran como
    mucho dos ventanas de intentos, y acertar uno vivo queda muy por debajo de una en mil millones."""
    espacio = len(vinc.ALFABETO) ** vinc.LARGO
    intentos = 2 * vinc.MAXIMO_GLOBAL * (vinc.VIGENCIA / vinc.VENTANA)
    assert espacio > 8 * 10**11
    assert intentos / espacio < 10**-9


# ── dos canjes a la vez ─────────────────────────────────────────────────────

@pytest.fixture
def abrir_otra(motor, tmp_path):
    """Una fábrica de conexiones NUEVAS a la misma base (la de `conn` en PostgreSQL; un archivo en SQLite)."""
    if motor == "postgres":
        from libracore.db import core

        return core.get_connection
    ruta = str(tmp_path / "central.db")
    base = sqlite3.connect(ruta)
    init_schema(base)
    base.execute("INSERT INTO node_identity (node_id, branch_id, installed_at, schema_version, active, secret_hash)"
                 " VALUES ('nodo-1', '1', '2026-10-20', 1, 1, 'x')")
    base.commit()
    base.close()
    return lambda: sqlite3.connect(ruta, timeout=10, check_same_thread=False)


def test_dos_canjes_simultaneos_uno_solo_gana(conn, motor, abrir_otra):
    if motor == "postgres":
        conn.execute("INSERT INTO node_identity (node_id, branch_id, installed_at, schema_version, active, secret_hash)"
                     " VALUES ('nodo-1', '1', '2026-10-20', 1, 1, 'x')")
        conn.commit()
        emisora = conn
    else:
        emisora = abrir_otra()
    emitido = vinc.emitir(emisora, "nodo-1", ahora=INICIO)
    emisora.commit()

    resultados: list[str | None] = []
    listos = threading.Barrier(2)

    def canjear():
        otra = abrir_otra()
        try:
            listos.wait()
            resultado = vinc.canjear(otra, emitido.codigo, ahora=INICIO + timedelta(minutes=1))
            time.sleep(0.3)  # con la fila tomada: el otro tiene que esperar y no encontrarla
            otra.commit()
            resultados.append(resultado)
        finally:
            otra.close()

    hilos = [threading.Thread(target=canjear) for _ in range(2)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(30)
    assert len(resultados) == 2 and resultados.count("nodo-1") == 1 and resultados.count(None) == 1


# ── piezas ──────────────────────────────────────────────────────────────────

def test_direccion_del_central():
    assert direccion_del_central("https://sistema.contalibra.com.ar") == "sistema.contalibra.com.ar"
    assert direccion_del_central("https://h.example:8443/sub") == "h.example:8443/sub"
    assert direccion_del_central("http://localhost:8000") == "http://localhost:8000"


def test_la_guarda_por_defecto_ya_no_es_la_del_adicional():
    """LibraEdge es parte del sistema (ADR-014): un `Aprovisionamiento` sin guarda no frena ningún alta."""
    aprov = ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias"], listar_sucursales=_sucursales)
    assert aprov.verificar_alta is ap.sin_guarda


def test_las_tablas_nacen_con_init_schema(conn):
    for tabla in ("libraedge_vinculacion", "libraedge_canje_fallido"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] == 0
    init_schema(conn)  # idempotente
