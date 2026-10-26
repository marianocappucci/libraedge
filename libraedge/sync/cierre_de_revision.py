"""Que el nodo se entere solo de que una operacion en revision ya se resolvio en el central (v0.25, ADR-028).

Cuando el central rechaza una operacion, queda en ``sync_inbox`` como ``manual_review`` y el nodo la marca igual en su
outbox. Alguien la resuelve en el central (``libraedge.sync.revision``): ``reintentar`` la deja ``applied``,
``descartar`` la deja ``discarded``. **El central no tiene ruta hacia el nodo** (ADR-024), asi que hasta v0.22 la copia
del nodo seguia en ``manual_review`` hasta que alguien corria ``libraedge-nodo revision reenviar <id>`` en esa PC.

## Como se entera

- **En el central**, ``POST /sync/v1/estado-operaciones`` (``create_sync_router``): el nodo manda una tanda de
  ``operation_id`` y el central contesta el estado que tiene de cada una en ``sync_inbox``
  (``libraedge.sync.revision.estados_de_operaciones``). **Solo lee**: no aplica nada ni llama a ningun manejador.
- **En el nodo**, :class:`CierreDeRevisionWorker`, un paso mas de ``Nodo.sincronizar`` (despues de subir y de bajar):
  cada ``cada_segundos`` (300 por defecto) toma una tanda de las que tiene en ``manual_review`` y las pregunta.
  Segun lo que conteste el central:

  - ``applied``: la aplicaron. El nodo la **reenvia** (``reenviar_operacion``, la misma funcion que ``revision
    reenviar``) y en el ciclo siguiente el push contesta ``duplicate`` y la confirma por el camino de siempre.
  - ``discarded``: la descartaron. El nodo la descarta (``descartar_operacion``, ADR-024) con el motivo del central.
  - ``manual_review``, o el central no sabe de ella: **se queda como esta**.

Por que no se reusa el push para preguntar esta en ``DECISIONS.md`` (ADR-028).

## Lo que no puede pasar

- **No frena la subida.** Va despues de subir y de bajar, solo si el ciclo no tuvo falla de transporte, y no levanta:
  todo problema vuelve como :class:`ResultadoCierreDeRevision`.
- **No hay tormenta.** Una tanda por ventana (``cada_segundos``), de a lo sumo ``tanda`` ids, y las tandas rotan
  (``ids_en_revision(despues_de=)``) para que, con mas en revision que una tanda, se recorran todas. Un error o un 404
  tambien consumen la ventana: no se reintenta en el ciclo siguiente.
- **Un central anterior a v0.25** contesta 404: el nodo sigue como hasta ahora (a mano con ``revision reenviar``) y
  vuelve a probar en la ventana siguiente, asi que cuando se actualice el central lo detecta solo.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from libraedge.sync import esquema

logger = logging.getLogger(__name__)

#: La ruta del central.
RUTA = "/sync/v1/estado-operaciones"

#: Cada cuanto se pregunta (segundos). ``0`` apaga la consulta.
CADA_SEGUNDOS_POR_DEFECTO = 300
#: Cuantas operaciones se preguntan por vez. Tiene que ser <= ``revision.MAXIMO_POR_CONSULTA`` del central.
TANDA_POR_DEFECTO = 50

#: Las variables del nodo.
VARIABLE_CADA_SEGUNDOS = "LIBRAEDGE_REVISION_CADA_SEGUNDOS"
VARIABLE_TANDA = "LIBRAEDGE_REVISION_TANDA"

#: Los estados de un intento (:attr:`ResultadoCierreDeRevision.estado`).
APAGADA = "apagada"
NO_TOCA = "no_toca"
SIN_NADA = "sin_nada"
CONSULTADA = "consultada"
CENTRAL_SIN_CONSULTA = "central_sin_consulta"
ERROR = "error"


class CentralSinConsultaDeEstado(RuntimeError):
    """El central contesto 404: es anterior a v0.25 y no tiene la ruta. No es una falla: el nodo sigue como siempre."""


# ── el transporte ───────────────────────────────────────────────────────────


class HttpCierreDeRevisionTransport:
    """Le pregunta al central el estado de unas operaciones. Misma autenticacion por nodo que la subida y la bajada."""

    def __init__(self, base_url: str, node_id: str, node_secret: str, timeout: float = 10.0,
                 version_esquema: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.node_secret = node_secret
        self.timeout = timeout
        self.version_esquema = version_esquema

    def __repr__(self) -> str:  # sin el secreto
        return f"HttpCierreDeRevisionTransport({self.base_url!r}, node_id={self.node_id!r})"

    def consultar(self, operation_ids: list[str]) -> dict[str, dict]:
        """``{operation_id: {"estado", "motivo"}}`` de lo que el central conoce. Lo que no conoce, no figura.

        Levanta :class:`CentralSinConsultaDeEstado` si el central contesta 404, ``EsquemaIncompatible`` si el esquema
        no es el del central, y el ``HTTPError`` / ``URLError`` de ``urllib`` en cualquier otro caso."""
        cuerpo = json.dumps({"node_id": self.node_id, "operation_ids": list(operation_ids)}).encode("utf-8")
        pedido = Request(
            f"{self.base_url}{RUTA}", data=cuerpo,
            headers={"Content-Type": "application/json",
                     **esquema.cabeceras_del_nodo(self.node_secret, self.version_esquema)},
            method="POST",
        )
        try:
            with urlopen(pedido, timeout=self.timeout) as respuesta:
                datos = json.loads(respuesta.read())
        except HTTPError as exc:
            esquema.relanzar_si_es_de_esquema(exc)
            if exc.code == 404:
                raise CentralSinConsultaDeEstado(
                    "el central no tiene la consulta de estado de operaciones (es anterior a v0.25)") from None
            raise
        operaciones = datos.get("operaciones") if isinstance(datos, dict) else None
        if not isinstance(operaciones, dict):
            raise ValueError("el central no contesto el estado de las operaciones (se esperaba un objeto JSON)")
        return operaciones


# ── el worker ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResultadoCierreDeRevision:
    """Que paso en un intento."""

    estado: str
    detalle: str | None = None
    consultadas: int = 0
    reenviadas: int = 0
    descartadas: int = 0
    siguen: int = 0


class CierreDeRevisionWorker:
    """Pregunta al central por las operaciones en ``manual_review`` del nodo y cierra las que ya se resolvieron.

    ``transporte`` tiene ``consultar(operation_ids) -> dict`` (:class:`HttpCierreDeRevisionTransport`). ``reloj`` es
    un reloj monotonico en segundos, inyectable para los tests. :meth:`run_once` **no levanta**."""

    def __init__(self, repository, transporte, cada_segundos: float = CADA_SEGUNDOS_POR_DEFECTO,
                 tanda: int = TANDA_POR_DEFECTO, reloj: Callable[[], float] = time.monotonic):
        self.repository = repository
        self.transporte = transporte
        self.cada_segundos = cada_segundos
        self.tanda = max(1, int(tanda))
        self.reloj = reloj
        self._proxima: float | None = None
        self._despues_de: str | None = None
        self._avisado_sin_consulta = False

    def run_once(self, forzar: bool = False) -> ResultadoCierreDeRevision:
        """Un intento si ya toca (``forzar`` lo hace ya). Un ciclo del nodo llama a esto, no cada ``cada_segundos``."""
        if self.cada_segundos <= 0 and not forzar:
            return ResultadoCierreDeRevision(APAGADA)
        ahora = self.reloj()
        if not forzar and self._proxima is not None and ahora < self._proxima:
            return ResultadoCierreDeRevision(NO_TOCA)
        # La ventana se consume antes de preguntar: un error, un 404 o un central lento no se reintentan en el ciclo
        # siguiente (60 s) sino en la ventana siguiente.
        self._proxima = ahora + max(0, self.cada_segundos)
        try:
            return self._intentar()
        except Exception as exc:  # noqa: BLE001 - un fallo de esto no puede frenar la subida de ventas
            return ResultadoCierreDeRevision(ERROR, f"{type(exc).__name__}: {exc}")

    def _intentar(self) -> ResultadoCierreDeRevision:
        ids = self.repository.ids_en_revision(self._despues_de, self.tanda)
        if not ids:
            self._despues_de = None
            return ResultadoCierreDeRevision(SIN_NADA)
        try:
            respuestas = self.transporte.consultar(ids)
        except CentralSinConsultaDeEstado as exc:
            if not self._avisado_sin_consulta:
                self._avisado_sin_consulta = True
                logger.warning("Revision: %s. Las operaciones en revision se reenvian a mano "
                               "(libraedge-nodo revision reenviar).", exc)
            return ResultadoCierreDeRevision(CENTRAL_SIN_CONSULTA, str(exc), consultadas=len(ids))
        self._avisado_sin_consulta = False
        # Tanda llena: puede haber mas, la proxima sigue desde el ultimo. Si no, se vuelve a arrancar por el principio.
        # Se avanza recien con la respuesta en la mano: ante un error, la ventana siguiente repite la misma tanda.
        self._despues_de = ids[-1] if len(ids) >= self.tanda else None

        reenviadas = descartadas = siguen = 0
        for operation_id in ids:
            dato = respuestas.get(operation_id)
            estado = dato.get("estado") if isinstance(dato, dict) else None
            if estado == "applied":
                if self._reenviar(operation_id):
                    reenviadas += 1
            elif estado == "discarded":
                if self._descartar(operation_id, dato.get("motivo")):
                    descartadas += 1
            else:
                siguen += 1  # `manual_review`, o que el central no la conoce, o un estado que este nodo no entiende
        if reenviadas or descartadas:
            logger.info("Revision: de %d consultadas, %d aplicadas en el central (se reenvian) y %d descartadas.",
                        len(ids), reenviadas, descartadas)
        return ResultadoCierreDeRevision(
            CONSULTADA, consultadas=len(ids), reenviadas=reenviadas, descartadas=descartadas, siguen=siguen)

    def _reenviar(self, operation_id: str) -> bool:
        """La misma funcion de ``revision reenviar``: vuelve a ``pending`` y el push siguiente dice ``duplicate``."""
        try:
            self.repository.reenviar_operacion(operation_id)
        except LookupError:
            return False  # ya no estaba en revision: alguien la reenvio o descarto mientras se preguntaba
        return True

    def _descartar(self, operation_id: str, motivo: str | None) -> bool:
        """La misma funcion de ``libraedge-nodo revision descartar`` (ADR-024), con el motivo del central."""
        texto = f"descartada en el central: {motivo}" if (motivo or "").strip() else "descartada en el central"
        try:
            self.repository.descartar_operacion(operation_id, texto)
        except (LookupError, ValueError):  # no existe, ya descartada, o cambio de estado mientras se preguntaba
            return False
        return True
