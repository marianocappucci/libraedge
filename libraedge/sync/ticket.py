"""El ticket de ARCA prestado por el central (ADR-009).

Un nodo factura con internet sin tener la clave de ARCA: el central --que tiene el
certificado y la clave en su disco-- le **presta** el ticket de acceso (WSAA) que
usa él. Copiar la clave al nodo no serviría aunque se quisiera: ARCA da un solo
ticket a la vez por certificado y servicio, así que el nodo no podría pedir uno
propio mientras el del central vale (``alreadyAuthenticated``).

LibraEdge no sabe de ARCA. Pone el transporte:

- **En el central**, ``POST /sync/v1/ticket-arca`` en ``create_sync_router(...,
  prestar_ticket=...)``. ``prestar_ticket(emisor_id)`` la aporta el producto
  (normalmente ``libracore.nodo_offline.ta_para_el_nodo``); puede ser ``async``.
- **En el nodo**, :class:`ClienteDeTicket`. ``pedir_ticket(emisor_id)`` trae el
  ticket y lo guarda **sólo en memoria** hasta poco antes de que venza;
  :meth:`ClienteDeTicket.para_numerador` lo adapta a la forma de ``obtener_ta`` que
  espera ``libracore.nodo_offline.numerador_con_ta``.

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

#: ``codigo`` de los errores que arma el central (``detail.codigo``).
CODIGO_SIN_COSTURA = "ticket_no_prestado"
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
    """El central respondió y no presta el ticket. No se arregla solo."""


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

    Es seguro entre hilos. La caché es por ``emisor_id`` y vive lo que vive el
    objeto: un proceso nuevo pide el ticket de nuevo.
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
        self._cache: dict[object, tuple[dict, datetime]] = {}
        self._candado = threading.Lock()

    def __repr__(self) -> str:  # sin el secreto ni los tickets
        return f"ClienteDeTicket({self.base_url!r}, node_id={self.node_id!r})"

    def _version(self) -> str | None:
        if callable(self.version_esquema):
            return self.version_esquema()
        return self.version_esquema

    def olvidar(self, emisor_id=None) -> None:
        """Descarta el ticket guardado de ese emisor (p. ej. si ARCA lo rechazó)."""
        with self._candado:
            self._cache.pop(emisor_id, None)

    def pedir_ticket(self, emisor_id=None) -> dict:
        """El ticket vigente del emisor: ``{"token", "sign", "expiracion", ...}``, de la caché o del central.

        Levanta :class:`SinConexionConElCentral`, :class:`TicketRechazado` o
        ``EsquemaIncompatible``. Bloquea (``urllib``): desde código ``async``,
        usar :meth:`pedir_ticket_async`."""
        with self._candado:
            guardado = self._cache.get(emisor_id)
            if guardado is not None:
                ticket, hasta = guardado
                if self._reloj() < hasta:
                    return dict(ticket)
                del self._cache[emisor_id]
            ticket = self._traer(emisor_id)
            vence = vencimiento(ticket)
            if vence is not None and self._reloj() < vence - self.margen:
                self._cache[emisor_id] = (dict(ticket), vence - self.margen)
            return ticket

    async def pedir_ticket_async(self, emisor_id=None) -> dict:
        """:meth:`pedir_ticket` en un hilo aparte, para no frenar el loop del producto."""
        return await asyncio.to_thread(self.pedir_ticket, emisor_id)

    def para_numerador(self, *, sin_conexion: Callable[[str], Exception] | None = None
                       ) -> Callable[[dict], Awaitable[dict]]:
        """El ``obtener_ta(arca)`` de ``libracore.nodo_offline.numerador_con_ta``.

        Pide el ticket del emisor de esa configuración (``arca["id"]``, la misma
        fila espejada del central). Con ``sin_conexion`` (p. ej. la
        ``SinConexion`` del motor), :class:`SinConexionConElCentral` y el 409 de
        esquema se levantan como esa excepción, con un mensaje para la pantalla::

            numerador_con_ta(cliente.para_numerador(sin_conexion=SinConexion), emisor_id)
        """

        async def obtener_ta(arca: dict) -> dict:
            emisor_id = (arca or {}).get("id")
            try:
                return await self.pedir_ticket_async(emisor_id)
            except (SinConexionConElCentral, esquema.EsquemaIncompatible) as exc:
                if sin_conexion is None:
                    raise
                raise sin_conexion(
                    "Sin conexión con el central: la venta se factura al volver la conexión."
                ) from exc

        return obtener_ta

    def _traer(self, emisor_id) -> dict:
        cuerpo = json.dumps({"node_id": self.node_id, "emisor_id": emisor_id}).encode("utf-8")
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
