"""El nodo como proceso: ciclo, estado y deteccion de corte.

Fase 4. Antes de esto LibraEdge era una biblioteca que no corria sola: tenia
outbox, worker, transporte, receptor y espejo, y nadie los invocaba.
"""

import json
import os

import pytest

from libraedge.cli import tablas_espejo
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import OutboxOperation
from libraedge.nodo import EstadoNodo, Nodo, escribir_estado, leer_estado
from libraedge.sync.worker import OutboxWorker, PushResult


def operacion(sequence=1):
    return OutboxOperation(
        operation_id=f"node-1:{sequence}", node_id="node-1", sequence=sequence,
        operation_type="pedido.cobrado", aggregate_type="venta",
        aggregate_id=f"node-1:venta:{sequence}", occurred_at="2026-08-30T10:00:00Z",
        schema_version=1, payload={"total": "100.00"},
    )


class TransporteQueAcepta:
    def push(self, op):
        return PushResult("accepted")


class TransporteSinInternet:
    """Lo que hace `urlopen` cuando se corta el enlace."""

    def push(self, op):
        raise OSError("Network is unreachable")


class TransporteQueRechaza:
    def push(self, op):
        return PushResult("rejected", "el central no entiende esta operacion")


class PullQueAnda:
    def __init__(self, cambios=0):
        self._cambios = cambios
        self.llamadas = 0

    def run_once(self, node_id, limit=500):
        self.llamadas += 1
        return self._cambios


class PullSinInternet:
    def run_once(self, node_id, limit=500):
        raise OSError("Network is unreachable")


@pytest.fixture
def nodo_y_repo(repo, tmp_path):
    repo.register_node("node-1", branch_id="branch-1")
    return repo, str(tmp_path / "estado.json")


# ── El ciclo ─────────────────────────────────────────────────────────────

def test_un_ciclo_completo_sube_baja_y_queda_en_linea(nodo_y_repo):
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    pull = PullQueAnda(cambios=3)
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteQueAcepta()), pull, ruta)

    estado = nodo.sincronizar()

    assert estado.en_linea is True
    assert estado.operaciones_subidas == 1
    assert estado.cambios_bajados == 3
    assert estado.pendientes == 0
    assert estado.ultimo_error is None


def test_primero_sube_y_despues_baja(repo, tmp_path):
    """El orden no es casual: lo que el nodo genero durante el corte no existe
    en ningun otro lado, y los datos de referencia si. Subir primero achica la
    ventana en la que un dato irremplazable vive en un solo disco."""
    repo.register_node("node-1", branch_id="branch-1")
    repo.enqueue_operation(operacion())
    orden = []

    class TransporteQueAnota:
        def push(self, op):
            orden.append("subida")
            return PushResult("accepted")

    class PullQueAnota:
        def run_once(self, node_id, limit=500):
            orden.append("bajada")
            return 0

    Nodo(repo, "node-1", OutboxWorker(repo, TransporteQueAnota()), PullQueAnota()).sincronizar()

    assert orden == ["subida", "bajada"]


# ── La deteccion de corte ────────────────────────────────────────────────

def test_sin_internet_el_nodo_queda_fuera_de_linea(nodo_y_repo):
    """🔴 La trampa que este ciclo tuvo que resolver.

    `OutboxWorker` **atrapa** los errores de transporte a proposito --los
    convierte en reintentos, que es lo que lo hace durable-- asi que un `except`
    alrededor del worker no ve nada. Y su cuenta de `procesadas` incluye a las
    que fallaron. Un nodo que mirara cualquiera de las dos cosas se declararia
    en linea con la cola entera atascada, que es exactamente la pantalla que no
    puede mentir.
    """
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteSinInternet()),
                PullQueAnda(), ruta)

    estado = nodo.sincronizar()

    assert estado.en_linea is False
    assert estado.ultimo_error is not None
    assert "unreachable" in estado.ultimo_error


def test_sin_internet_no_se_pierde_nada(nodo_y_repo):
    """El corte no puede consumir la operacion: es la unica copia que hay."""
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteSinInternet()),
                PullQueAnda(), ruta)

    estado = nodo.sincronizar()

    assert estado.pendientes == 1
    assert estado.operaciones_subidas == 0


def test_el_nodo_no_se_cae_porque_se_caiga_internet(nodo_y_repo):
    """🔴 La razon de existir de todo esto.

    Un `raise` que salga de `sincronizar()` es un proceso muerto en la PC de un
    cliente **en el peor momento posible**: justo cuando se corto el enlace y el
    local depende del nodo para seguir cobrando.
    """
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteSinInternet()),
                PullSinInternet(), ruta)

    estado = nodo.sincronizar()  # no levanta

    assert estado.en_linea is False


def test_vuelve_a_estar_en_linea_cuando_vuelve_el_enlace(nodo_y_repo):
    """El ciclo completo del pedido del humano: se corta, opera local, vuelve y
    sincroniza."""
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())

    caido = Nodo(repo, "node-1", OutboxWorker(repo, TransporteSinInternet()),
                 PullQueAnda(), ruta)
    assert caido.sincronizar().en_linea is False

    # Vuelve el enlace: misma cola, transporte que anda.
    repo.enqueue_operation(operacion(sequence=2))
    recuperado = Nodo(repo, "node-1", OutboxWorker(repo, TransporteQueAcepta()),
                      PullQueAnda(), ruta)
    estado = recuperado.sincronizar()

    assert estado.en_linea is True
    assert estado.pendientes == 0
    assert estado.operaciones_subidas == 2, "tambien tiene que salir la que quedo del corte"


def test_una_operacion_rechazada_no_es_un_corte(nodo_y_repo):
    """Un rechazo del central no es falta de conectividad.

    Si se confundieran, la pantalla diria "sin internet" mientras el enlace anda
    perfecto, y nadie miraria la operacion que quedo en revision manual --que es
    lo que de verdad hay que atender--.
    """
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteQueRechaza()),
                PullQueAnda(), ruta)

    estado = nodo.sincronizar()

    assert estado.en_linea is True, "el enlace anda; lo que fallo es la operacion"
    assert estado.pendientes == 0, "quedo en revision manual, no pendiente de envio"


# ── El estado ────────────────────────────────────────────────────────────

def test_el_estado_se_escribe_y_se_relee(nodo_y_repo):
    repo, ruta = nodo_y_repo
    Nodo(repo, "node-1", OutboxWorker(repo, TransporteQueAcepta()),
         PullQueAnda(), ruta).sincronizar()

    leido = leer_estado(ruta)
    assert leido is not None
    assert leido.node_id == "node-1"
    assert leido.en_linea is True


def test_escribir_el_estado_no_deja_temporales(tmp_path):
    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo("node-1", True, 0, 0))
    escribir_estado(ruta, EstadoNodo("node-1", False, 7, 3))

    assert json.loads(open(ruta, encoding="utf-8").read())["pendientes"] == 7
    temporales = [n for n in os.listdir(tmp_path) if n.startswith(".estado-")]
    assert temporales == [], f"quedaron temporales sin limpiar: {temporales}"


def test_un_write_que_falla_no_destruye_el_estado_anterior(tmp_path, monkeypatch):
    """🔴 La propiedad real de la escritura atomica, no su forma.

    Un `open(ruta, "w")` **trunca el archivo antes de escribir nada**: si algo
    falla en el medio --disco lleno, proceso matado, el corte de luz que este
    proyecto entero asume que va a pasar-- el estado anterior ya no esta y la UI
    se queda sin nada que mostrar. Con temporal + `os.replace()` el archivo
    viejo sigue entero.

    El test anterior de esta propiedad **no la medía**: comprobaba que no
    quedaran temporales, y un write directo tampoco deja ninguno, asi que
    pasaba verde con la defensa sacada. Se verifico con una mutacion.
    """
    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo("node-1", True, 1, 1))
    original = open(ruta, encoding="utf-8").read()

    def serializacion_rota(self):
        raise OSError("No space left on device")

    monkeypatch.setattr(EstadoNodo, "como_json", serializacion_rota)
    with pytest.raises(OSError):
        escribir_estado(ruta, EstadoNodo("node-1", False, 99, 99))

    assert open(ruta, encoding="utf-8").read() == original, (
        "el write fallido se llevo puesto el estado anterior"
    )


def test_leer_un_estado_que_no_existe_no_revienta(tmp_path):
    assert leer_estado(str(tmp_path / "no-existe.json")) is None


def test_leer_un_estado_corrupto_no_revienta(tmp_path):
    """Un corte de luz a mitad de un write viejo, o alguien que lo edito.

    La UI tiene que poder decir "no se" en vez de morirse.
    """
    ruta = tmp_path / "estado.json"
    ruta.write_text('{"node_id": "node-1", "en_lin', encoding="utf-8")
    assert leer_estado(str(ruta)) is None


def test_el_estado_reporta_los_pendientes_reales(nodo_y_repo):
    repo, ruta = nodo_y_repo
    repo.enqueue_operation(operacion())
    repo.enqueue_operation(operacion(sequence=2))
    nodo = Nodo(repo, "node-1", OutboxWorker(repo, TransporteSinInternet()),
                PullQueAnda(), ruta)

    assert nodo.sincronizar().pendientes == 2


# ── El recorrido completo ────────────────────────────────────────────────

def test_un_ciclo_real_contra_un_central_de_verdad(monkeypatch, tmp_path):
    """🔴 La prueba de que las fases 2, 3 y 4 componen.

    Es el escenario del pedido original, entero: el nodo tiene una venta del
    corte sin sincronizar y el central tiene un precio nuevo. Un solo ciclo
    tiene que **subir la venta** y **bajar el precio**, por HTTP y con el
    secreto del nodo.

    Las piezas se venian probando por separado --outbox, transporte, receptor,
    changelog, espejo, ciclo-- y cada una en verde no dice nada de si encajan.
    Aca se usan los transportes HTTP **reales**, con `urlopen` apuntado al
    TestClient: se ejercita el armado de la URL, la cabecera y el JSON de
    verdad.

    Va sobre dos bases SQLite --una del nodo y otra del central-- porque lo que
    se mide es el encaje; lo que depende del motor tiene sus propios tests
    contra PostgreSQL.
    """
    import sqlite3

    from fastapi.testclient import TestClient

    from libraedge.db.changelog import init_changelog_schema, sembrar
    from libraedge.db.schema import init_schema
    from libraedge.sync import http as http_module
    from libraedge.sync import pull as pull_module
    from libraedge.sync.api import create_sync_app
    from libraedge.sync.pull import HttpPullTransport, MirrorApplier, PullWorker
    from libraedge.sync.receiver import SyncReceiver

    # --- El central: registra el nodo y publica un precio ---
    central = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(central)
    init_changelog_schema(central)
    central.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " nombre TEXT NOT NULL, precio NUMERIC)"
    )
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("Milanesa", "8500"))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    repo_central = NodeRepository(central)
    secreto = repo_central.register_node("node-1", branch_id="sucursal-centro")

    ventas_aplicadas = []
    app = create_sync_app(
        SyncReceiver(central, operation_handler=ventas_aplicadas.append),
        repo_central, changelog_conn=central,
    )
    cliente = TestClient(app)

    # --- El nodo: una venta del corte, sin sincronizar ---
    nodo_conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(nodo_conn)
    nodo_conn.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, precio NUMERIC)"
    )
    repo_nodo = NodeRepository(nodo_conn)
    repo_nodo.register_node("node-1", branch_id="sucursal-centro")
    repo_nodo.enqueue_operation(operacion())
    nodo_conn.commit()

    assert nodo_conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 0, (
        "el nodo arranca sin catalogo: si ya lo tuviera, la bajada no probaria nada"
    )

    # --- El cable: urlopen contra el TestClient ---
    class Respuesta:
        def __init__(self, cuerpo):
            self._cuerpo = cuerpo

        def read(self):
            return self._cuerpo

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen_al_central(request, timeout):
        ruta = request.full_url.replace("https://central.example", "")
        cabeceras = dict(request.header_items())
        if request.get_method() == "POST":
            respuesta = cliente.post(ruta, content=request.data, headers=cabeceras)
        else:
            respuesta = cliente.get(ruta, headers=cabeceras)
        assert respuesta.status_code == 200, respuesta.text
        return Respuesta(json.dumps(respuesta.json()).encode("utf-8"))

    monkeypatch.setattr(http_module, "urlopen", urlopen_al_central)
    monkeypatch.setattr(pull_module, "urlopen", urlopen_al_central)

    nodo = Nodo(
        repo_nodo, "node-1",
        outbox_worker=OutboxWorker(
            repo_nodo, http_module.HttpSyncTransport("https://central.example", secreto)),
        pull_worker=PullWorker(
            repo_nodo,
            HttpPullTransport("https://central.example", "node-1", secreto),
            MirrorApplier(nodo_conn, {"productos": "id"}),
        ),
        ruta_estado=str(tmp_path / "estado.json"),
    )

    estado = nodo.sincronizar()
    nodo_conn.commit()

    # La venta del corte llego al central...
    assert estado.en_linea is True, estado.ultimo_error
    assert estado.operaciones_subidas == 1
    assert estado.pendientes == 0
    assert len(ventas_aplicadas) == 1
    assert ventas_aplicadas[0].payload["total"] == "100.00"

    # ...y el catalogo del central llego al nodo.
    assert estado.cambios_bajados == 1
    espejado = nodo_conn.execute("SELECT nombre, precio FROM productos").fetchone()
    assert espejado[0] == "Milanesa"
    assert str(espejado[1]) == "8500"

    # Un segundo ciclo no repite nada.
    segundo = nodo.sincronizar()
    assert segundo.operaciones_subidas == 0
    assert segundo.cambios_bajados == 0
    assert len(ventas_aplicadas) == 1

    central.close()
    nodo_conn.close()


# ── La configuracion del proceso ─────────────────────────────────────────

def test_las_tablas_espejo_se_parsean():
    assert tablas_espejo("productos:id, clientes:id") == {
        "productos": "id", "clientes": "id"}


def test_la_pk_por_defecto_es_id():
    assert tablas_espejo("productos") == {"productos": "id"}


def test_una_lista_de_tablas_vacia_no_arranca():
    """🔴 La lista **es** el reparto de autoridad.

    Un default vacio la volveria opcional en la practica, y el aplicador
    aceptaria cambios del central sobre cualquier tabla del nodo, incluidas las
    que son de su propia autoridad.
    """
    with pytest.raises(SystemExit):
        tablas_espejo("")


def test_una_variable_que_falta_no_arranca_a_medias(monkeypatch):
    """Falla temprano y diciendo cual falta.

    Un nodo que arranque sin `LIBRAEDGE_CENTRAL_URL` no falla: sincroniza contra
    el lugar equivocado, o contra ninguno, y se ve igual que uno sano.
    """
    from libraedge.cli import _requerido

    monkeypatch.delenv("LIBRAEDGE_CENTRAL_URL", raising=False)
    with pytest.raises(SystemExit, match="LIBRAEDGE_CENTRAL_URL"):
        _requerido("LIBRAEDGE_CENTRAL_URL")


def _entorno_de_nodo(monkeypatch, url_base, ruta_estado, central="http://127.0.0.1:9"):
    """El entorno que escribe el instalador.

    El central por defecto apunta al **puerto 9** (discard), que no escucha: es
    la forma mas parecida a "se corto internet" que se puede montar sin red.
    """
    monkeypatch.setenv("LIBRAEDGE_NODE_ID", "node-1")
    monkeypatch.setenv("LIBRAEDGE_NODE_SECRET", "el-secreto")
    monkeypatch.setenv("LIBRAEDGE_CENTRAL_URL", central)
    monkeypatch.setenv("LIBRAEDGE_DATABASE_URL", url_base)
    monkeypatch.setenv("LIBRAEDGE_TABLAS_ESPEJO", "productos:id")
    monkeypatch.setenv("LIBRAEDGE_ESTADO", ruta_estado)


def test_el_cli_sincroniza_sin_central_y_sale_1_sin_romperse(monkeypatch, tmp_path, capsys):
    """🔴 El caso para el que existe todo esto, por el camino real del proceso.

    Se levanta el nodo **como lo levanta el servicio** --desde el entorno, con
    `main(["sincronizar"])`-- contra un central que no contesta. Tiene que
    terminar ordenado, escribir el estado y salir 1, no reventar con un
    traceback en la PC de un cliente.
    """
    from tests.conftest import url_postgres

    from libraedge.cli import main
    from libraedge.db.schema import init_schema

    url = url_postgres()
    from libracore.db import core

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

    ruta = str(tmp_path / "estado.json")
    _entorno_de_nodo(monkeypatch, url, ruta)

    codigo = main(["sincronizar"])

    assert codigo == 1, "sin central tiene que salir distinto de cero"
    estado = leer_estado(ruta)
    assert estado is not None, "aun sin enlace tiene que dejar el estado escrito"
    assert estado.en_linea is False
    assert estado.ultimo_error is not None
    salida = json.loads(capsys.readouterr().out)
    assert salida["en_linea"] is False

    core._db_path = None
    core._database_url = None


def test_el_cli_correr_cicla_y_espera(monkeypatch, tmp_path):
    """El modo servicio: cicla, no sale sola.

    Se corta con un `sleep` que levanta a la segunda vuelta -- sin eso el test
    no terminaria nunca, que es justamente lo que el comando tiene que hacer.
    """
    from tests.conftest import url_postgres

    from libraedge.cli import main
    from libraedge.db.schema import init_schema

    url = url_postgres()
    from libracore.db import core

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

    vueltas = []

    def sleep_que_corta(segundos):
        vueltas.append(segundos)
        if len(vueltas) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("libraedge.cli.time.sleep", sleep_que_corta)

    with pytest.raises(KeyboardInterrupt):
        main(["correr", "--intervalo", "7"])

    assert vueltas == [7, 7], "tiene que ciclar con el intervalo pedido"

    core._db_path = None
    core._database_url = None


def test_el_comando_estado_no_necesita_la_base(tmp_path, monkeypatch, capsys):
    """Tiene que contestar con el servicio caido, que es cuando se pregunta.

    Si `estado` armara el nodo, necesitaria la base y la URL del central: no
    podria responder justo en el escenario para el que existe.
    """
    from libraedge.cli import main

    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo("node-1", False, 4, 9, ultimo_error="sin enlace"))
    monkeypatch.setenv("LIBRAEDGE_ESTADO", ruta)
    for sobrante in ("LIBRAEDGE_DATABASE_URL", "LIBRAEDGE_CENTRAL_URL",
                     "LIBRAEDGE_NODE_ID", "LIBRAEDGE_NODE_SECRET",
                     "LIBRAEDGE_TABLAS_ESPEJO"):
        monkeypatch.delenv(sobrante, raising=False)

    assert main(["estado"]) == 0
    salida = json.loads(capsys.readouterr().out)
    assert salida["pendientes"] == 4
    assert salida["en_linea"] is False


def test_el_comando_estado_sin_haber_sincronizado_sale_distinto(tmp_path, monkeypatch, capsys):
    """Un cero esperado necesita que el positivo tambien se vea: el test de
    arriba, sobre el mismo comando, devuelve 0."""
    from libraedge.cli import main

    monkeypatch.setenv("LIBRAEDGE_ESTADO", str(tmp_path / "todavia-no.json"))
    assert main(["estado"]) == 1
    assert "sin sincronizar" in capsys.readouterr().out
