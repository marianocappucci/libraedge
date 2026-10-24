"""El ticket de ARCA prestado por el central (ADR-009).

Un nodo factura con internet sin tener la clave de ARCA: el central --que tiene el
certificado y la clave en su disco-- le **presta** el ticket de acceso (WSAA) que
usa él. Copiar la clave al nodo no serviría aunque se quisiera: ARCA da un solo
ticket a la vez por certificado y servicio, así que el nodo no podría pedir uno
propio mientras el del central vale (``alreadyAuthenticated``).

LibraEdge no sabe de ARCA. Pone el transporte:

- **En el central**, ``POST /sync/v1/ticket-arca`` en ``create_sync_router(...,
  prestar_ticket=...)``. ``prestar_ticket(emisor_id, servicio=)`` la aporta el producto
  (normalmente ``libracore.nodo_offline.ta_para_el_nodo``); puede ser ``async``.
- **En el nodo**, :class:`ClienteDeTicket`. ``pedir_ticket(emisor_id, servicio)`` trae el
  ticket y lo guarda **sólo en memoria** hasta poco antes de que venza;
  :meth:`ClienteDeTicket.para_servicio` lo adapta a la forma de ``obtener_ta`` que
  esperan ``libracore.nodo_offline.ticket_prestado`` y ``libracore.cola_arca.ColaDeArca``
  (``obtener_ta(arca, servicio=)``), y :meth:`ClienteDeTicket.para_numerador` a la de
  ``numerador_con_ta`` (``obtener_ta(arca)``, siempre ``wsfe``).

**El servicio (v0.22, ADR-025).** ARCA da un ticket por certificado *y por servicio*: el de
facturación (``wsfe``) y el de la carta de porte (``wscpe``) son distintos y se piden con pares
de certificado distintos. El pedido trae ``servicio`` (opcional, ``wsfe`` por omisión) y el
ticket que vuelve dice de cuál es (``servicio``) y en qué ``ambiente``. LibraEdge no sabe qué
servicios existen: el que decide cuáles se prestan es el producto (``ValueError`` -> 501
``ticket_no_prestado``).

🔴 **El ticket es una credencial.** Firma comprobantes fiscales en nombre del CUIT
durante hasta 12 horas. Nunca se escribe en un log, en un archivo ni en el mensaje
de una excepción; el central lo contesta con ``Cache-Control: no-store``.

Errores, del lado del nodo:

- :class:`SinConexionConElCentral`: no hubo respuesta, o el central contestó 5xx
  (ARCA no le respondió a él). Se arregla solo: el producto la trata como
  ``SinConexion`` y la venta va a la bandeja.
- :class:`TicketRechazado`: el central contestó, pero no presta (401 nodo
  revocado, 422 sin ARCA para ese emisor, 501 central sin la costura). No se
  arregla solo: lo tiene que mirar alguien.
- ``EsquemaIncompatible`` (409): el nodo no está en la versión del central.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from libraedge.sync import esquema

logger = logging.getLogger(__name__)

#: La ruta del central.
RUTA = "/sync/v1/ticket-arca"

#: Cuánto antes del vencimiento el nodo deja de usar el ticket guardado y pide otro.
MARGEN = timedelta(minutes=10)

#: El servicio de siempre: la facturación electrónica. Es el que se pide cuando no se dice otro.
SERVICIO_FACTURACION = "wsfe"

#: La forma de un nombre de servicio en el pedido: minúsculas, dígitos y guion bajo (``wsfe``, ``wscpe``). Se valida
#: porque viaja en el cuerpo, se loguea y llega a la costura del producto.
_SERVICIO = re.compile(r"[a-z][a-z0-9_]{0,31}")

#: ``codigo`` de los errores que arma el central (``detail.codigo``).
CODIGO_SIN_COSTURA = "ticket_no_prestado"
#: El ticket que volvió no es del servicio pedido (un central viejo ignora ``servicio`` y manda el de ``wsfe``).
CODIGO_OTRO_SERVICIO = "ticket_de_otro_servicio"
CODIGO_NO_DISPONIBLE = "ticket_no_disponible"
CODIGO_ARCA_NO_RESPONDE = "arca_no_responde"
CODIGO_SIN_CONEXION = "sin_conexion"
CODIGO_RESPUESTA_INVALIDA = "respuesta_invalida"

VersionDelNodo = str | Callable[[], str | None] | None


class ErrorDeTicket(RuntimeError):
    """No hay ticket. ``codigo`` y ``mensaje`` son para mostrar; nunca traen el ticket."""

    def __init__(self, mensaje: str, *, codigo: str, status: int | None = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.codigo = codigo
        self.status = status


class SinConexionConElCentral(ErrorDeTicket):
    """El central no respondió, o respondió que ARCA no le respondió a él. Se arregla solo."""


class TicketRechazado(ErrorDeTicket):
    """El central respondió y no presta el ticket. No se arregla solo.

    Lleva ``rechazo_del_central = True``: es la marca con la que el motor
    (``libracore.nodo_offline.es_rechazo``) distingue un rechazo de un corte, así que
    ``ticket_prestado`` la clasifica como ``RechazoDelCentral`` aunque el producto no la traduzca."""

    rechazo_del_central = True


def servicio_valido(servicio) -> str:
    """El nombre del servicio normalizado (``" WSCPE "`` -> ``"wscpe"``). ``None`` es el de siempre, ``wsfe``.

    Levanta ``ValueError`` si no es un texto con la forma de un nombre de servicio."""
    if servicio is None:
        return SERVICIO_FACTURACION
    if not isinstance(servicio, str):
        raise ValueError("servicio invalido")
    limpio = servicio.strip().lower()
    if not _SERVICIO.fullmatch(limpio):
        raise ValueError("servicio invalido")
    return limpio


def vencimiento(ticket: dict) -> datetime | None:
    """La ``expiracion`` del ticket como fecha con zona, o ``None`` si falta, no se entiende o no tiene zona."""
    valor = ticket.get("expiracion")
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        fecha = datetime.fromisoformat(valor.strip())
    except ValueError:
        return None
    # Sin zona no se sabe cuándo vence de verdad (ARCA la manda siempre): no se guarda.
    return fecha if fecha.tzinfo else None


def _detalle(exc: HTTPError) -> dict:
    try:
        cuerpo = json.loads(exc.read() or b"{}")
    except (ValueError, OSError):
        return {}
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    return detalle if isinstance(detalle, dict) else {}


class ClienteDeTicket:
    """El cliente del nodo para ``POST /sync/v1/ticket-arca``.

    - ``base_url``, ``node_id``, ``node_secret``: los mismos de la subida y la
      bajada (``LIBRAEDGE_CENTRAL_URL``, ``LIBRAEDGE_NODE_ID``,
      ``LIBRAEDGE_NODE_SECRET``); ver :func:`cliente_desde_entorno`.
    - ``version_esquema``: texto o ``() -> str``; la misma que manda el proceso
      del nodo, para que el central conteste 409 si no coinciden.
    - ``margen``: el ticket guardado se deja de usar ``margen`` antes de vencer.

    Es seguro entre hilos. La caché es por ``(emisor_id, servicio)`` y vive lo que
    vive el objeto: un proceso nuevo pide el ticket de nuevo. Es por servicio porque
    ARCA da un ticket distinto para cada uno: servirle al que pide ``wscpe`` el de
    ``wsfe`` que quedó guardado le haría llamar a ARCA con el ticket equivocado.
    """

    def __init__(self, base_url: str, node_id: str, node_secret: str, *, timeout: float = 10.0,
                 version_esquema: VersionDelNodo = None, margen: timedelta = MARGEN,
                 reloj: Callable[[], datetime] | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self._secreto = node_secret
        self.timeout = timeout
        self.version_esquema = version_esquema
        self.margen = margen
        self._reloj = reloj or (lambda: datetime.now(UTC))
        self._cache: dict[tuple[object, str], tuple[dict, datetime]] = {}
        self._candado = threading.Lock()

    def __repr__(self) -> str:  # sin el secreto ni los tickets
        return f"ClienteDeTicket({self.base_url!r}, node_id={self.node_id!r})"

    def _version(self) -> str | None:
        if callable(self.version_esquema):
            return self.version_esquema()
        return self.version_esquema

    def olvidar(self, emisor_id=None, servicio: str = SERVICIO_FACTURACION) -> None:
        """Descarta el ticket guardado de ese emisor y servicio (p. ej. si ARCA lo rechazó).

        Sin ``servicio`` es el de ``wsfe``, como siempre: los de otros servicios se olvidan por separado."""
        with self._candado:
            self._cache.pop((emisor_id, servicio_valido(servicio)), None)

    def pedir_ticket(self, emisor_id=None, servicio: str = SERVICIO_FACTURACION) -> dict:
        """El ticket vigente del emisor y servicio: ``{"token", "sign", "expiracion", "servicio", "ambiente", ...}``,
        de la caché o del central.

        ``servicio`` es ``wsfe`` (facturación) si no se dice otro; ``wscpe`` es el de la carta de porte, que no
        depende de un emisor (``emisor_id=None``). Levanta :class:`SinConexionConElCentral`, :class:`TicketRechazado`
        o ``EsquemaIncompatible``. Bloquea (``urllib``): desde código ``async``, usar :meth:`pedir_ticket_async`."""
        servicio = servicio_valido(servicio)
        clave = (emisor_id, servicio)
        with self._candado:
            guardado = self._cache.get(clave)
            if guardado is not None:
                ticket, hasta = guardado
                if self._reloj() < hasta:
                    return dict(ticket)
                del self._cache[clave]
            ticket = self._traer(emisor_id, servicio)
            vence = vencimiento(ticket)
            if vence is not None and self._reloj() < vence - self.margen:
                self._cache[clave] = (dict(ticket), vence - self.margen)
            return ticket

    async def pedir_ticket_async(self, emisor_id=None, servicio: str = SERVICIO_FACTURACION) -> dict:
        """:meth:`pedir_ticket` en un hilo aparte, para no frenar el loop del producto."""
        return await asyncio.to_thread(self.pedir_ticket, emisor_id, servicio)

    def para_servicio(self, *, sin_conexion: Callable[[str], Exception] | None = None,
                      rechazo: Callable[[str], Exception] | None = None) -> Callable[..., Awaitable[dict]]:
        """El ``obtener_ta(arca, servicio=)`` de ``libracore.nodo_offline.ticket_prestado`` y de ``ColaDeArca``.

        Pide el ticket del emisor de esa configuración (``arca["id"]``, la misma fila espejada del central) y del
        ``servicio`` que se pida (``wsfe`` si no se dice: el motor llama ``obtener_ta(arca)`` para la facturación y
        ``obtener_ta(arca, servicio="wscpe")`` para los demás). Con ``sin_conexion`` (p. ej. la ``SinConexion`` del
        motor), :class:`SinConexionConElCentral` y el 409 de esquema se levantan como esa excepción, con un mensaje
        para la pantalla. Con ``rechazo`` (p. ej. la ``RechazoDelCentral`` del motor), :class:`TicketRechazado` se
        levanta como esa excepción **con el mensaje del central** (``El central no tiene el certificado y la clave de
        wscpe cargados``) en vez de uno genérico. Sin ``rechazo`` sale tal cual: lleva ``rechazo_del_central = True`` y
        el motor la trata igual como el rechazo que es (``RechazoDelCentral``, ``NodoSinArca`` 422), no como un corte::

            cola = ColaDeArca(..., obtener_ta=cliente.para_servicio(sin_conexion=SinConexion,
                                                                    rechazo=RechazoDelCentral))
        """

        async def obtener_ta(arca: dict, servicio: str = SERVICIO_FACTURACION) -> dict:
            emisor_id = (arca or {}).get("id")
            try:
                return await self.pedir_ticket_async(emisor_id, servicio)
            except (SinConexionConElCentral, esquema.EsquemaIncompatible) as exc:
                if sin_conexion is None:
                    raise
                raise sin_conexion(
                    "Sin conexión con el central: se emite al volver la conexión."
                ) from exc
            except TicketRechazado as exc:
                if rechazo is None:
                    raise
                raise rechazo(exc.mensaje) from exc

        return obtener_ta

    def para_numerador(self, *, sin_conexion: Callable[[str], Exception] | None = None
                       ) -> Callable[..., Awaitable[dict]]:
        """El ``obtener_ta(arca)`` de ``libracore.nodo_offline.numerador_con_ta``.

        Es :meth:`para_servicio`, que sirve igual con un solo parámetro (pide ``wsfe``). Se conserva por los
        productos que ya lo usan::

            numerador_con_ta(cliente.para_numerador(sin_conexion=SinConexion), emisor_id)
        """
        return self.para_servicio(sin_conexion=sin_conexion)

    def _traer(self, emisor_id, servicio: str = SERVICIO_FACTURACION) -> dict:
        cuerpo = json.dumps({"node_id": self.node_id, "emisor_id": emisor_id, "servicio": servicio}).encode("utf-8")
        pedido = Request(
            f"{self.base_url}{RUTA}", data=cuerpo, method="POST",
            headers={"Content-Type": "application/json",
                     **esquema.cabeceras_del_nodo(self._secreto, self._version())},
        )
        try:
            with urlopen(pedido, timeout=self.timeout) as respuesta:
                crudo = respuesta.read()
        except HTTPError as exc:
            raise self._error_http(exc) from None
        except (URLError, TimeoutError, OSError) as exc:
            logger.warning("Ticket de ARCA: el central no respondió (%s)", type(exc).__name__)
            raise SinConexionConElCentral(
                "El central no respondió.", codigo=CODIGO_SIN_CONEXION) from None
        try:
            ticket = json.loads(crudo)
        except ValueError:
            ticket = None
        if not (isinstance(ticket, dict) and isinstance(ticket.get("token"), str) and ticket["token"]
                and isinstance(ticket.get("sign"), str) and ticket["sign"]):
            raise TicketRechazado("El central respondió sin un ticket válido.",
                                  codigo=CODIGO_RESPUESTA_INVALIDA)
        # Sin `servicio` es el de `wsfe`: un central anterior a v0.22 ignora el pedido y no lo dice. Para cualquier otro
        # servicio eso es un ticket equivocado, y no se guarda ni se devuelve: ARCA contestaría con un error de
        # autenticación que no explica nada.
        dado = str(ticket.get("servicio") or SERVICIO_FACTURACION).strip().lower()[:32]
        if dado != servicio:
            raise TicketRechazado(
                f"El central devolvió un ticket de {dado} y hace falta uno de {servicio}: actualizá LibraEdge en el "
                "central.", codigo=CODIGO_OTRO_SERVICIO)
        return ticket

    def _error_http(self, exc: HTTPError) -> Exception:
        if exc.code == 409:
            incompatible = esquema.desde_error_http(exc)
            if incompatible is not None:
                return incompatible
        detalle = _detalle(exc)
        codigo = str(detalle.get("codigo") or f"http_{exc.code}")
        mensaje = str(detalle.get("mensaje") or f"El central contestó {exc.code} al pedir el ticket de ARCA.")
        logger.warning("Ticket de ARCA: el central contestó %s (%s)", exc.code, codigo)
        if exc.code >= 500 and exc.code != 501:
            return SinConexionConElCentral(mensaje, codigo=codigo, status=exc.code)
        return TicketRechazado(mensaje, codigo=codigo, status=exc.code)


def cliente_desde_entorno(*, version_esquema: VersionDelNodo = None, timeout: float = 10.0) -> ClienteDeTicket:
    """El cliente con ``LIBRAEDGE_CENTRAL_URL``, ``LIBRAEDGE_NODE_ID`` y ``LIBRAEDGE_NODE_SECRET``.

    Levanta ``RuntimeError`` si falta alguna: fuera de un nodo no hay a quién pedirle."""
    faltan = [v for v in ("LIBRAEDGE_CENTRAL_URL", "LIBRAEDGE_NODE_ID", "LIBRAEDGE_NODE_SECRET")
              if not (os.environ.get(v) or "").strip()]
    if faltan:
        raise RuntimeError(f"Faltan {', '.join(faltan)}: esta instancia no puede pedir el ticket al central.")
    return ClienteDeTicket(
        os.environ["LIBRAEDGE_CENTRAL_URL"].strip(), os.environ["LIBRAEDGE_NODE_ID"].strip(),
        os.environ["LIBRAEDGE_NODE_SECRET"].strip(), timeout=timeout, version_esquema=version_esquema,
    )
