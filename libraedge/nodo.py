"""El nodo como proceso: un ciclo de sincronizacion y un estado legible.

Hasta la Fase 4 (2026-08-30) LibraEdge era una biblioteca que **no corria sola**:
tenia el outbox, el worker, el transporte, el receptor y el espejo, pero nadie
los invocaba. Un nodo acumulaba operaciones y no las mandaba nunca. Esto es lo
que las pone a andar.

## El ciclo ES la sonda de conectividad

La tentacion es agregar un ping a `/health` del central y llamarlo "deteccion de
corte". No se hace, por dos motivos:

1. **Un `200` no prueba que la sincronizacion ande.** El catch-all de una SPA
   devuelve `200` con el `index.html` para cualquier ruta que no exista, asi que
   un nodo apuntado a la URL equivocada tendria una sonda en verde eterno. Y aun
   con la URL bien, el central puede responder el health y rechazar los push por
   credenciales.
2. **Una sonda separada se desincroniza de lo que mide.** Si la sonda dice "en
   linea" y los push fallan, la pantalla miente en el peor momento posible.

Aca el estado sale del **resultado real de sincronizar**: si el push y el pull
pasaron, el nodo esta en linea; si el transporte fallo, esta fuera. El
instrumento y el trabajo son el mismo, que es lo correcto cuando lo que importa
no es "hay internet" sino "puedo sincronizar".

## Primero sube, despues baja

El orden no es casual. Lo que el nodo genero durante el corte --las ventas-- **no
existe en ningun otro lado**; los datos de referencia que el central tiene para
darle existen igual si el ciclo se corta a la mitad. Subir primero achica la
ventana en la que un dato irremplazable vive en un solo disco.

## El nodo no se cae porque se caiga internet

Es la razon de existir de todo esto. `sincronizar()` **nunca propaga** un error
de transporte: lo registra, marca el nodo fuera de linea y devuelve. Un
`raise` aca seria un proceso muerto en la PC de un cliente en el peor momento.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class EstadoNodo:
    """Lo que el nodo sabe de si mismo, para que una UI lo muestre.

    Es lo que se escribe al archivo de estado y lo que leeria tanto la ventana
    de bandeja como una pantalla del producto.
    """

    node_id: str
    en_linea: bool
    pendientes: int
    cursor: int
    ultimo_intento: str | None = None
    ultima_sincronizacion_ok: str | None = None
    ultimo_error: str | None = None
    operaciones_subidas: int = 0
    cambios_bajados: int = 0

    def como_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def escribir_estado(ruta: str, estado: EstadoNodo) -> None:
    """Escribe el estado de forma **atomica**.

    Un `open(ruta, "w")` directo deja el archivo truncado y a medio escribir
    durante unos milisegundos, y lo que lo lee es una UI que refresca sola: le
    tocaria un JSON partido justo cuando el nodo esta sincronizando, que es
    cuando mas se lo mira. `os.replace()` es atomico dentro del mismo sistema de
    archivos, asi que el lector ve la version vieja entera o la nueva entera.
    """
    carpeta = os.path.dirname(os.path.abspath(ruta)) or "."
    os.makedirs(carpeta, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(dir=carpeta, prefix=".estado-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(estado.como_json())
        os.replace(temporal, ruta)
    except BaseException:
        if os.path.exists(temporal):
            os.unlink(temporal)
        raise


def leer_estado(ruta: str) -> EstadoNodo | None:
    """El ultimo estado escrito, o `None` si el nodo nunca sincronizo."""
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return EstadoNodo(**json.load(archivo))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


class Nodo:
    """Une el outbox que sube y el espejo que baja, en un ciclo.

    `outbox_worker` y `pull_worker` son los de `sync/worker.py` y `sync/pull.py`.
    Se reciben ya armados en vez de construirlos aca: quien despliega el nodo es
    el que sabe la URL del central, el secreto y que tablas espeja.
    """

    def __init__(self, repository, node_id: str, outbox_worker=None,
                 pull_worker=None, ruta_estado: str | None = None):
        self.repository = repository
        self.node_id = node_id
        self.outbox_worker = outbox_worker
        self.pull_worker = pull_worker
        self.ruta_estado = ruta_estado
        self._ultima_ok: str | None = None

    def pendientes(self) -> int:
        return len(self.repository.list_pending_operations(limit=10_000))

    def sincronizar(self, limite: int = 500) -> EstadoNodo:
        """Un ciclo: sube lo que haya, despues baja lo que cambio.

        Nunca levanta por un fallo de transporte -- ver el docstring del modulo.
        """
        intento = _ahora()
        subidas = 0
        bajados = 0
        error: str | None = None

        # 🔴 La fila propia del nodo, antes de tocar el cursor. Es idempotente y
        # va en cada ciclo a propósito: si falta, `set_server_cursor` hace un
        # UPDATE de cero filas --que es un éxito-- y el nodo vuelve a bajar el
        # espejo entero para siempre, sin que nada falle. Ponerlo acá y no en el
        # instalador hace que valga para cualquier nodo, se haya instalado como
        # se haya instalado.
        self.repository.asegurar_identidad_local(self.node_id)

        try:
            if self.outbox_worker is not None:
                # 🔴 El worker **no levanta** cuando el transporte falla: atrapa
                # el error y lo convierte en un reintento, que es lo que lo hace
                # durable. Asi que un `except` alrededor no ve nada, y su cuenta
                # de `procesadas` incluye a las que fallaron. Esta linea es la
                # que hace que un corte se note: sin ella el nodo se declara en
                # linea con la cola entera atascada.
                resultado = self.outbox_worker.run_once(limit=limite)
                subidas = resultado.confirmadas
                if resultado.hubo_falla_de_transporte:
                    error = resultado.ultimo_error
            if self.pull_worker is not None:
                bajados = self.pull_worker.run_once(self.node_id, limit=limite)
        except Exception as excepcion:  # noqa: BLE001 - ver el docstring del modulo
            error = f"{type(excepcion).__name__}: {excepcion}"

        # Hubo un `fallo_al_subir` aparte de `error` hasta que la matriz de
        # mutaciones mostro que sacarlo dejaba la suite verde: `error` ya queda
        # seteado en la misma rama, asi que era una segunda condicion que no
        # decidia nada.
        en_linea = error is None
        if en_linea:
            self._ultima_ok = intento

        estado = EstadoNodo(
            node_id=self.node_id,
            en_linea=en_linea,
            pendientes=self.pendientes(),
            cursor=self.repository.get_server_cursor(self.node_id),
            ultimo_intento=intento,
            ultima_sincronizacion_ok=self._ultima_ok,
            ultimo_error=error,
            operaciones_subidas=subidas,
            cambios_bajados=bajados,
        )
        if self.ruta_estado:
            escribir_estado(self.ruta_estado, estado)
        return estado
