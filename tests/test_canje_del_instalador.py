"""El ayudante del instalador canjea el código de vinculación por la carga (ADR-014).

Contra un central de verdad (el router de `pantalla_del_central` en un uvicorn local, por http a 127.0.0.1, que es lo
único que `normalizar_url_del_central` acepta sin https) y con respuestas armadas para los errores. Lo que se cuida:
el código no viaja por la línea de comandos, el secreto no se imprime y la carga se escribe con permisos de secreto.
"""

import contextlib
import io
import json
import os
import socket
import sqlite3
import stat
import threading
import time
import urllib.error

import pytest

from libraedge import aprovisionamiento as ap
from libraedge import instalador as ins
from libraedge.db.schema import init_schema
from tests.test_aprovisionamiento import _sucursales
from tests.test_instalador import CARPETA

CUIT_FICTICIO = "20-12345678-6"


# ── la dirección y el código ────────────────────────────────────────────────

@pytest.mark.parametrize(("entrada", "sale"), [
    ("sistema.contalibra.com.ar", "https://sistema.contalibra.com.ar"),
    (" https://sistema.contalibra.com.ar/ ", "https://sistema.contalibra.com.ar"),
    ("h.example:8443/sub", "https://h.example:8443/sub"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
])
def test_normalizar_direccion(entrada, sale):
    assert ins.normalizar_direccion(entrada) == sale


@pytest.mark.parametrize("mala", ["", "http://sistema.contalibra.com.ar", "https://u:p@h.example", "h.example/?x=1"])
def test_una_direccion_mala_se_rechaza(mala):
    with pytest.raises(ins.ErrorDeInstalacion, match="direccion"):
        ins.normalizar_direccion(mala)


def test_el_codigo_sale_del_entorno_o_de_un_archivo_nunca_de_un_argumento(tmp_path):
    assert ins.leer_codigo(entorno={"LIBRAEDGE_CODIGO": " k7q4 m2xp\n"}) == "K7Q4-M2XP"
    archivo = tmp_path / "codigo.txt"
    archivo.write_text("K7Q4-M2XP\r\n", encoding="utf-8")
    assert ins.leer_codigo(str(archivo), entorno={}) == "K7Q4-M2XP"
    with pytest.raises(ins.ErrorDeInstalacion, match="LIBRAEDGE_CODIGO"):
        ins.leer_codigo(entorno={})
    with pytest.raises(ins.ErrorDeInstalacion, match="vacio"):
        ins.leer_codigo(str(tmp_path / "no-esta.txt"), entorno={})
    with pytest.raises(ins.ErrorDeInstalacion, match="formato") as exc:
        ins.leer_codigo(entorno={"LIBRAEDGE_CODIGO": "K7Q4-M2X0"})
    assert "M2X0" not in str(exc.value), "el error no repite lo que se tipeó"
    # El parser no tiene un --codigo: el código no pasa por la línea de comandos (Inno y el transcript la loguean).
    with pytest.raises(SystemExit):
        ins._parser().parse_args(["canjear", "--definicion", "d", "--direccion", "x", "--salida", "s",
                                  "--codigo", "K7Q4-M2XP"])


# ── contra un central de verdad ─────────────────────────────────────────────

def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def central(tmp_path):
    """Un central de VentaLibra de juguete: SQLite en archivo, el router de administración y el de canje."""
    import uvicorn
    from fastapi import FastAPI

    from libraedge.pantalla_del_central import create_canje_router, create_nodos_router

    ruta_base = str(tmp_path / "central.db")
    base = sqlite3.connect(ruta_base)
    init_schema(base)
    base.execute("CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL)")
    base.execute("INSERT INTO branches VALUES (1, 'Centro', 1)")
    base.execute("CREATE TABLE categorias (id INTEGER PRIMARY KEY, nombre TEXT)")
    base.commit()
    base.close()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"empresa_cuit": CUIT_FICTICIO, "empresa_iva_condition": "RI"}), encoding="utf-8")

    @contextlib.contextmanager
    def abrir():
        conexion = sqlite3.connect(ruta_base, timeout=10, check_same_thread=False)
        try:
            yield conexion
        finally:
            conexion.close()

    aprov = ap.Aprovisionamiento(prefijo="ventalibra", tablas_de_referencia=["categorias"],
                                 listar_sucursales=_sucursales)
    puerto = _puerto_libre()
    entorno = {"LIBRAEDGE_CENTRAL_URL": "https://sistema.ventalibra.example"}
    app = FastAPI()
    app.include_router(create_nodos_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno))
    app.include_router(create_canje_router(aprov, abrir, ruta_config=lambda: str(config), entorno=entorno,
                                           exigir_https=False))
    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=puerto, log_level="warning"))
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    for _ in range(100):
        if servidor.started:
            break
        time.sleep(0.05)
    from fastapi.testclient import TestClient

    admin = TestClient(app, base_url="https://sistema.ventalibra.example")
    yield f"http://127.0.0.1:{puerto}", admin
    servidor.should_exit = True
    hilo.join(10)


def test_canjear_escribe_la_carga_con_permisos_de_secreto(central, tmp_path, monkeypatch, capsys):
    direccion, admin = central
    codigo = admin.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()["codigo"]
    monkeypatch.setenv("LIBRAEDGE_CODIGO", codigo)
    salida = tmp_path / "carga-nodo.json"

    assert ins.main(["canjear", "--definicion", str(CARPETA / "productos" / "ventalibra.json"),
                     "--direccion", direccion, "--salida", str(salida)]) == 0
    resumen = json.loads(capsys.readouterr().out)
    assert resumen["producto"] == "ventalibra" and resumen["central"] == "https://sistema.ventalibra.example"
    carga = ins.leer_carga(str(salida), "ventalibra")
    secreto = carga.entorno["LIBRAEDGE_NODE_SECRET"]
    assert secreto not in json.dumps(resumen) and codigo not in json.dumps(resumen)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(salida).st_mode) == 0o600

    # Un solo uso: el segundo canje falla con el mensaje del central y no toca el archivo.
    antes = salida.read_text(encoding="utf-8")
    assert ins.main(["canjear", "--definicion", str(CARPETA / "productos" / "ventalibra.json"),
                     "--direccion", direccion, "--salida", str(salida)]) == 2
    err = capsys.readouterr().err
    assert "no es válido o ya venció" in err and codigo not in err
    assert salida.read_text(encoding="utf-8") == antes


def test_un_instalador_de_otro_producto_no_gasta_el_codigo(central, tmp_path):
    direccion, admin = central
    codigo = admin.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()["codigo"]
    with pytest.raises(ins.ErrorDeInstalacion, match="ventalibra y el instalador es de contalibra"):
        ins.canjear_codigo(direccion, codigo, "contalibra")
    assert ins.canjear_codigo(direccion, codigo, "ventalibra")["producto"] == "ventalibra"


def test_sobre_un_archivo_que_ya_existe_conserva_sus_permisos(tmp_path):
    ruta = tmp_path / "carga.json"
    ruta.write_text("viejo", encoding="utf-8")
    os.chmod(ruta, 0o600)
    ins.escribir_carga_canjeada(str(ruta), {"a": 1})
    assert json.loads(ruta.read_text(encoding="utf-8")) == {"a": 1}
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600


# ── los errores ─────────────────────────────────────────────────────────────

def _http_error(codigo, cuerpo, cabeceras=None):
    import email.message

    mensaje = email.message.Message()
    for clave, valor in (cabeceras or {}).items():
        mensaje[clave] = valor
    return urllib.error.HTTPError("https://x/sync/v1/vinculacion", codigo, "error", mensaje,
                                  io.BytesIO(json.dumps(cuerpo).encode()))


@pytest.mark.parametrize(("error", "esperado"), [
    (_http_error(404, {"detail": {"codigo": "codigo_invalido", "mensaje": "El código no es válido."}}),
     "El código no es válido."),
    (_http_error(429, {"detail": {"codigo": "demasiados_intentos", "mensaje": "Demasiados intentos."}},
                 {"Retry-After": "120"}), "reintentar en 120 segundos"),
    (_http_error(503, {"detail": {"codigo": "emisor_invalido", "mensaje": "El config.json del sistema: CUIT"}}),
     "contesto 503 al canjear el codigo: El config.json"),
    (_http_error(500, "no es json"), "contesto 500"),
    (urllib.error.URLError(ConnectionRefusedError("rechazada")), "No se pudo conectar con https://h.example"),
    (TimeoutError(), "No se pudo conectar"),
])
def test_los_errores_dicen_que_paso_sin_el_codigo(error, esperado):
    def abrir(_pedido):
        raise error

    with pytest.raises(ins.ErrorDeInstalacion, match=esperado) as exc:
        ins.canjear_codigo("h.example", "K7Q4-M2XP", "ventalibra", abrir=abrir)
    assert "K7Q4" not in str(exc.value)


def test_una_respuesta_que_no_es_una_carga_se_rechaza():
    class Respuesta(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with pytest.raises(ins.ErrorDeInstalacion, match="no contesto una carga"):
        ins.canjear_codigo("h.example", "K7Q4-M2XP", "ventalibra", abrir=lambda p: Respuesta(b"<html>"))
    with pytest.raises(ins.ErrorDeInstalacion, match="formato"):
        ins.canjear_codigo("h.example", "K7Q4-M2XP", "ventalibra",
                           abrir=lambda p: Respuesta(json.dumps({"formato": "otro"}).encode()))


def test_el_pedido_va_por_post_a_la_ruta_de_canje_con_el_producto():
    vistos = []

    def abrir(pedido):
        vistos.append(pedido)
        raise urllib.error.URLError("corte")

    with pytest.raises(ins.ErrorDeInstalacion):
        ins.canjear_codigo("sistema.ventalibra.example", "k7q4m2xp", "ventalibra", abrir=abrir)
    [pedido] = vistos
    assert pedido.full_url == "https://sistema.ventalibra.example/sync/v1/vinculacion" and pedido.method == "POST"
    assert json.loads(pedido.data) == {"codigo": "K7Q4-M2XP", "producto": "ventalibra"}


def test_el_contexto_tls_verifica_siempre():
    import ssl

    contexto = ins.contexto_tls()
    assert contexto.verify_mode == ssl.CERT_REQUIRED and contexto.check_hostname


# ── el central de prueba del CI ─────────────────────────────────────────────

def test_el_central_de_prueba_del_ci_emite_canjea_y_verifica(tmp_path):
    """`tests/central_de_prueba.py` es el que levanta `publicar-instalador.yml`: si se rompe, se rompe acá primero."""
    from fastapi.testclient import TestClient

    from tests.central_de_prueba import armar_app

    app = armar_app(tmp_path, "ventalibra", "https://localhost:8443")
    cliente = TestClient(app, base_url="https://localhost:8443")
    emitido = cliente.post("/api/nodos-offline/vinculacion", json={"sucursal_id": "1"}).json()
    assert emitido["direccion"] == "localhost:8443"
    carga = cliente.post("/sync/v1/vinculacion", json={"codigo": emitido["codigo"], "producto": "ventalibra"}).json()
    leida = ins.carga_desde_dict(carga, "ventalibra")
    assert leida.entorno["LIBRAEDGE_CENTRAL_URL"] == "https://localhost:8443"
    secreto = leida.entorno["LIBRAEDGE_NODE_SECRET"]
    node_id = emitido["node_id"]
    assert cliente.post("/prueba/secreto", json={"node_id": node_id, "secreto": secreto}).json() == {"ok": True}
    assert cliente.post("/prueba/secreto", json={"node_id": node_id, "secreto": "otro" * 8}).json() == {"ok": False}
    # Armarlo de nuevo sobre la misma carpeta no pisa nada.
    armar_app(tmp_path, "ventalibra", "https://localhost:8443")
