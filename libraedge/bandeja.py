"""Qué muestra la ventana de bandeja. Sin nada de Windows adentro.

Esto es **la decisión**: dado el estado que dejó el nodo, qué ícono, qué título y
qué detalle corresponden. El caparazón gráfico —que sí es de Windows y no se
puede probar acá— vive en `bandeja_windows.py` y no hace más que dibujar lo que
esta función devuelve.

Están separados a propósito: la parte que tiene reglas es la que decide, y esa
se prueba entera. Si estuvieran juntos, lo único verificable sería mirar la
pantalla.

## 🔴 La trampa: un estado viejo no es un estado bueno

El archivo de estado lo escribe el nodo en cada ciclo. Si el servicio se muere
—o alguien lo paró, o la PC quedó a medio arrancar— el archivo **queda como
estaba**, y lo último que dijo puede ser "en línea, todo al día".

Una bandeja que renderice el archivo tal cual mostraría el ícono verde con el
nodo muerto: exactamente la pantalla que no puede mentir. Por eso lo primero que
mira `resumen_para_la_bandeja` es **cuándo** se escribió, no qué dice.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum


class Severidad(StrEnum):
    """El ícono. Tres estados, no cinco: es una bandeja, se mira de reojo."""

    OK = "ok"                 # sincronizando, nada pendiente
    ATENCION = "atencion"     # anda pero hay algo que mirar: pendientes, o sin enlace
    PROBLEMA = "problema"     # el nodo no está funcionando


@dataclass(frozen=True)
class ResumenBandeja:
    severidad: Severidad
    titulo: str
    detalle: str

    @property
    def texto(self) -> str:
        """Lo que va en el tooltip, en una sola pieza."""
        return f"{self.titulo}\n{self.detalle}" if self.detalle else self.titulo


#: Cuántos ciclos puede saltearse el nodo antes de que se lo dé por caído.
#:
#: Tres y no uno: un ciclo puede tardar de más porque el central está lento o
#: porque la PC estaba ocupada, y una bandeja que grita a la primera demora
#: enseña a ignorarla. Tres ciclos seguidos sin escribir ya no es demora.
CICLOS_ANTES_DE_DARLO_POR_MUERTO = 3


def _antiguedad(marca: str | None, ahora: datetime) -> timedelta | None:
    if not marca:
        return None
    try:
        cuando = datetime.fromisoformat(marca)
    except ValueError:
        return None
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=UTC)
    return ahora - cuando


def _describir(delta: timedelta) -> str:
    segundos = int(delta.total_seconds())
    if segundos < 60:
        return "hace menos de un minuto"
    minutos = segundos // 60
    if minutos < 60:
        return f"hace {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"hace {horas} h"
    return f"hace {horas // 24} d"


def resumen_para_la_bandeja(estado, ahora: datetime | None = None,
                            intervalo_segundos: int = 60) -> ResumenBandeja:
    """Qué mostrar, dado el último estado que dejó el nodo.

    `estado` es un `EstadoNodo` o `None` —que es lo que devuelve `leer_estado`
    cuando el archivo no existe todavía o está corrupto—.

    El orden de las preguntas importa y es el de la gravedad: primero si el nodo
    está vivo, después si hay enlace, después si quedó algo sin mandar.
    """
    ahora = ahora or datetime.now(UTC)

    if estado is None:
        return ResumenBandeja(
            Severidad.PROBLEMA, "El nodo todavía no sincronizó",
            "No hay estado escrito. Si acaba de instalarse, es normal; si no, "
            "revisar que el servicio esté corriendo.",
        )

    # 🔴 Primero la antigüedad, y no lo que el estado dice. Un archivo viejo que
    # dice "en línea" es el modo de fallar más engañoso que tiene esta pantalla.
    antiguedad = _antiguedad(estado.ultimo_intento, ahora)
    limite = timedelta(seconds=intervalo_segundos * CICLOS_ANTES_DE_DARLO_POR_MUERTO)
    if antiguedad is None or antiguedad > limite:
        cuando = _describir(antiguedad) if antiguedad else "nunca"
        return ResumenBandeja(
            Severidad.PROBLEMA, "El nodo no está sincronizando",
            f"El último intento fue {cuando}. El servicio puede estar detenido."
            + (f" Hay {estado.pendientes} operaciones sin enviar."
               if estado.pendientes else ""),
        )

    if not estado.en_linea:
        detalle = "Se sigue operando normalmente; lo pendiente se envía solo al volver."
        if estado.pendientes:
            detalle = (f"{estado.pendientes} operaciones esperando. " + detalle)
        return ResumenBandeja(Severidad.ATENCION, "Sin conexión con el central", detalle)

    if estado.pendientes:
        return ResumenBandeja(
            Severidad.ATENCION, "Sincronizando",
            f"{estado.pendientes} operaciones todavía en cola.",
        )

    return ResumenBandeja(
        Severidad.OK, "Al día",
        f"Última sincronización {_describir(antiguedad)}.",
    )
