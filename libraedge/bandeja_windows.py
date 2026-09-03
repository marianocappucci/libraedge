"""El ícono de bandeja de Windows. Dibuja lo que decide `bandeja.py`.

> 🟢 **Probado en una VM con Windows 11 el 2026-08-31.** Se escribió en un
> entorno sin Windows y sin escritorio, así que estuvo sin ejecutar hasta esa
> fecha. Lo verificado, enumerando el área de notificación por UI Automation
> desde la sesión del usuario:
>
> - El ícono aparece en la barra de tareas —elemento entre el Explorador y
>   Seguridad de Windows— y su tooltip es exactamente lo que produce
>   `bandeja.py`: *"Al día / Última sincronización hace menos de un minuto."*
> - **Cambia solo**: al desconectar la red pasó a *"Sin conexión con el central /
>   Se sigue operando normalmente; lo pendiente se envía solo al volver"*, y al
>   reconectar volvió.
> - Los tres colores se dibujan con el valor de su severidad, y la esquina del
>   PNG queda transparente.
>
> Lo que **no** se verificó por píxeles: cómo se ve el círculo a 16 px en la
> pantalla. La captura GDI no toma la barra de tareas de Windows 11, y el color
> ya está comprobado leyendo el píxel central de `_icono()`.
>
> 🔴 Y el escenario que hay que tener presente: la bandeja **no puede correr
> como servicio** —Windows no le deja dibujar a la sesión 0—, así que arranca
> desde un acceso directo del inicio de sesión. Si nadie inicia sesión en esa
> PC, no hay ícono; y está bien, porque no hay a quién mostrárselo.
>
> Para verlo sin Windows y sin ícono: `libraedge-nodo bandeja --una-vez`, que
> imprime exactamente lo mismo que iría en el tooltip.

Depende de `pystray` y `Pillow`, que están en el extra `bandeja` — no en las
dependencias normales: el nodo tiene que poder correr como servicio en una PC sin
sesión gráfica, y ahí un import de GUI sobra.
"""

import time
from datetime import UTC, datetime, timezone

from libraedge.bandeja import Severidad, resumen_para_la_bandeja
from libraedge.nodo import leer_estado

#: Los tres colores, en el orden en que se miran de reojo. Verde/ámbar/rojo y no
#: una paleta: es un ícono de 16 píxeles al lado del reloj.
_COLORES = {
    Severidad.OK: (34, 139, 34),
    Severidad.ATENCION: (218, 145, 0),
    Severidad.PROBLEMA: (178, 34, 34),
}


def _icono(severidad: Severidad):
    """Un círculo del color de la severidad. Sin texto: a 16 px no se lee."""
    from PIL import Image, ImageDraw

    imagen = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(imagen)
    dibujo.ellipse((6, 6, 58, 58), fill=_COLORES[severidad] + (255,))
    return imagen


def correr(ruta_estado: str, intervalo_segundos: int = 60,
           refresco_segundos: int = 10) -> int:
    """Muestra el ícono y lo refresca hasta que lo cierren.

    `refresco_segundos` es cada cuánto **relee el archivo**, no cada cuánto
    sincroniza el nodo: son dos relojes distintos y confundirlos haría que la
    bandeja mostrara datos viejos justo cuando cambian. Se relee más seguido de
    lo que el nodo escribe, a propósito.
    """
    try:
        import pystray
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise SystemExit(
            "Falta pystray. Instalar con: pip install 'libraedge[bandeja]'"
        ) from exc

    def _resumen():
        return resumen_para_la_bandeja(
            leer_estado(ruta_estado), datetime.now(UTC), intervalo_segundos
        )

    inicial = _resumen()
    icono = pystray.Icon(
        "libraedge", _icono(inicial.severidad), inicial.texto,
        menu=pystray.Menu(
            pystray.MenuItem(lambda _: _resumen().titulo, None, enabled=False),
            pystray.MenuItem("Salir", lambda: icono.stop()),
        ),
    )

    def _refrescar(_):
        icono.visible = True
        while icono.visible:
            actual = _resumen()
            icono.icon = _icono(actual.severidad)
            # El tooltip es lo que el operador lee: ahí va el detalle completo.
            icono.title = actual.texto
            time.sleep(refresco_segundos)

    icono.run(setup=_refrescar)
    return 0
