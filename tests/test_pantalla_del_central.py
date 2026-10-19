"""«Sucursal sin internet»: el router del central que da de alta el nodo y entrega la carga (ADR-011).

Con el producto de juguete de `test_aprovisionamiento.py`, contra los dos motores. Lo que se cuida, en orden:

1. **El secreto sólo viaja en la respuesta que es la carga**, como adjunto y sin caché; ni el `GET`, ni un error,
   ni una cabecera lo traen, y la base guarda sólo el hash.
2. La carga que sale es la que el instalador acepta (`carga_desde_dict`, las mismas reglas con que la va a leer).
3. Las guardas del CLI siguen siendo las guardas: segundo nodo, adicional `offline`, sucursal inexistente.
4. Nada se emite si la URL del central o el emisor no sirven (se valida antes del alta).
"""

import contextlib
import json

import pytest

from libraedge import aprovisionamiento as ap
from libraedge.db.changelog import tablas_publicadas
from libraedge.db.repository import NodeRepository
from libraedge.instalador import carga_desde_dict
from libraedge.pantalla_del_central import (
    candidatas_a_url_del_central,
    create_nodos_router,
    nuevo_node_id,
    resolver_url_del_central,
    url_del_instalador,
)
from tests.test_aprovisionamiento import _crear_producto, _sucursales

CENTRAL = "https://demo.juguete.com.ar"
#: CUIT ficticio con dígito verificador válido.
CUIT_FICTICIO = "20-12345678-6"


@pytest.fixture
def producto(conn):
    return _crear_producto(conn, sucursales=(("1", "Centro", 1), ("2", "Norte", 1), ("3", "Vieja", 0)))


@pytest.fixture
def config(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({
        "empresa_nombre": "Comercio de Prueba SA", "empresa_cuit": CUIT_FICTICIO,
        "empresa_iva_condition": "RI", "mp_access_token": "no-tiene-que-viajar",
    }), encoding="utf-8")
    return ruta


def _cliente(conn, config, *, verificar_alta=ap.sin_guarda, entorno=None, **opciones):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    aprov = ap.Aprovisionamiento(prefijo="juguete", tablas_de_referencia=["categorias", "productos"],
                                 listar_sucursales=_sucursales, verificar_alta=verificar_alta)
    app = FastAPI()
    app.include_router(create_nodos_router(
        aprov, lambda: contextlib.nullcontext(conn), ruta_config=lambda: str(config),
        entorno={"LIBRAEDGE_CENTRAL_URL": CENTRAL} if entorno is None else entorno, **opciones))
    return TestClient(app)


def _secreto_en_la_base(conn, node_id):
    return conn.execute("SELECT secret_hash FROM node_identity WHERE node_id = ?", (node_id,)).fetchone()[0]


# ── el estado ───────────────────────────────────────────────────────────────

def test_el_estado_lista_sucursales_nodos_e_instalador(producto, config):
    NodeRepository(producto).register_node("nodo-viejo", branch_id="2", prefijo="S02")
    cliente = _cliente(producto, config)
    r = cliente.get("/api/nodos-offline")
    assert r.status_code == 200
    datos = r.json()
    assert datos["producto"] == "juguete"
    assert datos["instalador_url"] == (
        "https://github.com/marianocappucci/libraedge/releases/latest/download/libraedge-nodo-juguete.exe")
    assert datos["central_url"] == CENTRAL and datos["central_url_error"] is None
    assert datos["contratado"] is False  # la tabla `modulos` del juguete está vacía
    assert datos["emisor_faltan"] == [] and datos["emisor_error"] is None
    assert [(s["id"], s["nombre"], s["activa"], s["nodo"]) for s in datos["sucursales"]] == [
        ("1", "Centro", True, None), ("2", "Norte", True, "nodo-viejo"), ("3", "Vieja", False, None)]
    [nodo] = datos["nodos"]
    assert nodo["node_id"] == "nodo-viejo" and nodo["sucursal"] == "Norte" and nodo["prefijo"] == "S02"
    assert nodo["estado"] == "nunca" and nodo["ultima_sincronizacion"] is None and nodo["registrado"]
    # 🔴 Ni el secreto ni su hash.
    assert "secret" not in r.text and _secreto_en_la_base(producto, "nodo-viejo") not in r.text


def test_el_estado_de_cada_nodo(producto, config):
    repo = NodeRepository(producto)
    repo.register_node("a", branch_id="1", prefijo="S01")
    repo.register_node("b", branch_id="2", prefijo="S02")
    repo.registrar_contacto("a")
    producto.execute("UPDATE node_identity SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE node_id = 'b'")
    producto.commit()
    repo.register_node("c", branch_id="1", prefijo="S01B")
    repo.deactivate_node("c")
    estados = {n["node_id"]: n["estado"] for n in _cliente(producto, config).get("/api/nodos-offline").json()["nodos"]}
    assert estados == {"a": "al_dia", "b": "sin_noticias", "c": "baja"}


def test_el_adicional_contratado_se_informa(producto, config):
    producto.execute("INSERT INTO modulos (modulo, habilitado) VALUES ('offline', 1)")
    producto.commit()
    assert _cliente(producto, config).get("/api/nodos-offline").json()["contratado"] is True


def test_sin_config_el_estado_dice_que_falta_el_emisor(producto, tmp_path):
    datos = _cliente(producto, tmp_path / "no-existe.json").get("/api/nodos-offline").json()
    assert datos["emisor_faltan"] == ["empresa_cuit", "empresa_iva_condition"]


# ── el alta y la carga ──────────────────────────────────────────────────────

def test_el_alta_devuelve_la_carga_como_adjunto_sin_cache(producto, config, motor):
    cliente = _cliente(producto, config)
    r = cliente.post("/api/nodos-offline", json={"sucursal_id": "2"})
    assert r.status_code == 200, r.text
    node_id = r.headers["x-libraedge-node-id"]
    assert node_id.startswith("nodo-2-")
    assert r.headers["x-libraedge-prefijo"] == "S02"
    assert r.headers["content-disposition"] == f'attachment; filename="carga-nodo-{node_id}.json"'
    assert r.headers["cache-control"] == "no-store"

    carga = r.json()
    leida = carga_desde_dict(carga, "juguete")  # la acepta el instalador, con sus mismas reglas
    assert leida.entorno["LIBRAEDGE_CENTRAL_URL"] == CENTRAL
    assert leida.entorno["LIBRAEDGE_NODE_ID"] == node_id
    assert leida.sucursal == {"id": "2", "nombre": "Norte"}
    assert leida.emisor["empresa_cuit"] == CUIT_FICTICIO
    assert "mp_access_token" not in carga["emisor"]
    # El secreto de la carga es el que verifica el central, y en la base queda sólo su hash.
    secreto = carga["entorno"]["LIBRAEDGE_NODE_SECRET"]
    assert NodeRepository(producto).verify_node_secret(node_id, secreto)
    assert _secreto_en_la_base(producto, node_id) != secreto
    # Ninguna cabecera lo lleva, y el GET de después tampoco.
    assert all(secreto not in valor for valor in r.headers.values())
    assert secreto not in cliente.get("/api/nodos-offline").text
    if motor == "postgres":  # el alta publica: sin triggers el nodo arrancaría con el espejo vacío
        assert set(tablas_publicadas(producto)) == {"categorias", "productos"}


def test_un_segundo_nodo_en_la_sucursal_es_409_y_no_emite_nada(producto, config):
    cliente = _cliente(producto, config)
    assert cliente.post("/api/nodos-offline", json={"sucursal_id": "1"}).status_code == 200
    r = cliente.post("/api/nodos-offline", json={"sucursal_id": "Centro"})
    assert r.status_code == 409
    assert r.json()["detail"]["codigo"] == "sucursal_con_nodo"
    assert "LIBRAEDGE_NODE_SECRET" not in r.text
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 1


@pytest.mark.parametrize("sucursal", ["3", "99", ""])
def test_una_sucursal_inactiva_o_inexistente_es_422(producto, config, sucursal):
    r = _cliente(producto, config).post("/api/nodos-offline", json={"sucursal_id": sucursal})
    assert r.status_code == 422
    assert r.json()["detail"]["codigo"] == "sucursal_invalida"
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_sin_el_adicional_el_alta_es_403_con_el_mensaje_de_la_guarda(producto, config):
    def guarda(conn, node_id, branch_id):  # como `guarda_offline_de_libracore` sin el adicional contratado
        raise ap.AltaRechazada("El plan no incluye «Modo offline».")

    r = _cliente(producto, config, verificar_alta=guarda).post("/api/nodos-offline", json={"sucursal_id": "1"})
    assert r.status_code == 403
    assert r.json()["detail"] == {"codigo": "adicional_offline", "mensaje": "El plan no incluye «Modo offline»."}
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_sin_url_https_del_central_no_se_emite_nada(producto, config):
    cliente = _cliente(producto, config, entorno={})
    r = cliente.post("/api/nodos-offline", json={"sucursal_id": "1"}, headers={"origin": "http://intruso.example"})
    assert r.status_code == 422
    assert r.json()["detail"]["codigo"] == "sin_url_del_central"
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0
    assert cliente.get("/api/nodos-offline").json()["central_url_error"]


def test_un_cuit_invalido_en_el_config_no_emite_nada(producto, tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"empresa_cuit": "20-12345678-0", "empresa_iva_condition": "RI"}), encoding="utf-8")
    r = _cliente(producto, ruta).post("/api/nodos-offline", json={"sucursal_id": "1"})
    assert r.status_code == 422 and r.json()["detail"]["codigo"] == "emisor_invalido"
    assert producto.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


# ── carga nueva y baja ──────────────────────────────────────────────────────

def test_una_carga_nueva_invalida_la_anterior_y_conserva_el_prefijo(producto, config):
    cliente = _cliente(producto, config)
    primera = cliente.post("/api/nodos-offline", json={"sucursal_id": "1"})
    node_id = primera.headers["x-libraedge-node-id"]
    viejo = primera.json()["entorno"]["LIBRAEDGE_NODE_SECRET"]

    r = cliente.post(f"/api/nodos-offline/{node_id}/carga")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    nuevo = r.json()["entorno"]["LIBRAEDGE_NODE_SECRET"]
    assert nuevo != viejo
    assert r.json()["entorno"]["LIBRAEDGE_PREFIJO"] == primera.json()["entorno"]["LIBRAEDGE_PREFIJO"]
    repo = NodeRepository(producto)
    assert repo.verify_node_secret(node_id, nuevo) and not repo.verify_node_secret(node_id, viejo)


def test_carga_nueva_de_un_nodo_inexistente_o_dado_de_baja(producto, config):
    cliente = _cliente(producto, config)
    assert cliente.post("/api/nodos-offline/no-existe/carga").status_code == 404
    NodeRepository(producto).register_node("nodo-baja", branch_id="1", prefijo="S01")
    NodeRepository(producto).deactivate_node("nodo-baja")
    r = cliente.post("/api/nodos-offline/nodo-baja/carga")
    assert r.status_code == 409 and r.json()["detail"]["codigo"] == "nodo_dado_de_baja"


def test_la_baja_revoca_el_secreto_y_libera_la_sucursal(producto, config):
    cliente = _cliente(producto, config)
    alta = cliente.post("/api/nodos-offline", json={"sucursal_id": "1"})
    node_id = alta.headers["x-libraedge-node-id"]
    secreto = alta.json()["entorno"]["LIBRAEDGE_NODE_SECRET"]

    r = cliente.delete(f"/api/nodos-offline/{node_id}")
    assert r.status_code == 200 and r.json() == {"node_id": node_id, "ya_estaba": False}
    assert not NodeRepository(producto).verify_node_secret(node_id, secreto)
    assert cliente.delete(f"/api/nodos-offline/{node_id}").json()["ya_estaba"] is True
    assert cliente.delete("/api/nodos-offline/no-existe").status_code == 404

    # La sucursal queda libre para otra PC, que es otro nodo con otro prefijo (nunca se reusa).
    otra = cliente.post("/api/nodos-offline", json={"sucursal_id": "1"})
    assert otra.status_code == 200
    assert otra.headers["x-libraedge-node-id"] != node_id
    assert otra.headers["x-libraedge-prefijo"] == "S01B"


# ── piezas ──────────────────────────────────────────────────────────────────

def test_url_del_instalador():
    assert url_del_instalador("VentaLibra").endswith("/libraedge-nodo-ventalibra.exe")
    with pytest.raises(ValueError):
        url_del_instalador("../otra")


def test_la_url_del_central_sale_en_orden_y_sólo_https():
    cabeceras = {"origin": "https://desde-el-navegador.example", "host": "interno:8000"}
    assert resolver_url_del_central("ventalibra", cabeceras, {"LIBRAEDGE_CENTRAL_URL": "https://fija.example/"}) \
        == "https://fija.example"
    entorno = {"VENTALIBRA_RESET_URL_BASE": "https://cliente.ventalibra.com.ar/reset-password"}
    assert resolver_url_del_central("ventalibra", cabeceras, entorno) == "https://cliente.ventalibra.com.ar"
    assert resolver_url_del_central("ventalibra", cabeceras, {}) == "https://desde-el-navegador.example"
    detras_del_proxy = {"host": "cliente.ventalibra.com.ar", "x-forwarded-proto": "https"}
    assert resolver_url_del_central("ventalibra", detras_del_proxy, {}) == "https://cliente.ventalibra.com.ar"
    # Un túnel a localhost por http no sirve para un nodo que está en otra PC.
    from libraedge.instalador import ErrorDeInstalacion

    with pytest.raises(ErrorDeInstalacion, match="LIBRAEDGE_CENTRAL_URL"):
        resolver_url_del_central("ventalibra", {"origin": "http://localhost:8701", "host": "localhost:8701"}, {})
    assert [o for o, _ in candidatas_a_url_del_central("x", None, {})] == []


def test_nuevo_node_id_es_unico_y_valido():
    ids = {nuevo_node_id("2") for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("nodo-2-") and " " not in i for i in ids)
    assert nuevo_node_id("Sucursal Norte!").startswith("nodo-sucursalnorte-")


def test_sin_modulos_no_esta_contratado(conn):
    from libraedge.pantalla_del_central import adicional_offline_contratado

    assert adicional_offline_contratado(conn) is False
    # La conexión sigue sirviendo después del error (en PostgreSQL la transacción abortada se descarta).
    assert conn.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0


def test_el_modulo_no_importa_libracore():
    import libraedge.pantalla_del_central as modulo

    fuente = open(modulo.__file__, encoding="utf-8").read()
    assert "import libracore" not in fuente and "from libracore" not in fuente
