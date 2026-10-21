"""La bajada: el nodo espeja los datos de referencia del central.

La contraparte de `worker.py`. Aquel drena el outbox hacia arriba --los eventos
que el nodo genero--; este trae hacia abajo el estado de los datos de referencia
--catalogo, precios, clientes-- que el nodo solo espeja y nunca edita.

## La propiedad que hace que esto sea seguro ante un corte

El nodo **aplica primero y avanza el cursor despues**, nunca al reves. Si se
corta la luz en el medio, el cursor sigue apuntando a antes de lo ya aplicado y
esos cambios se vuelven a aplicar en el arranque siguiente. Como son `upsert` por
clave primaria --idempotentes--, reaplicar es inofensivo. Al reves seria un
agujero: el cursor diria que ya se aplico algo que no se aplico, y ese dato
quedaria viejo para siempre.

Por eso el orden importa mas que la transaccionalidad: no hace falta que apply y
cursor entren en la misma transaccion, hace falta que el cursor nunca vaya
adelante.
"""

import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from libraedge.altas import normalizar_altas
from libraedge.db.changelog import cargar_payload, columnas_de_clave, validar_identificador
from libraedge.db.guarda import marcar_aplicando
from libraedge.domain.sync import ReferenceChange, ReferenceOperation
from libraedge.mapeo import TABLA as TABLA_MAPEO
from libraedge.sync import esquema, reconciliacion


class HttpPullTransport:
    """Trae cambios del central. Misma autenticacion por nodo que la subida."""

    def __init__(self, base_url: str, node_id: str, node_secret: str, timeout: float = 10.0,
                 version_esquema: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.node_secret = node_secret
        self.timeout = timeout
        #: Ver `sync/esquema.py`: con esto el central puede contestar 409 antes
        #: de mandar filas con columnas que la base del nodo todavia no tiene.
        self.version_esquema = version_esquema

    def pull(self, cursor: int = 0, limit: int = 500) -> tuple[ReferenceChange, ...]:
        consulta = urlencode({"node_id": self.node_id, "cursor": cursor, "limit": limit})
        request = Request(
            f"{self.base_url}/sync/v1/pull?{consulta}",
            headers=esquema.cabeceras_del_nodo(self.node_secret, self.version_esquema),
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as respuesta:
                crudo = respuesta.read()
        except HTTPError as exc:
            esquema.relanzar_si_es_de_esquema(exc)
            raise
        # Sin `parse_float=Decimal` los precios que vienen como NUMERIC del
        # central se degradarian a float justo antes de escribirse en el nodo,
        # que es el dato con el que se cobra durante el corte.
        datos = cargar_payload(crudo.decode("utf-8")) or {}
        return tuple(
            ReferenceChange(
                cursor=cambio["cursor"], table_name=cambio["table_name"],
                row_id=cambio["row_id"], operation=ReferenceOperation(cambio["operation"]),
                payload=cambio.get("payload"), recorded_at=cambio.get("recorded_at"),
            )
            for cambio in datos.get("changes", ())
        )


class MirrorApplier:
    """Escribe un `ReferenceChange` en la base local del nodo.

    Es **generico**: arma el upsert a partir de las claves del payload, asi que
    no conoce ninguna tabla del producto. Eso es lo que permite espejar 26 tablas
    sin escribir 26 aplicadores, y lo que mantiene a LibraEdge sin saber que es
    un producto o un precio.

    `tablas_permitidas` no es opcional y no es decorativo: el nombre de la tabla
    llega **desde la red** y termina interpolado en un `INSERT`. Sin la lista, un
    central comprometido --o simplemente con un trigger de mas-- podria hacer que
    el nodo escriba en cualquier tabla suya, incluidas las que son de su
    autoridad, como `ventas`. La lista es el reparto de autoridad, hecho codigo.

    La clave de cada tabla es una columna (`"id"`) o, desde v0.7, varias
    (`("party_id", "role")`): una tabla de relacion como `party_roles` de
    VentaLibra no tiene `id`. Con clave compuesta el upsert y el borrado salen
    de las columnas de la clave en el payload --que trae la fila entera, tambien
    en un `delete`--, no del `row_id`.

    ## Desde v0.17: abre la guarda del espejo (ADR-019)

    Si el nodo instalo `instalar_guarda_de_espejo` en alguna tabla, solo este
    aplicador puede escribirla: antes de cada cambio fija `libraedge.aplicando`
    con `SET LOCAL`, que vale hasta el commit de esa transaccion. Hay que fijarla
    en **cada** `aplicar` y no una vez al construirlo: `PullWorker` commitea
    despues de cada cambio y el `SET LOCAL` muere con el commit. Sobre una tabla
    sin guarda no cambia nada, y sobre SQLite es un no-op.

    ## Desde v0.18: reapunta las altas del nodo (ADR-021)

    Con `node_id` y `altas` (las tablas donde el nodo da de alta filas con un id provisorio negativo), una fila de
    `libraedge_mapeo` **de este nodo** no solo se guarda: reapunta lo que referenciaba al id provisorio hacia el
    definitivo y borra la fila provisoria (`sync/reconciliacion.py`), en la misma transaccion del `aplicar`. Las filas
    del mapeo de **otros** nodos se ignoran: no son del nodo ni le sirven. Un reapuntado que falla **no frena el
    espejo**: se deshace (`SAVEPOINT`), queda anotado en `sin_reconciliar` y en stderr, y el barrido del final de cada
    ciclo (`reconciliar_pendientes`) lo reintenta; lo peor que pasa es que la fila provisoria sigue un rato junto a la
    definitiva. Sin `altas` el aplicador es el de v0.17.
    """

    def __init__(self, conn, tablas_permitidas: dict[str, str | tuple[str, ...]], *, node_id: str | None = None,
                 altas=()):
        self._conn = conn
        self._tablas = {
            validar_identificador(tabla): columnas_de_clave(pk)
            for tabla, pk in tablas_permitidas.items()
        }
        self._node_id = node_id
        self._altas = {alta.tabla: alta for alta in normalizar_altas(altas)}
        if self._altas:
            if not node_id:
                raise ValueError("las altas en el nodo piden el node_id: el mapeo se reconoce por nodo")
            if TABLA_MAPEO not in self._tablas:
                raise ValueError(
                    f"las altas en el nodo piden espejar {TABLA_MAPEO}: sin el mapeo el nodo nunca se entera del id "
                    "definitivo")
            for tabla in self._altas:
                if tabla not in self._tablas:
                    raise ValueError(f"la tabla {tabla!r} tiene alta en el nodo pero el nodo no la espeja")
                if len(self._tablas[tabla]) != 1:
                    raise ValueError(f"la tabla {tabla!r} tiene alta en el nodo y una clave compuesta: no se admite")
        #: `{(tabla, id_provisorio): motivo}` de lo que no se pudo reapuntar. Vacio cuando todo esta al dia.
        self.sin_reconciliar: dict[tuple[str, int], str] = {}

    def aplicar(self, cambio: ReferenceChange) -> None:
        tabla = cambio.table_name
        if tabla not in self._tablas:
            raise PermissionError(
                f"el central mando un cambio sobre {tabla!r}, que no esta en las "
                f"tablas que este nodo espeja"
            )
        clave = self._tablas[tabla]
        marcar_aplicando(self._conn)

        if tabla == TABLA_MAPEO and self._altas:
            self._aplicar_mapeo(cambio, clave)
            return

        if cambio.operation == ReferenceOperation.DELETE:
            if len(clave) == 1:
                self._conn.execute(
                    f"DELETE FROM {tabla} WHERE {clave[0]} = ?", (cambio.row_id,))
                return
            valores = self._valores_de_clave(tabla, clave, cambio)
            condicion = " AND ".join(f"{columna} = ?" for columna in clave)
            self._conn.execute(f"DELETE FROM {tabla} WHERE {condicion}", valores)
            return

        self._upsert(tabla, clave, cambio)

    def _upsert(self, tabla: str, clave: tuple[str, ...], cambio: ReferenceChange) -> None:
        payload = cambio.payload or {}
        columnas = [validar_identificador(columna) for columna in payload]
        if not columnas:
            raise ValueError(f"upsert sin payload para {tabla}:{cambio.row_id}")
        faltan = [columna for columna in clave if columna not in payload]
        if faltan:
            raise ValueError(
                f"upsert de {tabla}:{cambio.row_id} sin las columnas de su clave: {faltan}")

        marcadores = ", ".join("?" for _ in columnas)
        asignaciones = ", ".join(
            f"{columna} = excluded.{columna}" for columna in columnas if columna not in clave
        )
        conflicto = ", ".join(clave)
        # `ON CONFLICT ... DO UPDATE` es el mismo dialecto en SQLite y en
        # PostgreSQL, incluido el pseudo-registro `excluded`.
        sql = (
            f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES ({marcadores}) "
            f"ON CONFLICT ({conflicto}) DO UPDATE SET {asignaciones}"
            if asignaciones
            else f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES ({marcadores}) "
                 f"ON CONFLICT ({conflicto}) DO NOTHING"
        )
        self._conn.execute(sql, tuple(payload[columna] for columna in columnas))

    # ── las altas del nodo (ADR-021) ────────────────────────────────────────

    def _aplicar_mapeo(self, cambio: ReferenceChange, clave: tuple[str, ...]) -> None:
        """Una fila del mapeo: la guarda y reapunta, si es de este nodo y de una tabla con alta. Si no, la ignora."""
        payload = cambio.payload or {}
        if cambio.operation == ReferenceOperation.DELETE or payload.get("node_id") != self._node_id:
            return
        if payload.get("tabla") not in self._altas:
            return
        self._upsert(TABLA_MAPEO, clave, cambio)
        self._reconciliar(payload["tabla"], int(payload["id_nodo"]), int(payload["id_central"]))

    def _reconciliar(self, tabla: str, provisorio: int, definitivo: int) -> None:
        alta = self._altas[tabla]
        try:
            with reconciliacion.savepoint(self._conn):
                resultado = reconciliacion.reapuntar(
                    self._conn, tabla, provisorio, definitivo, clave=self._tablas[tabla][0],
                    referencias=alta.referencias, espejadas=frozenset(self._tablas) - {TABLA_MAPEO})
        except Exception as exc:  # noqa: BLE001 - lo que sea que falle no puede frenar el espejo
            motivo = f"{type(exc).__name__}: {exc}"
            if self.sin_reconciliar.get((tabla, provisorio)) != motivo:
                print(f"AVISO: no se pudo reapuntar {tabla} {provisorio} -> {definitivo}: {motivo}. "
                      "Se reintenta en el proximo ciclo.", file=sys.stderr)
            self.sin_reconciliar[(tabla, provisorio)] = motivo
            return
        if resultado == reconciliacion.PENDIENTE:
            self.sin_reconciliar[(tabla, provisorio)] = f"la fila {definitivo} del central todavia no bajo"
        else:
            self.sin_reconciliar.pop((tabla, provisorio), None)

    def reconciliar_pendientes(self) -> int:
        """El barrido del final de cada ciclo: reintenta los reapuntados que no se pudieron hacer (la definitiva
        todavia no habia bajado, o fallo). Devuelve cuantos quedaron hechos. Barato: una consulta por tabla con alta,
        que sin provisorias vivas no devuelve nada. Commitea (es su propia transaccion)."""
        if not self._altas:
            return 0
        hechos = 0
        marcar_aplicando(self._conn)
        for tabla in self._altas:
            clave = self._tablas[tabla][0]
            filas = self._conn.execute(
                f"SELECT m.id_nodo, m.id_central FROM {TABLA_MAPEO} m WHERE m.node_id = ? AND m.tabla = ?"
                f" AND EXISTS (SELECT 1 FROM {tabla} t WHERE t.{clave} = m.id_nodo) ORDER BY m.id_nodo DESC",
                (self._node_id, tabla),
            ).fetchall()
            for id_nodo, id_central in ((f[0], f[1]) for f in filas):
                self._reconciliar(tabla, int(id_nodo), int(id_central))
                hechos += (tabla, int(id_nodo)) not in self.sin_reconciliar
        self._conn.commit()
        return hechos

    @staticmethod
    def _valores_de_clave(tabla, clave, cambio: ReferenceChange) -> tuple:
        """Los valores de una clave compuesta: del payload, o del `row_id`.

        El trigger y `sembrar()` arman el `row_id` de una clave compuesta como
        un arreglo JSON de textos (`'["7", "cliente"]'`); se usa solo si el
        payload no trae la fila.
        """
        payload = cambio.payload or {}
        if all(columna in payload for columna in clave):
            return tuple(payload[columna] for columna in clave)
        try:
            valores = json.loads(cambio.row_id)
        except ValueError:
            valores = None
        if not isinstance(valores, list) or len(valores) != len(clave):
            raise ValueError(
                f"delete de {tabla}:{cambio.row_id} sin los valores de su clave {list(clave)}")
        return tuple(valores)


class PullWorker:
    """Trae los cambios pendientes y los espeja, avanzando el cursor.

    Devuelve cuantos cambios aplico.
    """

    def __init__(self, repository, transport, applier):
        self.repository = repository
        self.transport = transport
        self.applier = applier

    def run_once(self, node_id: str, limit: int = 500) -> int:
        cursor = self.repository.get_server_cursor(node_id)
        cambios = self.transport.pull(cursor, limit)
        aplicados = 0
        for cambio in cambios:
            self.applier.aplicar(cambio)
            # El cursor avanza cambio por cambio y **despues** de aplicarlo: si
            # se corta la luz en el medio, lo aplicado queda y lo que falta se
            # vuelve a pedir. Ver el docstring del modulo.
            self.repository.set_server_cursor(node_id, cambio.cursor)
            aplicados += 1
        # Las altas del nodo (ADR-021): lo que no se pudo reapuntar al aplicar el mapeo (la definitiva todavia no
        # habia bajado, o fallo) se reintenta al final de cada ciclo. Un aplicador sin altas no lo tiene.
        barrer = getattr(self.applier, "reconciliar_pendientes", None)
        if barrer is not None:
            barrer()
        return aplicados


def serializar_cambio(cambio: ReferenceChange) -> dict:
    """Un `ReferenceChange` como lo manda el central por HTTP.

    🔴 **`default=str` no es un detalle de conveniencia: es lo que hace que los
    decimales viajen como TEXTO.** Un `NUMERIC` de PostgreSQL llega a Python como
    `Decimal`; si saliera como numero JSON, se degradaria a `float` en algun
    punto del camino --el `jsonable_encoder` de FastAPI lo hace-- y `19.99`
    dejaria de ser exactamente `19.99`. Ese es el precio con el que el nodo cobra
    durante un corte, asi que la perdida no seria cosmetica.

    Como texto, los dos motores lo castean exacto al escribirlo en la columna
    `NUMERIC`, que es del mismo tipo porque el nodo corre las mismas migraciones.
    Lo cubre `test_el_precio_no_pierde_precision_al_viajar`, verificado con una
    mutacion: sacando el `default=str` la suite se pone roja.

    > Hubo un `_valor_para_el_cable()` que convertia los `Decimal` a mano, con
    > este mismo comentario encima. La matriz de mutaciones mostro que sacarlo
    > dejaba la suite **verde**: `default=str` ya hacia el trabajo, y esa funcion
    > era una linea inerte defendida por un parrafo. Se retiro.
    """
    payload = cambio.payload
    if payload is not None:
        # `default=str` cubre los Decimal y, de paso, lo que tampoco es JSON
        # nativo: fechas, UUID.
        payload = json.loads(json.dumps(payload, default=str))
    return {
        "cursor": cambio.cursor,
        "table_name": cambio.table_name,
        "row_id": cambio.row_id,
        "operation": str(cambio.operation),
        "payload": payload,
        "recorded_at": cambio.recorded_at,
    }
