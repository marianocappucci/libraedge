"""La carga inicial de existencias: el nodo trae las de SU sucursal del central, una vez (ADR-023).

Tres capas, cada una con su gate:

1. **La ruta del central** (`GET /sync/v1/carga-inicial`): 401 / 501 / 200, y que la sucursal sale del nodo autenticado.
2. **El worker del nodo**: corre una vez y marca, reintenta si la funcion espera, un 501 es «nada que traer», y el
   resto de los errores quedan en el estado sin romper el ciclo.
3. **El instalador**: la variable viaja en la definicion del producto y llega a los nodos ya instalados.

Con un producto de juguete (`tests/producto_de_prueba.py`): LibraEdge no sabe que es una existencia.
"""

import contextlib
import io
import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError

import pytest

from libraedge import instalador as ins
from libraedge.db.repository import NodeRepository
from libraedge.db.schema import init_schema
from libraedge.nodo import EstadoNodo, Nodo, escribir_estado, leer_estado
from libraedge.sync import carga_inicial as modulo
from libraedge.sync import esquema
from libraedge.sync.api import RUTAS_AUTENTICADAS_POR_NODO, create_sync_router
from libraedge.sync.carga_inicial import (
    CargaInicialNoDisponible,
    CargaInicialWorker,
    HttpCargaInicialTransport,
    marca_de,
    resolver_funcion,
)
from tests import producto_de_prueba as producto
from tests.test_nodo import PullQueAnda, PullSinInternet, TransporteQueAcepta, operacion

FUNCION = "tests.producto_de_prueba:aplicar"
CENTRAL = "https://central.ejemplo"


@pytest.fixture(autouse=True)
def _producto_limpio():
    producto.reiniciar()
    yield
    producto.reiniciar()


# ── La ruta del central ─────────────────────────────────────────────────────


@pytest.fixture
def central():
    conexion = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(conexion)
    yield conexion
    conexion.close()


def _montar(central, carga_inicial, **opciones):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(create_sync_router(
        lambda: contextlib.nullcontext(central), carga_inicial=carga_inicial, **opciones))
    repo = NodeRepository(central)
    secretos = {"node-1": repo.register_node("node-1", branch_id="b1"),
                "node-2": repo.register_node("node-2", branch_id="b2")}
    return TestClient(app), secretos


def _pedir(cliente, secreto, node_id="node-1", cabeceras=None):
    cabeceras = {"Authorization": f"Bearer {secreto}", **(cabeceras or {})} if secreto else (cabeceras or {})
    return cliente.get("/sync/v1/carga-inicial", params={"node_id": node_id}, headers=cabeceras)


def test_entrega_la_carga_al_nodo_y_anota_el_latido(central):
    pedidos = []

    def carga(conexion, node_id, branch_id):
        pedidos.append((conexion, node_id, branch_id))
        return {"existencias": [{"producto_id": 7, "cantidad": Decimal("12.500")}]}

    cliente, secretos = _montar(central, carga)
    assert central.execute("SELECT last_seen_at FROM node_identity WHERE node_id = 'node-1'").fetchone()[0] is None

    respuesta = _pedir(cliente, secretos["node-1"])

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "no-store"
    # 🔴 Los decimales viajan como TEXTO (como la bajada): como numero JSON se degradarian a float en el camino.
    assert respuesta.json() == {"existencias": [{"producto_id": 7, "cantidad": "12.500"}]}
    assert pedidos == [(central, "node-1", "b1")]
    assert central.execute("SELECT last_seen_at FROM node_identity WHERE node_id = 'node-1'").fetchone()[0]


def test_la_sucursal_sale_del_nodo_autenticado_y_no_de_lo_que_pida(central):
    pedidos = []
    cliente, secretos = _montar(central, lambda c, n, b: pedidos.append((n, b)) or {})

    assert _pedir(cliente, secretos["node-2"], "node-2").status_code == 200
    assert pedidos == [("node-2", "b2")]

    # Con el secreto de un nodo no se piden las existencias de otro: el gate es por node_id.
    pedidos.clear()
    assert _pedir(cliente, secretos["node-1"], "node-2").status_code == 401
    assert pedidos == []
    # Y el parametro `branch_id` (o cualquiera) que se agregue no cambia la sucursal.
    r = cliente.get("/sync/v1/carga-inicial", params={"node_id": "node-1", "branch_id": "b2"},
                    headers={"Authorization": f"Bearer {secretos['node-1']}"})
    assert r.status_code == 200 and pedidos == [("node-1", "b1")]


def test_sin_credenciales_o_con_un_nodo_de_baja_es_401(central):
    pedidos = []
    cliente, secretos = _montar(central, lambda c, n, b: pedidos.append(n) or {})

    assert _pedir(cliente, None).status_code == 401
    assert _pedir(cliente, "secreto-que-no-es").status_code == 401
    NodeRepository(central).deactivate_node("node-1")
    assert _pedir(cliente, secretos["node-1"]).status_code == 401
    assert pedidos == []


def test_sin_la_costura_es_501_pero_despues_de_autenticar(central):
    cliente, secretos = _montar(central, None)

    # A quien no es un nodo no se le cuenta que este central no tiene carga inicial.
    assert _pedir(cliente, "secreto-que-no-es").status_code == 401
    r = _pedir(cliente, secretos["node-1"])
    assert r.status_code == 501
    assert r.json()["detail"]["codigo"] == modulo.CODIGO_SIN_COSTURA


def test_el_esquema_viejo_es_409(central):
    cliente, secretos = _montar(central, lambda c, n, b: {}, version_esquema="v2")
    assert _pedir(cliente, secretos["node-1"]).status_code == 409
    assert _pedir(cliente, secretos["node-1"], cabeceras={esquema.CABECERA: "v2"}).status_code == 200


def test_una_costura_que_no_devuelve_un_dict_es_502(central):
    cliente, secretos = _montar(central, lambda c, n, b: [1, 2])
    assert _pedir(cliente, secretos["node-1"]).status_code == 502


def test_la_costura_es_sincronica(central):
    async def carga(conexion, node_id, branch_id):
        return {}

    with pytest.raises(ValueError, match="sincronica"):
        create_sync_router(lambda: contextlib.nullcontext(central), carga_inicial=carga)


def test_la_ruta_esta_en_la_documentacion_de_rutas_del_nodo(central):
    """Los productos suman una fila «PUBLICA» por cada una en su matriz de roles: tiene que ser la lista completa."""
    from fastapi import APIRouter

    router = create_sync_router(lambda: contextlib.nullcontext(central))
    assert isinstance(router, APIRouter)
    montadas = {(metodo, ruta.path) for ruta in router.routes for metodo in ruta.methods}
    assert set(RUTAS_AUTENTICADAS_POR_NODO) <= montadas
    assert ("GET", "/sync/v1/carga-inicial") in RUTAS_AUTENTICADAS_POR_NODO
    # Las que NO son del nodo: la salud usa el token de monitoreo.
    assert not [r for r in RUTAS_AUTENTICADAS_POR_NODO if "salud" in r[1]]


# ── El transporte del nodo ──────────────────────────────────────────────────


class _Respuesta:
    def __init__(self, cuerpo):
        self._cuerpo = cuerpo

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_contra(test_client, pedidos):
    """Un ``urlopen`` que le habla al TestClient, con la misma forma de errores que urllib."""

    def urlopen(pedido, timeout=None):
        pedidos.append(pedido)
        ruta = pedido.full_url.split("://", 1)[1].split("/", 1)[1]
        respuesta = test_client.get("/" + ruta, headers=dict(pedido.header_items()))
        if respuesta.status_code >= 400:
            raise HTTPError(pedido.full_url, respuesta.status_code, "error", {}, io.BytesIO(respuesta.content))
        return _Respuesta(respuesta.content)

    return urlopen


def _transporte(monkeypatch, test_client, secreto, node_id="node-1", **kwargs):
    pedidos = []
    monkeypatch.setattr(modulo, "urlopen", _urlopen_contra(test_client, pedidos))
    return HttpCargaInicialTransport(CENTRAL, node_id, secreto, **kwargs), pedidos


def test_traer_hace_el_get_autenticado_y_devuelve_el_dict(central, monkeypatch):
    cliente, secretos = _montar(
        central, lambda c, n, b: {"sucursal": b, "existencias": [{"cantidad": Decimal("1.250")}]})
    transporte, pedidos = _transporte(monkeypatch, cliente, secretos["node-1"])

    datos = transporte.traer()

    assert datos == {"sucursal": "b1", "existencias": [{"cantidad": "1.250"}]}
    [pedido] = pedidos
    assert pedido.full_url == f"{CENTRAL}/sync/v1/carga-inicial?node_id=node-1"
    assert pedido.get_header("Authorization") == f"Bearer {secretos['node-1']}"
    assert secretos["node-1"] not in repr(transporte)


def test_traer_conserva_la_precision_de_un_decimal_que_llega_como_numero(monkeypatch):
    class Central:
        def get(self, ruta, headers):
            class R:
                status_code = 200
                content = b'{"cantidad": 12.500}'
            return R()

    transporte, _ = _transporte(monkeypatch, Central(), "s")
    assert transporte.traer()["cantidad"] == Decimal("12.500")


def test_un_501_es_que_el_producto_no_tiene_carga_inicial(central, monkeypatch):
    cliente, secretos = _montar(central, None)
    transporte, _ = _transporte(monkeypatch, cliente, secretos["node-1"])
    with pytest.raises(CargaInicialNoDisponible):
        transporte.traer()


def test_un_404_no_es_un_501(monkeypatch):
    """Un central anterior a v0.20 no tiene la ruta: eso se arregla actualizandolo, no dandolo por hecho."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    transporte, _ = _transporte(monkeypatch, TestClient(FastAPI()), "s")
    with pytest.raises(HTTPError) as error:
        transporte.traer()
    assert error.value.code == 404


def test_el_esquema_viejo_es_esquema_incompatible(central, monkeypatch):
    cliente, secretos = _montar(central, lambda c, n, b: {}, version_esquema="v2")
    transporte, _ = _transporte(monkeypatch, cliente, secretos["node-1"], version_esquema="v1")
    with pytest.raises(esquema.EsquemaIncompatible):
        transporte.traer()


def test_una_respuesta_que_no_es_un_objeto_no_sirve(monkeypatch):
    class Central:
        def get(self, ruta, headers):
            class R:
                status_code = 200
                content = b"[1, 2]"
            return R()

    transporte, _ = _transporte(monkeypatch, Central(), "s")
    with pytest.raises(ValueError, match="objeto JSON"):
        transporte.traer()


# ── La funcion del producto ─────────────────────────────────────────────────


def test_resolver_la_funcion():
    assert resolver_funcion(FUNCION) is producto.aplicar


@pytest.mark.parametrize("texto, dice", [
    (None, "no se entiende"),
    ("", "no se entiende"),
    ("sin_dos_puntos", "no se entiende"),
    ("modulo:", "no se entiende"),
    ("a:b:c", "no se entiende"),
    ("libracommerce_que_todavia_no_la_tiene.offline_stock:aplicar_carga_inicial", "no se pudo importar el modulo"),
    ("tests.producto_de_prueba:no_existe", "no tiene 'no_existe'"),
    ("tests.producto_de_prueba:llamadas", "no es una funcion"),
])
def test_una_funcion_que_no_se_puede_resolver_dice_por_que(texto, dice):
    with pytest.raises(modulo.CargaInicialMalConfigurada) as error:
        resolver_funcion(texto)
    assert dice in str(error.value)
    if "importar" in dice or "tiene" in dice:
        assert "actualizar el nodo" in str(error.value)


# ── El worker ───────────────────────────────────────────────────────────────


class TransporteDeMentira:
    def __init__(self, datos=None, error=None):
        self.datos, self.error, self.pedidos = datos if datos is not None else {"existencias": []}, error, 0

    def traer(self):
        self.pedidos += 1
        if self.error:
            raise self.error
        return self.datos


@pytest.fixture
def existencias(conn):
    """La tabla donde la funcion de juguete escribe lo que trajo (autoridad del nodo)."""
    conn.execute("CREATE TABLE existencias (producto_id INTEGER PRIMARY KEY, cantidad TEXT NOT NULL)")
    conn.commit()
    return conn


def _filas(conn):
    conn.rollback()  # lo que no se commiteo no se ve desde otra transaccion
    return [tuple(f) for f in conn.execute("SELECT producto_id, cantidad FROM existencias ORDER BY 1").fetchall()]


def _trabajador(conn, transporte=None, especificacion=FUNCION, node_id="node-1"):
    return CargaInicialWorker(NodeRepository(conn), conn, node_id, transporte or TransporteDeMentira(), especificacion)


def _escribe(filas, hecha=True, motivo=None):
    """Una funcion de producto que trae, escribe SIN commitear y contesta como el producto."""

    def funcion(conexion, traer):
        traer()
        for producto_id, cantidad in filas:
            conexion.execute("INSERT INTO existencias (producto_id, cantidad) VALUES (?, ?)", (producto_id, cantidad))
        return {"hecha": hecha, "motivo": motivo, "filas": len(filas)}

    return funcion


def test_corre_una_vez_y_deja_la_marca(existencias):
    conn = existencias
    producto.reiniciar(_escribe([(1, "5")]))
    transporte = TransporteDeMentira()
    trabajador = _trabajador(conn, transporte)

    resultado = trabajador.run_once()

    assert resultado.estado == modulo.HECHA and resultado.terminada
    assert resultado.resultado["filas"] == 1
    assert resultado.para_el_estado() is None
    assert NodeRepository(conn).tiene_marca("carga_inicial:node-1")
    assert marca_de("node-1") == "carga_inicial:node-1"
    assert len(producto.llamadas) == 1 and transporte.pedidos == 1
    # La funcion recibio LA conexion del nodo y el `traer` del transporte.
    assert producto.llamadas[0][0] is conn and producto.llamadas[0][1] == transporte.traer

    segunda = trabajador.run_once()
    assert segunda.estado == modulo.YA_HECHA and segunda.terminada
    assert len(producto.llamadas) == 1, "con la marca no se vuelve a llamar: pisaria el stock que el nodo ya movio"
    assert _filas(conn) == [(1, "5")]


def test_lo_que_escribio_la_funcion_y_la_marca_entran_juntos(existencias):
    """Sin commit propio de la funcion: el worker commitea las dos cosas en una transaccion."""
    conn = existencias
    producto.reiniciar(_escribe([(1, "5"), (2, "7")]))

    assert _trabajador(conn).run_once().estado == modulo.HECHA

    conn.rollback()  # si no hubiera commit, esto se llevaria las filas y la marca
    assert _filas(conn) == [(1, "5"), (2, "7")]
    assert NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_si_la_funcion_espera_no_marca_y_reintenta_el_ciclo_siguiente(existencias):
    conn = existencias
    producto.reiniciar(_escribe([(1, "5")], hecha=False, motivo="el espejo todavia no trajo los depositos"))
    trabajador = _trabajador(conn)

    primera = trabajador.run_once()

    assert primera.estado == modulo.ESPERA and not primera.terminada
    assert primera.para_el_estado() == "esperando: el espejo todavia no trajo los depositos"
    assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")
    assert _filas(conn) == [], "lo que escribio antes de decir que esperaba se deshace"

    producto.reiniciar(_escribe([(1, "5")]))
    segunda = trabajador.run_once()

    assert segunda.estado == modulo.HECHA
    assert _filas(conn) == [(1, "5")]
    assert NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_esperar_sin_motivo_igual_espera(existencias):
    producto.reiniciar({"hecha": False})
    resultado = _trabajador(existencias).run_once()
    assert resultado.estado == modulo.ESPERA and resultado.para_el_estado().startswith("esperando: ")


def test_una_funcion_que_no_devuelve_nada_se_da_por_hecha(existencias):
    producto.reiniciar(lambda conexion, traer: None)
    assert _trabajador(existencias).run_once().estado == modulo.HECHA


def test_un_error_de_la_funcion_queda_en_el_estado_y_se_reintenta(existencias):
    conn = existencias

    def revienta(conexion, traer):
        conexion.execute("INSERT INTO existencias (producto_id, cantidad) VALUES (1, '5')")
        raise ValueError("el deposito 3 no tiene sucursal")

    producto.reiniciar(revienta)
    trabajador = _trabajador(conn)

    resultado = trabajador.run_once()

    assert resultado.estado == modulo.ERROR and not resultado.terminada
    assert resultado.para_el_estado() == "error: ValueError: el deposito 3 no tiene sucursal"
    assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")
    assert _filas(conn) == [], "y la conexion quedo utilizable, sin lo que escribio a medias"

    producto.reiniciar(_escribe([(1, "5")]))
    assert trabajador.run_once().estado == modulo.HECHA


def test_el_modulo_que_todavia_no_existe_es_un_error_claro_y_no_marca(conn):
    """Un nodo cuyo libracommerce es anterior a la carga inicial: no se rompe, lo dice y reintenta."""
    trabajador = _trabajador(conn, especificacion="libracommerce_viejo.erp.offline_stock:aplicar_carga_inicial")

    resultado = trabajador.run_once()

    assert resultado.estado == modulo.ERROR
    assert "no se pudo importar el modulo" in resultado.para_el_estado()
    assert "actualizar el nodo" in resultado.para_el_estado()
    assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_un_501_del_central_se_da_por_hecho_y_se_loguea(existencias, caplog):
    conn = existencias
    producto.reiniciar(_escribe([(1, "5")]))
    transporte = TransporteDeMentira(error=CargaInicialNoDisponible("el central no tiene carga inicial"))
    trabajador = _trabajador(conn, transporte)

    with caplog.at_level(logging.WARNING, logger=modulo.logger.name):
        resultado = trabajador.run_once()

    assert resultado.estado == modulo.NO_DISPONIBLE and resultado.terminada
    assert resultado.para_el_estado() is None
    assert NodeRepository(conn).tiene_marca("carga_inicial:node-1")
    assert "node-1" in caplog.text and "no hay nada que traer" in caplog.text
    assert trabajador.run_once().estado == modulo.YA_HECHA


def test_el_resto_de_los_errores_del_central_se_reintentan(conn):
    producto.reiniciar(lambda conexion, traer: traer())
    for error in (HTTPError(CENTRAL, 401, "no", {}, io.BytesIO(b"")), OSError("Network is unreachable"),
                  esquema.EsquemaIncompatible("v2", "v1")):
        trabajador = _trabajador(conn, TransporteDeMentira(error=error))

        resultado = trabajador.run_once()

        assert resultado.estado == modulo.ERROR, error
        assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_con_la_bajada_incompleta_ni_se_llama_a_la_funcion(conn):
    resultado = _trabajador(conn).run_once(bajada_completa=False)
    assert resultado.estado == modulo.ESPERA and "bajada" in resultado.detalle
    assert producto.llamadas == []


def test_forzar_borra_la_marca_y_corre_ya(existencias):
    conn = existencias
    trabajador = _trabajador(conn)
    assert trabajador.run_once().estado == modulo.HECHA
    assert trabajador.run_once().estado == modulo.YA_HECHA

    assert trabajador.run_once(forzar=True).estado == modulo.HECHA

    assert len(producto.llamadas) == 2
    assert NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_forzar_con_una_funcion_que_espera_deja_el_nodo_sin_marca(conn):
    trabajador = _trabajador(conn)
    trabajador.run_once()
    producto.reiniciar({"hecha": False, "motivo": "x"})

    assert trabajador.run_once(forzar=True).estado == modulo.ESPERA
    assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")


def test_la_marca_es_por_nodo(conn):
    assert _trabajador(conn, node_id="node-1").run_once().estado == modulo.HECHA
    assert _trabajador(conn, node_id="node-2").run_once().estado == modulo.HECHA
    assert len(producto.llamadas) == 2


def test_las_marcas_del_repositorio(repo, conn):
    assert not repo.tiene_marca("x")
    repo.poner_marca("x")
    repo.poner_marca("x")  # idempotente
    assert repo.tiene_marca("x")
    repo.quitar_marca("x")
    assert not repo.tiene_marca("x")
    # No pisa las secuencias de verdad (`operations`, el mapeo): son otro nombre en la misma tabla.
    assert repo.next_sequence() == 1 and not repo.tiene_marca("carga_inicial:node-1")
    # `commit=False` queda dentro de la transaccion del llamador.
    repo.poner_marca("y", commit=False)
    conn.rollback()
    assert not repo.tiene_marca("y")


def test_de_punta_a_punta_con_el_central_de_verdad(central, conn, monkeypatch):
    """El nodo trae SU sucursal por la ruta real, la funcion del producto la escribe y queda marcada."""
    conn.execute("CREATE TABLE existencias (producto_id INTEGER PRIMARY KEY, cantidad TEXT NOT NULL)")
    conn.commit()

    def del_central(conexion, node_id, branch_id):
        por_sucursal = {"b1": [(1, Decimal("5.5"))], "b2": [(1, Decimal("99"))]}
        return {"existencias": [{"producto_id": p, "cantidad": c} for p, c in por_sucursal[branch_id]]}

    def aplicar(conexion, traer):
        for fila in traer()["existencias"]:
            conexion.execute("INSERT INTO existencias (producto_id, cantidad) VALUES (?, ?)",
                             (fila["producto_id"], fila["cantidad"]))
        return {"hecha": True}

    cliente, secretos = _montar(central, del_central)
    transporte, _ = _transporte(monkeypatch, cliente, secretos["node-1"])
    producto.reiniciar(aplicar)

    trabajador = CargaInicialWorker(NodeRepository(conn), conn, "node-1", transporte, FUNCION)
    assert trabajador.run_once().estado == modulo.HECHA
    assert _filas(conn) == [(1, "5.5")], "las existencias de b1, no las de b2"


def test_un_central_viejo_sin_la_ruta_no_marca(conn, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    transporte, _ = _transporte(monkeypatch, TestClient(FastAPI()), "s")
    producto.reiniciar(lambda conexion, traer: traer())

    resultado = CargaInicialWorker(NodeRepository(conn), conn, "node-1", transporte, FUNCION).run_once()

    assert resultado.estado == modulo.ERROR and "404" in resultado.detalle
    assert not NodeRepository(conn).tiene_marca("carga_inicial:node-1")


# ── Dentro del ciclo del nodo ───────────────────────────────────────────────


class PullQueRegistra(PullQueAnda):
    def __init__(self, eventos, cambios=0):
        super().__init__(cambios)
        self.eventos = eventos

    def run_once(self, node_id, limit=500):
        self.eventos.append("pull")
        return super().run_once(node_id, limit)


def _nodo(conn, pull, *, outbox=None, **kwargs):
    repo = NodeRepository(conn)
    return Nodo(repo, "node-1", outbox_worker=outbox, pull_worker=pull,
                carga_inicial_worker=_trabajador(conn, **kwargs))


def test_el_ciclo_corre_la_carga_despues_del_pull_y_una_sola_vez(conn):
    eventos = []
    producto.reiniciar(lambda conexion, traer: eventos.append("carga") or {"hecha": True})
    nodo = _nodo(conn, PullQueRegistra(eventos))

    primero = nodo.sincronizar()
    segundo = nodo.sincronizar()

    assert eventos == ["pull", "carga", "pull"]
    assert primero.carga_inicial is None and segundo.carga_inicial is None
    assert primero.en_linea and segundo.en_linea


def test_si_la_carga_espera_el_estado_lo_dice_y_el_nodo_sigue_en_linea(conn):
    producto.reiniciar({"hecha": False, "motivo": "faltan los depositos"})
    estado = _nodo(conn, PullQueAnda()).sincronizar()

    assert estado.carga_inicial == "esperando: faltan los depositos"
    assert estado.en_linea and estado.ultimo_error is None

    producto.reiniciar({"hecha": True})
    assert _nodo(conn, PullQueAnda()).sincronizar().carga_inicial is None


def test_un_error_de_la_carga_no_rompe_el_ciclo_ni_frena_la_subida_de_ventas(conn):
    producto.reiniciar(RuntimeError("se cayo la carga"))
    repo = NodeRepository(conn)
    repo.enqueue_operation(operacion())
    from libraedge.sync.worker import OutboxWorker

    nodo = _nodo(conn, PullQueAnda(), outbox=OutboxWorker(repo, TransporteQueAcepta()))

    estado = nodo.sincronizar()

    assert estado.operaciones_subidas == 1 and estado.pendientes == 0
    assert estado.en_linea is True and estado.ultimo_error is None
    assert estado.carga_inicial == "error: RuntimeError: se cayo la carga"


def test_sin_central_no_se_intenta_la_carga(conn):
    nodo = _nodo(conn, PullSinInternet())

    estado = nodo.sincronizar()

    assert estado.en_linea is False and estado.carga_inicial is None
    assert producto.llamadas == []


def test_con_la_bajada_llena_espera_a_que_termine(conn):
    nodo = _nodo(conn, PullQueAnda(cambios=3))

    estado = nodo.sincronizar(limite=3)

    assert estado.carga_inicial == "esperando: la bajada del espejo todavia no termino"
    assert producto.llamadas == []
    assert nodo.sincronizar(limite=4).carga_inicial is None and len(producto.llamadas) == 1


def test_una_bajada_llena_despues_de_la_carga_no_dice_nada(conn):
    nodo = _nodo(conn, PullQueAnda(cambios=3))
    assert nodo.sincronizar(limite=4).carga_inicial is None
    assert nodo.sincronizar(limite=3).carga_inicial is None


def test_un_nodo_sin_carga_inicial_es_el_de_siempre(conn):
    estado = Nodo(NodeRepository(conn), "node-1", pull_worker=PullQueAnda()).sincronizar()
    assert estado.carga_inicial is None


def test_el_estado_lleva_la_carga_inicial_y_lee_los_de_antes(tmp_path):
    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo("node-1", True, 0, 0, carga_inicial="error: x"))
    assert leer_estado(ruta).carga_inicial == "error: x"
    assert json.loads(Path(ruta).read_text(encoding="utf-8"))["carga_inicial"] == "error: x"

    # El estado que dejo un nodo de antes de v0.20 no tiene la clave: se lee igual.
    viejo = json.loads(Path(ruta).read_text(encoding="utf-8"))
    del viejo["carga_inicial"]
    Path(ruta).write_text(json.dumps(viejo), encoding="utf-8")
    assert leer_estado(ruta).carga_inicial is None


# ── El CLI de soporte ───────────────────────────────────────────────────────


@pytest.fixture
def nodo_en_postgres(monkeypatch, tmp_path):
    from libracore.db import core

    from tests.conftest import url_postgres
    from tests.test_nodo import _entorno_de_nodo

    url = url_postgres()
    core.configure(url)
    conexion = core.get_connection()
    try:
        conexion.execute("DROP SCHEMA public CASCADE")
        conexion.execute("CREATE SCHEMA public")
        conexion.commit()
        init_schema(conexion)
        NodeRepository(conexion).register_node("node-1", branch_id="b1")
        conexion.commit()
    finally:
        conexion.close()
        core._db_path = None
        core._database_url = None
    _entorno_de_nodo(monkeypatch, url, str(tmp_path / "estado.json"))
    monkeypatch.delenv("LIBRAEDGE_CARGA_INICIAL", raising=False)
    yield url
    core._db_path = None
    core._database_url = None


def test_el_cli_sin_la_variable_dice_que_el_producto_no_tiene_carga(nodo_en_postgres, capsys):
    from libraedge.cli import main

    assert main(["carga-inicial"]) == 1
    assert "LIBRAEDGE_CARGA_INICIAL" in capsys.readouterr().err
    assert producto.llamadas == []


def test_el_cli_hace_la_carga_una_vez_y_forzar_la_repite(nodo_en_postgres, monkeypatch, capsys):
    from libraedge.cli import main

    monkeypatch.setenv("LIBRAEDGE_CARGA_INICIAL", FUNCION)

    assert main(["carga-inicial"]) == 0
    assert json.loads(capsys.readouterr().out)["estado"] == modulo.HECHA
    assert len(producto.llamadas) == 1

    assert main(["carga-inicial"]) == 0
    capturado = capsys.readouterr()
    assert json.loads(capturado.out)["estado"] == modulo.YA_HECHA
    assert "--forzar" in capturado.err
    assert len(producto.llamadas) == 1

    assert main(["carga-inicial", "--forzar"]) == 0
    assert json.loads(capsys.readouterr().out)["estado"] == modulo.HECHA
    assert len(producto.llamadas) == 2


def test_el_cli_sale_1_si_la_funcion_espera_o_falla(nodo_en_postgres, monkeypatch, capsys):
    from libraedge.cli import main

    monkeypatch.setenv("LIBRAEDGE_CARGA_INICIAL", FUNCION)
    producto.reiniciar({"hecha": False, "motivo": "faltan depositos"})
    assert main(["carga-inicial"]) == 1
    assert json.loads(capsys.readouterr().out)["detalle"] == "faltan depositos"

    producto.reiniciar(ValueError("mal"))
    assert main(["carga-inicial"]) == 1
    assert json.loads(capsys.readouterr().out)["estado"] == modulo.ERROR


def test_el_cli_con_un_producto_sin_el_modulo_no_se_rompe(nodo_en_postgres, monkeypatch, capsys):
    from libraedge.cli import main

    monkeypatch.setenv("LIBRAEDGE_CARGA_INICIAL", "libracommerce_viejo.erp.offline_stock:aplicar_carga_inicial")
    assert main(["carga-inicial"]) == 1
    assert "no se pudo importar el modulo" in json.loads(capsys.readouterr().out)["detalle"]


def test_el_servicio_con_la_variable_mal_puesta_arranca_igual(nodo_en_postgres, monkeypatch):
    """`construir_nodo` (el arranque del servicio) no se cae por `LIBRAEDGE_CARGA_INICIAL`: lo dice en el estado."""
    from libraedge.cli import construir_nodo

    monkeypatch.setenv("LIBRAEDGE_CARGA_INICIAL", "esto no es una funcion")
    nodo, conexion = construir_nodo()
    try:
        resultado = nodo.carga_inicial_worker.run_once()
        assert resultado.estado == modulo.ERROR and "no se entiende" in resultado.detalle
    finally:
        conexion.close()


# ── El instalador ───────────────────────────────────────────────────────────

INSTALADOR = Path(__file__).resolve().parent.parent / "instalador"
TODOS = sorted(p.stem for p in (INSTALADOR / "productos").glob("*.json"))
#: Los de LibraCommerce con existencias por sucursal: traen la carga inicial de stock al vincular (ADR-023). LibraClub
#: la suma por el stock del buffet (libraclub ADR-023, 2026-10-24).
PRODUCTOS = ["contalibra", "libraclub", "restolibra", "ventalibra"]
#: LibraCargo trae su carga propia (órdenes, caja, terceros; su ADR-056), no la de existencias de LibraCommerce.
CARGA_PROPIA = {"libracargo": "app.servicios.carga_inicial:aplicar_carga_inicial"}
SIN_EXISTENCIAS = list(CARGA_PROPIA)
VALOR = "libracommerce.erp.offline_stock:aplicar_carga_inicial"


def _definicion(producto_):
    return ins.leer_definicion(str(INSTALADOR / "productos" / f"{producto_}.json"))


@pytest.mark.parametrize("producto_", PRODUCTOS)
def test_cada_producto_lleva_la_variable_en_su_definicion(producto_):
    definicion = _definicion(producto_)
    assert definicion.variables == {"LIBRAEDGE_CARGA_INICIAL": VALOR}
    # El valor es una funcion que resuelve el nodo: tiene que tener la forma modulo:funcion.
    assert ins._ASGI.match(VALOR)


def test_los_productos_con_y_sin_existencias_son_todos_los_del_instalador():
    assert sorted(PRODUCTOS + SIN_EXISTENCIAS) == TODOS


@pytest.mark.parametrize("producto_", SIN_EXISTENCIAS)
def test_los_productos_sin_existencias_declaran_su_carga_propia_y_no_la_de_stock(producto_):
    """La de `libracommerce.erp.offline_stock` traería existencias a un nodo sin `stock_movements` que cargar."""
    from tests.test_instalador import _carga

    definicion = _definicion(producto_)
    assert definicion.variables == {"LIBRAEDGE_CARGA_INICIAL": CARGA_PROPIA[producto_]}
    assert ins._ASGI.match(CARGA_PROPIA[producto_])
    carga = ins.carga_desde_dict(_carga(producto_), producto_)
    entorno = ins.armar_entorno(definicion, carga, clave_pg="x", estado="e", data_dir="d")
    assert entorno["LIBRAEDGE_CARGA_INICIAL"] == CARGA_PROPIA[producto_] != VALOR


@pytest.mark.parametrize("producto_", PRODUCTOS)
def test_con_la_carga_el_nodo_env_la_trae(producto_):
    from tests.test_instalador import _carga

    definicion = _definicion(producto_)
    carga = ins.carga_desde_dict(_carga(producto_), producto_)
    entorno = ins.armar_entorno(definicion, carga, clave_pg="x", estado="e", data_dir="d")
    assert entorno["LIBRAEDGE_CARGA_INICIAL"] == VALOR
    assert ins.leer_entorno(ins.formatear_entorno(entorno))["LIBRAEDGE_CARGA_INICIAL"] == VALOR
    # Opcional: un nodo sin ella no se declara incompleto (el producto instalado puede no traerla todavia).
    assert "LIBRAEDGE_CARGA_INICIAL" not in ins.revisar_entorno(definicion, {})


def test_una_definicion_con_la_variable_mal_formada_se_rechaza():
    from tests.test_instalador import _definicion as definicion_cruda

    with pytest.raises(ins.ErrorDeInstalacion, match="modulo:funcion"):
        ins.definicion_desde_dict(definicion_cruda(variables={"LIBRAEDGE_CARGA_INICIAL": "no es una funcion"}))
    ins.definicion_desde_dict(definicion_cruda(variables={"LIBRAEDGE_CARGA_INICIAL": "a.b:c"}))


def test_un_nodo_ya_instalado_la_recibe_al_actualizar_sin_vincular():
    """La actualizacion sin carga (apt, o /SINVINCULAR sobre un nodo vinculado) no rearma el nodo.env."""
    definicion = _definicion("ventalibra")
    viejo = "# comentario\nSECRET_KEY=el-de-antes\nLIBRAAUTH_COOKIE_SECURE=0"

    nuevo, agregadas = ins.completar_entorno(viejo, definicion)

    assert agregadas == ["LIBRAEDGE_CARGA_INICIAL"]
    assert nuevo == viejo + f"\nLIBRAEDGE_CARGA_INICIAL={VALOR}\n"
    assert ins.completar_entorno(nuevo, definicion) == (nuevo, [])
    # Sin la definicion es el comportamiento de antes.
    assert ins.completar_entorno(viejo) == (viejo, [])


def test_al_actualizar_se_respeta_lo_que_soporte_haya_puesto():
    definicion = _definicion("ventalibra")
    puesto = "LIBRAEDGE_CARGA_INICIAL=otro.modulo:otra\nLIBRAAUTH_COOKIE_SECURE=0\n"
    assert ins.completar_entorno(puesto, definicion) == (puesto, [])


def test_el_ayudante_completa_con_la_definicion_sin_mostrar_valores(tmp_path, capsys):
    env = tmp_path / "nodo.env"
    env.write_text("SECRET_KEY=el-de-antes\n", encoding="utf-8")

    assert ins.main(["completar-entorno", "--entorno", str(env),
                     "--definicion", str(INSTALADOR / "productos" / "restolibra.json")]) == 0

    salida = capsys.readouterr().out
    assert "LIBRAEDGE_CARGA_INICIAL" in salida and "LIBRAAUTH_COOKIE_SECURE" in salida and VALOR not in salida
    assert ins.leer_entorno(env.read_text(encoding="utf-8")) == {
        "SECRET_KEY": "el-de-antes", "LIBRAAUTH_COOKIE_SECURE": "0", "LIBRAEDGE_CARGA_INICIAL": VALOR}


def test_los_scripts_de_actualizacion_le_pasan_la_definicion():
    linux = (INSTALADOR / "linux" / "preparar_nodo.sh").read_text(encoding="utf-8")
    assert 'completar-entorno --entorno "$ARCHIVO_ENV" --definicion "$DEFINICION"' in linux
    windows = (INSTALADOR / "instalar.ps1").read_text(encoding="utf-8")
    rama = windows[windows.index("if ($SinVincular) {"):windows.index("exit $salida")]
    assert '"completar-entorno", "--entorno", $archivoEnv,' in rama
    assert '"--definicion", (Join-Path $RaizNodo "instalador\\producto.json")' in rama


def test_en_windows_el_entorno_de_los_servicios_se_vuelve_a_cargar_al_actualizar():
    """El entorno de un servicio de NSSM vive en el registro: sin esto la variable quedaba en el nodo.env."""
    comun = (INSTALADOR / "comun.ps1").read_text(encoding="utf-8")
    funcion = comun[comun.index("function Actualizar-NodoVinculado"):]
    carga = funcion.index("& $nssm set $servicio AppEnvironmentExtra $pares")
    assert funcion.index("Cargar-EntornoEnProceso") < carga < funcion.index("$Plan.migraciones")
    assert carga < funcion.index('"LibraEdgeProducto", "LibraEdgeNodo", "LibraEdgeDescubrimiento"')
    assert '@("LibraEdgeProducto", "LibraEdgeNodo")' in funcion


def test_el_ejemplo_de_nodo_env_la_explica():
    ejemplo = (INSTALADOR / "nodo.env.ejemplo").read_text(encoding="utf-8")
    assert f"LIBRAEDGE_CARGA_INICIAL={VALOR}" in ejemplo
