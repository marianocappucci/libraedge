"""La bajada: changelog central, cursor, espejo en el nodo.

Fase 2 del nodo espejo (2026-08-30). Antes de esto LibraEdge era solo-subida y
`node_identity.last_server_cursor` no lo leia ni lo escribia nadie.

Los tests van contra los dos motores donde tiene sentido. El trigger es
PostgreSQL puro --el central siempre corre PostgreSQL-- y por eso su test se
saltea explicito en `[sqlite]`, diciendo por que, en vez de no existir.
"""

import decimal

import pytest

from libraedge.db.changelog import (
    cargar_payload,
    init_changelog_schema,
    instalar_trigger,
    listar_cambios,
    sembrar,
    validar_identificador,
)
from libraedge.db.repository import NodeRepository
from libraedge.domain.sync import ReferenceChange, ReferenceOperation
from libraedge.sync.pull import MirrorApplier, PullWorker, serializar_cambio


@pytest.fixture
def central(conn):
    """Una base con el changelog y una tabla de referencia de juguete."""
    init_changelog_schema(conn)
    conn.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " nombre TEXT NOT NULL, precio NUMERIC)"
    )
    conn.commit()
    return conn


def cambio(cursor=1, tabla="productos", row_id="1", operacion="upsert", payload=None):
    return ReferenceChange(
        cursor=cursor, table_name=tabla, row_id=row_id,
        operation=ReferenceOperation(operacion),
        payload=payload if payload is not None else {"id": 1, "nombre": "yerba"},
    )


# --------------------------------------------------------------------------
# El changelog
# --------------------------------------------------------------------------

def test_sembrar_vuelca_el_estado_actual_como_upserts(central):
    """El snapshot inicial no es un mecanismo aparte: es sembrar el changelog."""
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("yerba", 100))
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("mate", 250))
    central.commit()

    assert sembrar(central, "productos") == 2
    central.commit()

    cambios = listar_cambios(central)
    assert [c.table_name for c in cambios] == ["productos", "productos"]
    assert all(c.operation == ReferenceOperation.UPSERT for c in cambios)
    assert {c.payload["nombre"] for c in cambios} == {"yerba", "mate"}


def test_sembrar_una_tabla_vacia_no_inventa_cambios(central):
    """Un cero tiene que ser un cero medido, no la tabla equivocada."""
    assert sembrar(central, "productos") == 0
    assert listar_cambios(central) == ()


def test_listar_cambios_respeta_el_cursor_y_el_orden(central):
    central.execute("INSERT INTO productos (nombre) VALUES (?)", ("uno",))
    central.execute("INSERT INTO productos (nombre) VALUES (?)", ("dos",))
    central.execute("INSERT INTO productos (nombre) VALUES (?)", ("tres",))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    todos = listar_cambios(central)
    assert [c.cursor for c in todos] == sorted(c.cursor for c in todos)

    desde_el_primero = listar_cambios(central, desde=todos[0].cursor)
    assert len(desde_el_primero) == len(todos) - 1
    assert desde_el_primero[0].cursor == todos[1].cursor


def test_listar_cambios_respeta_el_limite(central):
    for nombre in ("uno", "dos", "tres", "cuatro"):
        central.execute("INSERT INTO productos (nombre) VALUES (?)", (nombre,))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    assert len(listar_cambios(central, limit=2)) == 2


def test_el_trigger_captura_insert_update_y_delete(central, motor):
    """CDC por trigger: ningun camino de escritura puede saltearse.

    Es lo que evita tener que publicar a mano las 26 tablas de referencia --el
    que se olvide no falla, deja de espejarse en silencio--.
    """
    if motor == "sqlite":
        pytest.skip(
            "el trigger es PL/pgSQL y el central siempre corre PostgreSQL; "
            "en SQLite no hay nada equivalente que probar"
        )
    instalar_trigger(central, "productos")
    central.commit()

    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("yerba", 100))
    central.execute("UPDATE productos SET precio = ? WHERE nombre = ?", (120, "yerba"))
    central.execute("DELETE FROM productos WHERE nombre = ?", ("yerba",))
    central.commit()

    cambios = listar_cambios(central)
    assert [str(c.operation) for c in cambios] == ["upsert", "upsert", "delete"]
    assert cambios[1].payload["precio"] == decimal.Decimal("120")
    assert all(c.row_id == "1" for c in cambios)


def test_instalar_trigger_es_idempotente(central, motor):
    if motor == "sqlite":
        pytest.skip("el trigger es PL/pgSQL; ver el test de arriba")
    instalar_trigger(central, "productos")
    instalar_trigger(central, "productos")
    central.commit()

    central.execute("INSERT INTO productos (nombre) VALUES (?)", ("yerba",))
    central.commit()

    # Un trigger duplicado daria dos filas por la misma escritura.
    assert len(listar_cambios(central)) == 1


@pytest.mark.parametrize(
    "nombre", ["productos; DROP TABLE productos", "pro ductos", "", "1tabla", "tabla-x"]
)
def test_validar_identificador_rechaza_lo_que_no_es_un_nombre(nombre):
    """Los nombres de tabla llegan desde la red y se interpolan en SQL."""
    with pytest.raises(ValueError):
        validar_identificador(nombre)


# --------------------------------------------------------------------------
# El cursor del nodo
# --------------------------------------------------------------------------

def test_un_nodo_nuevo_arranca_en_cero(repo):
    repo.register_node("node-1", branch_id="branch-1")
    assert repo.get_server_cursor("node-1") == 0


def test_el_cursor_avanza_y_se_relee(repo):
    repo.register_node("node-1", branch_id="branch-1")
    repo.set_server_cursor("node-1", 42)
    assert repo.get_server_cursor("node-1") == 42


def test_el_cursor_nunca_retrocede(repo):
    """Dos ciclos de bajada superpuestos pueden terminar fuera de orden.

    El que termine ultimo no puede hacer que el nodo vuelva a pedir cambios que
    ya aplico: reaplicar es inofensivo, pero rehacer el snapshot entero sobre
    una base viva no es lo mismo que no hacer nada.
    """
    repo.register_node("node-1", branch_id="branch-1")
    repo.set_server_cursor("node-1", 100)
    repo.set_server_cursor("node-1", 7)
    assert repo.get_server_cursor("node-1") == 100


# --------------------------------------------------------------------------
# El espejo en el nodo
# --------------------------------------------------------------------------

@pytest.fixture
def nodo(conn):
    """Una base de nodo con la tabla de referencia que va a espejar."""
    conn.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY,"
        " nombre TEXT NOT NULL, precio NUMERIC)"
    )
    conn.commit()
    return conn


def test_el_espejo_inserta_una_fila_nueva(nodo):
    MirrorApplier(nodo, {"productos": "id"}).aplicar(
        cambio(payload={"id": 1, "nombre": "yerba", "precio": "100.50"})
    )
    nodo.commit()
    fila = nodo.execute("SELECT nombre, precio FROM productos WHERE id = ?", (1,)).fetchone()
    assert fila[0] == "yerba"


def test_el_espejo_actualiza_una_fila_que_ya_estaba(nodo):
    aplicador = MirrorApplier(nodo, {"productos": "id"})
    aplicador.aplicar(cambio(payload={"id": 1, "nombre": "yerba", "precio": "100"}))
    aplicador.aplicar(cambio(cursor=2, payload={"id": 1, "nombre": "yerba", "precio": "120"}))
    nodo.commit()
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 1
    leido = nodo.execute("SELECT precio FROM productos").fetchone()[0]
    assert decimal.Decimal(str(leido)) == decimal.Decimal("120")


def test_el_espejo_es_idempotente(nodo):
    """Reaplicar tiene que ser inofensivo: es lo que sostiene el orden
    aplicar-primero-avanzar-despues ante un corte de luz."""
    aplicador = MirrorApplier(nodo, {"productos": "id"})
    uno = cambio(payload={"id": 1, "nombre": "yerba", "precio": "100"})
    aplicador.aplicar(uno)
    aplicador.aplicar(uno)
    aplicador.aplicar(uno)
    nodo.commit()
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 1


def test_el_espejo_borra(nodo):
    aplicador = MirrorApplier(nodo, {"productos": "id"})
    aplicador.aplicar(cambio(payload={"id": 1, "nombre": "yerba"}))
    aplicador.aplicar(cambio(cursor=2, operacion="delete", payload={"id": 1, "nombre": "yerba"}))
    nodo.commit()
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 0


def test_el_espejo_rechaza_una_tabla_que_no_esta_en_la_lista(nodo):
    """La lista de tablas ES el reparto de autoridad, hecho codigo.

    El nombre de la tabla llega desde la red. Sin esto, un central comprometido
    --o con un trigger de mas-- podria hacer que el nodo escriba en una tabla de
    SU autoridad, como `ventas`, que es justo lo que el diseño promete que no
    puede pasar.
    """
    aplicador = MirrorApplier(nodo, {"productos": "id"})
    with pytest.raises(PermissionError):
        aplicador.aplicar(cambio(tabla="ventas", payload={"id": 1, "total": "999"}))


def test_el_espejo_rechaza_una_columna_que_no_es_un_identificador(nodo):
    aplicador = MirrorApplier(nodo, {"productos": "id"})
    with pytest.raises(ValueError):
        aplicador.aplicar(cambio(payload={"id": 1, "nombre) VALUES (1); DROP": "x"}))


#: Un valor que **float no puede representar**. Es la parte que importa del
#: test de precision: con `19.99` el test pasa aunque el valor se haya degradado
#: a float, porque `str(19.99)` es `"19.99"` en los dos casos. Con esto no:
#: `float("12345678901234567890.123456789")` da `1.2345678901234567e+19`.
PRECIO_QUE_FLOAT_ROMPE = "12345678901234567890.123456789"


def test_el_precio_no_pierde_precision_al_guardarse_en_el_nodo(nodo, motor):
    """El valor llega exacto **a la tabla del nodo**, no sólo al cable.

    Se saltea en `[sqlite]` y el motivo es un hallazgo, no una comodidad: la
    **afinidad de tipo** de SQLite convierte lo que se guarda en una columna
    `NUMERIC` a REAL, así que ahí la precisión se pierde en el último paso por
    más que el viaje la haya conservado. Un nodo sobre SQLite no podría espejar
    un precio exacto — un argumento más para la decisión de que el nodo corra
    PostgreSQL.
    """
    if motor == "sqlite":
        pytest.skip(
            "la afinidad NUMERIC de SQLite degrada el valor a REAL al guardarlo; "
            "el nodo corre PostgreSQL"
        )
    payload = cargar_payload(
        '{"id": 1, "nombre": "yerba", "precio": ' + PRECIO_QUE_FLOAT_ROMPE + "}"
    )
    en_el_cable = serializar_cambio(
        ReferenceChange(
            cursor=1, table_name="productos", row_id="1",
            operation=ReferenceOperation.UPSERT, payload=payload,
        )
    )
    MirrorApplier(nodo, {"productos": "id"}).aplicar(
        ReferenceChange(
            cursor=1, table_name="productos", row_id="1",
            operation=ReferenceOperation.UPSERT, payload=en_el_cable["payload"],
        )
    )
    nodo.commit()
    # Se relee como TEXTO: la capa de conexión de LibraCore devuelve los NUMERIC
    # como float --decisión deliberada de la familia-- así que preguntar por la
    # columna directamente mediría esa conversión y no lo que se guardó.
    guardado = nodo.execute(
        "SELECT CAST(precio AS TEXT) FROM productos WHERE id = ?", (1,)
    ).fetchone()[0]
    assert decimal.Decimal(guardado) == decimal.Decimal(PRECIO_QUE_FLOAT_ROMPE)


def test_el_precio_no_pierde_precision_al_viajar(nodo):
    """🔴 Un precio degradado a float es el dato con el que se cobra en el corte.

    `to_jsonb` de PostgreSQL escribe el NUMERIC como numero JSON con sus digitos
    exactos; `json.loads` por defecto lo levanta como `float` y los pierde. Por
    eso el payload se lee con `parse_float=Decimal` y los decimales viajan como
    texto por el cable.

    **El valor de prueba no es `19.99` a proposito** — ver
    `PRECIO_QUE_FLOAT_ROMPE`: con dos decimales, este test pasaria igual con el
    defecto puesto.
    """
    # Tal como sale del changelog: un numero JSON con todos sus digitos.
    payload = cargar_payload(
        '{"id": 1, "nombre": "yerba", "precio": ' + PRECIO_QUE_FLOAT_ROMPE + "}"
    )
    assert isinstance(payload["precio"], decimal.Decimal), "se leyo como float"
    assert payload["precio"] == decimal.Decimal(PRECIO_QUE_FLOAT_ROMPE)

    en_el_cable = serializar_cambio(
        ReferenceChange(
            cursor=1, table_name="productos", row_id="1",
            operation=ReferenceOperation.UPSERT, payload=payload,
        )
    )
    assert en_el_cable["payload"]["precio"] == PRECIO_QUE_FLOAT_ROMPE, (
        "el decimal tiene que viajar como texto: como numero JSON, el "
        "jsonable_encoder de FastAPI lo pasa por float"
    )


def test_sembrar_conserva_la_precision_igual_que_el_trigger(central, motor):
    """El snapshot inicial y las actualizaciones tienen que dar lo mismo.

    🔴 `sembrar()` leia con `SELECT *`, que pasa por la conversion a float de la
    capa de LibraCore, mientras que el trigger lee `to_jsonb(...)::text`, que no.
    El resultado era **el mismo precio escrito de dos formas segun por que camino
    llego** — y el redondeado era el del nodo recien instalado.
    """
    if motor == "sqlite":
        pytest.skip("la conversion a float es de la capa PostgreSQL de LibraCore")

    central.execute(
        "INSERT INTO productos (nombre, precio) VALUES (?, ?)",
        ("yerba", PRECIO_QUE_FLOAT_ROMPE),
    )
    central.commit()
    sembrar(central, "productos")
    central.commit()

    sembrado = listar_cambios(central)[0]
    assert sembrado.payload["precio"] == decimal.Decimal(PRECIO_QUE_FLOAT_ROMPE)


# --------------------------------------------------------------------------
# El worker
# --------------------------------------------------------------------------

def _cliente(conn, changelog_conn=None):
    from fastapi.testclient import TestClient

    from libraedge.sync.api import create_sync_app
    from libraedge.sync.receiver import SyncReceiver

    node_repo = NodeRepository(conn)
    secret = node_repo.register_node("node-1", branch_id="branch-1")
    app = create_sync_app(SyncReceiver(conn), node_repo, changelog_conn=changelog_conn)
    return TestClient(app), secret


def test_pull_sin_token_es_rechazado(central):
    cliente, _ = _cliente(central, central)
    assert cliente.get("/sync/v1/pull", params={"node_id": "node-1"}).status_code == 401


def test_pull_con_secreto_de_otro_nodo_es_rechazado(central):
    """La bajada tiene el mismo gate que la subida, no uno mas laxo.

    Sin esto, cualquiera que alcance el endpoint se lleva el catalogo, los
    precios y los clientes enteros: la bajada es de LECTURA, pero de todo.
    """
    cliente, secreto = _cliente(central, central)
    respuesta = cliente.get(
        "/sync/v1/pull", params={"node_id": "node-99"},
        headers={"Authorization": f"Bearer {secreto}"},
    )
    assert respuesta.status_code == 401


def test_pull_en_un_central_sin_changelog_responde_501(central):
    """501 y no 404: un 404 lo leeria un nodo como "version vieja del central"."""
    cliente, secreto = _cliente(central, changelog_conn=None)
    respuesta = cliente.get(
        "/sync/v1/pull", params={"node_id": "node-1"},
        headers={"Authorization": f"Bearer {secreto}"},
    )
    assert respuesta.status_code == 501


def test_pull_devuelve_los_cambios_desde_el_cursor(central):
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("yerba", 100))
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("mate", 250))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    cliente, secreto = _cliente(central, central)
    cabeceras = {"Authorization": f"Bearer {secreto}"}

    todo = cliente.get("/sync/v1/pull", params={"node_id": "node-1"}, headers=cabeceras).json()
    assert len(todo["changes"]) == 2
    assert todo["cursor"] == todo["changes"][-1]["cursor"]

    resto = cliente.get(
        "/sync/v1/pull",
        params={"node_id": "node-1", "cursor": todo["changes"][0]["cursor"]},
        headers=cabeceras,
    ).json()
    assert len(resto["changes"]) == 1


def test_pull_sin_cambios_devuelve_el_mismo_cursor(central):
    """Un cero esperado necesita que el positivo tambien se vea: ver el test
    de arriba, que sobre el mismo montaje devuelve 2."""
    cliente, secreto = _cliente(central, central)
    respuesta = cliente.get(
        "/sync/v1/pull", params={"node_id": "node-1", "cursor": 77},
        headers={"Authorization": f"Bearer {secreto}"},
    ).json()
    assert respuesta["changes"] == []
    assert respuesta["cursor"] == 77


def test_recorrido_completo_el_nodo_espeja_un_producto_del_central(monkeypatch):
    """De punta a punta: el central publica, el nodo lo espeja por HTTP.

    Va sobre dos bases SQLite --una del central y otra del nodo-- porque lo que
    se ejercita aca es el **cableado**: autenticacion, forma del JSON, cursor y
    aplicador. Lo que si depende del motor --el trigger y la precision decimal--
    tiene sus propios tests contra PostgreSQL; hacerlo todo aca obligaria a dos
    bases PostgreSQL separadas sin agregar nada que no este cubierto.
    """
    import json
    import sqlite3

    from libraedge.db.schema import init_schema
    from libraedge.sync import pull as modulo_pull

    # --- El central: changelog sembrado con un producto ---
    central = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(central)
    init_changelog_schema(central)
    central.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " nombre TEXT NOT NULL, precio NUMERIC)"
    )
    central.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", ("yerba", "100.50"))
    central.commit()
    sembrar(central, "productos")
    central.commit()

    cliente, secreto = _cliente(central, central)

    # --- El nodo: base propia, vacia, con la tabla a espejar ---
    nodo = sqlite3.connect(":memory:", check_same_thread=False)
    init_schema(nodo)
    nodo.execute(
        "CREATE TABLE productos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, precio NUMERIC)"
    )
    repo_nodo = NodeRepository(nodo)
    repo_nodo.register_node("node-1", branch_id="branch-1")
    nodo.commit()
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 0

    # El transporte real, con `urlopen` apuntado al TestClient: se ejercita el
    # armado de la URL y de la cabecera de verdad, no una version de mentira.
    class RespuestaFalsa:
        def __init__(self, cuerpo):
            self._cuerpo = cuerpo

        def read(self):
            return self._cuerpo

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen_al_testclient(request, timeout):
        url = request.full_url.replace("https://central.example", "")
        respuesta = cliente.get(url, headers=dict(request.header_items()))
        assert respuesta.status_code == 200, respuesta.text
        return RespuestaFalsa(json.dumps(respuesta.json()).encode("utf-8"))

    monkeypatch.setattr(modulo_pull, "urlopen", urlopen_al_testclient)

    transporte = modulo_pull.HttpPullTransport("https://central.example", "node-1", secreto)
    worker = modulo_pull.PullWorker(
        repo_nodo, transporte, MirrorApplier(nodo, {"productos": "id"})
    )

    assert worker.run_once("node-1") == 1
    nodo.commit()

    espejado = nodo.execute("SELECT id, nombre FROM productos").fetchone()
    assert espejado[1] == "yerba"
    assert repo_nodo.get_server_cursor("node-1") > 0

    # Un segundo ciclo sin cambios nuevos no vuelve a traer nada.
    assert worker.run_once("node-1") == 0

    # Y un cambio posterior en el central llega al nodo.
    central.execute("UPDATE productos SET precio = ? WHERE id = 1", ("120.75",))
    central.commit()
    sembrar(central, "productos")
    central.commit()
    assert worker.run_once("node-1") == 1
    nodo.commit()
    assert str(nodo.execute("SELECT precio FROM productos").fetchone()[0]) == "120.75"

    central.close()
    nodo.close()


class TransporteFalso:
    def __init__(self, cambios):
        self._cambios = cambios
        self.pedidos = []

    def pull(self, cursor=0, limit=500):
        self.pedidos.append(cursor)
        return tuple(c for c in self._cambios if c.cursor > cursor)[:limit]


def test_el_worker_aplica_y_avanza_el_cursor(repo, nodo):
    repo.register_node("node-1", branch_id="branch-1")
    transporte = TransporteFalso([
        cambio(cursor=1, row_id="1", payload={"id": 1, "nombre": "yerba"}),
        cambio(cursor=2, row_id="2", payload={"id": 2, "nombre": "mate"}),
    ])
    worker = PullWorker(repo, transporte, MirrorApplier(nodo, {"productos": "id"}))

    assert worker.run_once("node-1") == 2
    nodo.commit()
    assert repo.get_server_cursor("node-1") == 2
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 2


def test_el_worker_pide_desde_el_cursor_guardado(repo, nodo):
    """Sin esto el nodo rehace el snapshot entero en cada ciclo."""
    repo.register_node("node-1", branch_id="branch-1")
    repo.set_server_cursor("node-1", 1)
    transporte = TransporteFalso([
        cambio(cursor=1, row_id="1", payload={"id": 1, "nombre": "yerba"}),
        cambio(cursor=2, row_id="2", payload={"id": 2, "nombre": "mate"}),
    ])
    worker = PullWorker(repo, transporte, MirrorApplier(nodo, {"productos": "id"}))

    assert worker.run_once("node-1") == 1
    nodo.commit()
    assert transporte.pedidos == [1]
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 1


def test_el_cursor_no_avanza_mas_alla_de_lo_aplicado(repo, nodo):
    """🔴 La propiedad que hace que un corte de luz no pierda datos.

    Si el aplicador falla en el segundo cambio, el cursor tiene que haber
    quedado en el primero: el arranque siguiente vuelve a pedir desde ahi. Si
    avanzara antes de aplicar, ese dato quedaria viejo para siempre y nadie se
    enteraria.
    """
    repo.register_node("node-1", branch_id="branch-1")
    transporte = TransporteFalso([
        cambio(cursor=1, row_id="1", payload={"id": 1, "nombre": "yerba"}),
        cambio(cursor=2, tabla="ventas", row_id="2", payload={"id": 2, "total": "999"}),
        cambio(cursor=3, row_id="3", payload={"id": 3, "nombre": "mate"}),
    ])
    worker = PullWorker(repo, transporte, MirrorApplier(nodo, {"productos": "id"}))

    with pytest.raises(PermissionError):
        worker.run_once("node-1")
    nodo.commit()

    assert repo.get_server_cursor("node-1") == 1
    assert nodo.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 1


# ── El nodo que nadie registró localmente ────────────────────────────────

def test_un_nodo_recien_instalado_guarda_su_cursor(repo, nodo, tmp_path):
    """🔴 El defecto que apareció al cerrar el circuito contra el central real.

    **Ningún test de este archivo llamaba a `register_node()` de más ni de
    menos: todos lo llamaban.** Y en producción, del lado del nodo, no lo llama
    nadie — el central registra el nodo en SU base, el instalador escribe el
    `.env`, y la tabla `node_identity` del nodo queda vacía.

    Con esa tabla vacía, `set_server_cursor` hace `UPDATE ... WHERE node_id = ?`
    sobre cero filas. Eso **no falla**: un UPDATE que no toca nada es un éxito.
    Así que el cursor nunca se guarda y el nodo vuelve a bajar el espejo entero
    en cada ciclo, para siempre. Como los upserts del aplicador son
    idempotentes, los datos quedan bien y nadie se entera.

    Medido contra el central de demo el 2026-08-31: 102 cambios bajados, y otra
    vez los mismos 102 al minuto siguiente.

    Este test arranca **sin `register_node`**, como una instalación de verdad.
    """
    from libraedge.nodo import Nodo

    assert repo._conn.execute(
        "SELECT COUNT(*) FROM node_identity").fetchone()[0] == 0, (
        "el arranque de este test es una base SIN la fila del nodo: si algún "
        "fixture la crea, el test deja de medir lo que dice medir")

    transporte = TransporteFalso([
        cambio(cursor=1, row_id="1", payload={"id": 1, "nombre": "yerba"}),
        cambio(cursor=2, row_id="2", payload={"id": 2, "nombre": "mate"}),
    ])
    worker = PullWorker(repo, transporte, MirrorApplier(nodo, {"productos": "id"}))
    n = Nodo(repo, "node-1", pull_worker=worker,
             ruta_estado=str(tmp_path / "estado.json"))

    assert n.sincronizar().cambios_bajados == 2
    nodo.commit()
    assert repo.get_server_cursor("node-1") == 2, (
        "el cursor no se guardó: la fila propia del nodo no existía")

    # Y la consecuencia observable, que es la que se vio en la VM: el segundo
    # ciclo no puede volver a pedir desde cero.
    assert n.sincronizar().cambios_bajados == 0
    assert transporte.pedidos == [0, 2], (
        "el segundo ciclo tiene que pedir desde 2, no desde 0")
