"""La carga inicial de existencias: al vincularse, el nodo trae las de SU sucursal del central, una sola vez (ADR-023).

Un nodo recien vinculado arranca con el stock en cero: el espejo trae el catalogo y los depositos, pero las existencias
son de autoridad del nodo (las mueven sus ventas y sus recepciones) y no bajan por el changelog. La decision del humano
(2026-10-21) es que **al vincular el nodo trae las existencias de su sucursal del central y desde ahi manda el nodo**.

LibraEdge no sabe que es una existencia. Pone el transporte y el "una sola vez":

- **En el central**, ``GET /sync/v1/carga-inicial?node_id=`` en ``create_sync_router(..., carga_inicial=...)``.
  ``carga_inicial(conexion, node_id, branch_id) -> dict`` la aporta el producto (en la familia,
  ``erp.offline_stock.carga_inicial_del_central`` de LibraCommerce). La sucursal sale de ``node_identity.branch_id`` del
  nodo **autenticado**: el nodo no la elige, asi que no puede pedir las existencias de otra.
- **En el nodo**, :class:`CargaInicialWorker`. Corre en cada ciclo de ``Nodo.sincronizar``, despues de la bajada,
  mientras falte la marca ``carga_inicial:<node_id>`` en ``local_sequences``. Llama a la funcion del producto que dice
  ``LIBRAEDGE_CARGA_INICIAL=modulo:funcion`` con ``(conexion, traer)``; ``traer()`` hace el GET y devuelve el dict del
  central. En la familia, ``erp.offline_stock:aplicar_carga_inicial`` de LibraCommerce.

## Lo que devuelve la funcion del producto

Un ``dict``. Con ``"hecha": False`` (y un ``"motivo"``) dice «todavia no puedo» -- por ejemplo, el espejo no trajo
aun los depositos de la sucursal--: el worker **no marca** y vuelve a llamarla en el ciclo siguiente. Cualquier otra
cosa (sin ``hecha``, o ``hecha`` verdadera) es «lo hice»: el worker marca.

La funcion **no necesita commitear**. Si no lo hace, el worker commitea lo que la funcion escribio **junto con la
marca**, en una sola transaccion: no hay forma de que quede aplicada y sin marca (se aplicaria dos veces, pisando el
stock que el nodo ya movio) ni marcada y sin aplicar (el nodo nunca tendria sus existencias). Si la funcion levanta una
excepcion o dice que espera, el worker hace ``rollback``.

## Errores: el nodo no se cae

Todo error queda en el estado del nodo (``EstadoNodo.carga_inicial``) y se reintenta en el ciclo siguiente, sin tocar
``en_linea`` ni cortar la subida de las ventas: la carga inicial es un paso aparte de la sincronizacion.

- El modulo o la funcion no existen (el producto instalado es anterior a la carga inicial): error claro, no se marca.
- El central contesta **501** (el producto del central no pasa ``carga_inicial``): no hay nada que traer. Se marca como
  hecha y se loguea, una vez. **Un 404 no es un 501**: un central anterior a v0.20 no tiene la ruta, y eso se arregla
  actualizandolo; marcarlo como hecho dejaria al nodo sin sus existencias para siempre.
- 401, 409 de esquema, red caida o cualquier otra: error, se reintenta.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from libraedge.db.changelog import cargar_payload
from libraedge.sync import esquema

logger = logging.getLogger(__name__)

#: La ruta del central.
RUTA = "/sync/v1/carga-inicial"

#: La variable del nodo con la funcion del producto, ``modulo:funcion``.
VARIABLE = "LIBRAEDGE_CARGA_INICIAL"

#: ``detail.codigo`` del 501 del central.
CODIGO_SIN_COSTURA = "carga_inicial_no_disponible"

#: Los estados de un intento (:attr:`ResultadoCargaInicial.estado`).
HECHA = "hecha"
YA_HECHA = "ya_hecha"
NO_DISPONIBLE = "no_disponible"
ESPERA = "espera"
ERROR = "error"


def marca_de(node_id: str) -> str:
    """El nombre de la marca en ``local_sequences`` que dice «este nodo ya trajo sus existencias»."""
    return f"carga_inicial:{node_id}"


class CargaInicialNoDisponible(RuntimeError):
    """El central contesto 501: el producto del central no tiene carga inicial. No hay nada que traer."""


class CargaInicialMalConfigurada(RuntimeError):
    """``LIBRAEDGE_CARGA_INICIAL`` no se entiende o no se puede importar. El mensaje dice que pasa y que hacer."""


# ── el transporte ───────────────────────────────────────────────────────────


class HttpCargaInicialTransport:
    """Trae la carga inicial del central. Misma autenticacion por nodo que la subida y la bajada."""

    def __init__(self, base_url: str, node_id: str, node_secret: str, timeout: float = 30.0,
                 version_esquema: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.node_secret = node_secret
        #: Mas holgado que el de la bajada (10 s): es una sola respuesta con todas las existencias de la sucursal.
        self.timeout = timeout
        self.version_esquema = version_esquema

    def __repr__(self) -> str:  # sin el secreto
        return f"HttpCargaInicialTransport({self.base_url!r}, node_id={self.node_id!r})"

    def traer(self) -> dict:
        """El ``dict`` que arma el producto del central para la sucursal de este nodo.

        Levanta :class:`CargaInicialNoDisponible` si el central contesta 501, ``EsquemaIncompatible`` si el esquema no
        es el del central, y el ``HTTPError`` / ``URLError`` de ``urllib`` en cualquier otro caso."""
        consulta = urlencode({"node_id": self.node_id})
        pedido = Request(
            f"{self.base_url}{RUTA}?{consulta}",
            headers=esquema.cabeceras_del_nodo(self.node_secret, self.version_esquema),
            method="GET",
        )
        try:
            with urlopen(pedido, timeout=self.timeout) as respuesta:
                crudo = respuesta.read()
        except HTTPError as exc:
            esquema.relanzar_si_es_de_esquema(exc)
            if exc.code == 501:
                raise CargaInicialNoDisponible(
                    "el central no tiene carga inicial de existencias para este producto") from None
            raise
        # Sin `parse_float=Decimal` una cantidad con decimales se degradaria a float antes de llegar al producto.
        datos = cargar_payload(crudo.decode("utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("el central no contesto la carga inicial (se esperaba un objeto JSON)")
        return datos


# ── la funcion del producto ─────────────────────────────────────────────────

FuncionDelProducto = Callable[[object, Callable[[], dict]], "Mapping | None"]


def resolver_funcion(especificacion: str | None) -> FuncionDelProducto:
    """``"modulo:funcion"`` -> la funcion. Levanta :class:`CargaInicialMalConfigurada` con un mensaje util.

    Se importa **cuando hace falta** y no al arrancar el nodo: un producto instalado con un motor anterior (sin el
    modulo) no puede tirar abajo el servicio que sube las ventas."""
    texto = (especificacion or "").strip()
    modulo, _, nombre = texto.partition(":")
    if not modulo or not nombre or ":" in nombre:
        raise CargaInicialMalConfigurada(
            f"{VARIABLE}={texto!r} no se entiende: usar 'modulo:funcion'.")
    try:
        funcion = getattr(import_module(modulo), nombre)
    except ImportError as exc:
        raise CargaInicialMalConfigurada(
            f"{VARIABLE}={texto}: no se pudo importar el modulo ({type(exc).__name__}: {exc}). El producto instalado "
            "todavia no trae la carga inicial de existencias: actualizar el nodo. Mientras tanto sigue sincronizando.",
        ) from None
    except AttributeError:
        raise CargaInicialMalConfigurada(
            f"{VARIABLE}={texto}: el modulo {modulo!r} no tiene {nombre!r}. El producto instalado todavia no trae la "
            "carga inicial de existencias: actualizar el nodo. Mientras tanto sigue sincronizando.") from None
    if not callable(funcion):
        raise CargaInicialMalConfigurada(f"{VARIABLE}={texto}: {nombre!r} no es una funcion.")
    return funcion


# ── el worker ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResultadoCargaInicial:
    """Que paso en un intento. ``resultado`` es lo que devolvio la funcion del producto (para soporte)."""

    estado: str
    detalle: str | None = None
    resultado: Mapping | None = None

    @property
    def terminada(self) -> bool:
        """Si la carga inicial ya no hay que volver a intentarla (hecha ahora, antes, o sin nada que traer)."""
        return self.estado in (HECHA, YA_HECHA, NO_DISPONIBLE)

    def para_el_estado(self) -> str | None:
        """Lo que va a ``EstadoNodo.carga_inicial``: ``None`` si no hay nada que decir (esta hecha)."""
        if self.estado == ESPERA:
            return f"esperando: {self.detalle}"
        if self.estado == ERROR:
            return f"error: {self.detalle}"
        return None


class CargaInicialWorker:
    """Corre la funcion del producto una vez por nodo, y deja la marca.

    ``especificacion`` es ``modulo:funcion`` (``LIBRAEDGE_CARGA_INICIAL``); ``transporte`` tiene ``traer() -> dict``
    (:class:`HttpCargaInicialTransport`); ``conexion`` es la del producto en el nodo, la misma del espejo, que se le
    pasa a la funcion. :meth:`run_once` **no levanta**: todo problema vuelve como ``ResultadoCargaInicial``."""

    def __init__(self, repository, conexion, node_id: str, transporte, especificacion: str | None):
        self.repository = repository
        self.conexion = conexion
        self.node_id = node_id
        self.transporte = transporte
        self.especificacion = especificacion

    def run_once(self, forzar: bool = False, bajada_completa: bool = True) -> ResultadoCargaInicial:
        """Un intento. ``forzar`` borra la marca y corre ya (soporte: ``libraedge-nodo carga-inicial --forzar``).

        ``bajada_completa=False`` es la bajada de este ciclo que lleno su tanda (puede quedar espejo por bajar): la
        funcion del producto lee del espejo y todavia no se la llama. Lo pasa ``Nodo.sincronizar``."""
        try:
            return self._intentar(forzar, bajada_completa)
        except Exception as exc:  # noqa: BLE001 - un fallo de la carga inicial no puede frenar la subida de ventas
            self._deshacer()
            return ResultadoCargaInicial(ERROR, f"{type(exc).__name__}: {exc}")

    def _deshacer(self) -> None:
        try:
            self.conexion.rollback()
        except Exception:  # noqa: BLE001 - la conexion puede estar cerrada; el error que importa es el de antes
            pass

    def _intentar(self, forzar: bool, bajada_completa: bool) -> ResultadoCargaInicial:
        marca = marca_de(self.node_id)
        if forzar:
            self.repository.quitar_marca(marca)
        elif self.repository.tiene_marca(marca):
            self.conexion.commit()  # la lectura abre una transaccion en PostgreSQL: no se la deja abierta
            return ResultadoCargaInicial(YA_HECHA)
        if not bajada_completa:
            self.conexion.commit()
            return ResultadoCargaInicial(ESPERA, "la bajada del espejo todavia no termino")
        try:
            funcion = resolver_funcion(self.especificacion)
        except CargaInicialMalConfigurada as exc:
            return ResultadoCargaInicial(ERROR, str(exc))

        try:
            resultado = funcion(self.conexion, self.transporte.traer)
        except CargaInicialNoDisponible as exc:
            self._deshacer()
            # `warning` y no `info`: sin `logging` configurado solo salen los warnings, y esto pasa UNA vez por nodo.
            logger.warning("Carga inicial de %s: %s. Se da por hecha (no hay nada que traer).", self.node_id, exc)
            self.repository.poner_marca(marca)
            return ResultadoCargaInicial(NO_DISPONIBLE, str(exc))
        except Exception as exc:  # noqa: BLE001 - lo que sea que falle se reintenta en el ciclo siguiente
            self._deshacer()
            return ResultadoCargaInicial(ERROR, f"{type(exc).__name__}: {exc}")

        if isinstance(resultado, Mapping) and not resultado.get("hecha", True):
            self._deshacer()
            return ResultadoCargaInicial(
                ESPERA, str(resultado.get("motivo") or "la funcion del producto pidio esperar"), resultado)

        # La marca entra en la misma transaccion que lo que la funcion escribio (ver el docstring del modulo).
        self.repository.poner_marca(marca, commit=False)
        self.conexion.commit()
        logger.info("Carga inicial de %s: hecha.", self.node_id)
        return ResultadoCargaInicial(HECHA, resultado=resultado if isinstance(resultado, Mapping) else None)
