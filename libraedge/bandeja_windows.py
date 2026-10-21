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

**Abrir <Producto>** (ADR-013) abre el mismo acceso directo del menu Inicio que deja el instalador: el producto en
modo aplicacion de Edge, desde la copia local. Es la accion por defecto (clic en el icono). Otra linea del menu dice
la direccion para los otros puestos del local, con la IP de esta PC, y al hacer clic la copia.

Depende de `pystray` y `Pillow`, que están en el extra `bandeja` — no en las
dependencias normales: el nodo tiene que poder correr como servicio en una PC sin
sesión gráfica, y ahí un import de GUI sobra.
"""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timezone

from libraedge.bandeja import (
    Severidad,
    acceso_directo_de_windows,
    etiqueta_otros_puestos,
    producto_del_nodo,
    resumen_para_la_bandeja,
)
from libraedge.instalador import ip_de_la_lan, url_de_la_lan, url_local
from libraedge.nodo import leer_estado

#: Sin ventana de consola para `clip.exe` (la bandeja corre con pythonw).
_SIN_VENTANA = 0x08000000

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


def definicion_instalada() -> str:
    """El Python embebido esta en ``<RaizNodo>\\python``: la definicion del producto, en
    ``<RaizNodo>\\instalador\\producto.json``. Sin argumentos nuevos: el acceso directo de la bandeja lo crea el
    instalador y la bandeja es la de la version de LibraEdge que pinee el producto."""
    return os.path.join(os.path.dirname(sys.prefix), "instalador", "producto.json")


def abrir_producto(acceso: str | None, url: str, *, existe: Callable[[str], bool] = os.path.exists,
                   abrir_archivo: Callable[[str], object] | None = None,
                   navegador: Callable[[str], object] | None = None) -> str:
    """Abre el producto como el acceso directo del instalador; si no esta, la URL local en el navegador.

    Devuelve que uso (``"acceso"`` o ``"navegador"``)."""
    if acceso and existe(acceso):
        (abrir_archivo or os.startfile)(acceso)  # type: ignore[attr-defined]  # solo existe en Windows
        return "acceso"
    if navegador is None:
        import webbrowser

        navegador = webbrowser.open
    navegador(url)
    return "navegador"


def copiar(texto: str, correr: Callable[..., object] = subprocess.run) -> None:
    """Al portapapeles con ``clip.exe``, sin dependencias. Un fallo no tumba la bandeja."""
    try:
        correr(["clip"], input=texto, text=True, check=False, creationflags=_SIN_VENTANA)
    except OSError:
        pass


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

    producto = producto_del_nodo(definicion_instalada())
    items = [pystray.MenuItem(lambda _: _resumen().titulo, None, enabled=False)]
    if producto:
        nombre, puerto = producto
        acceso = acceso_directo_de_windows(nombre, os.environ.get("ProgramData"))

        def _otros_puestos() -> str:
            return url_de_la_lan(ip_de_la_lan(), puerto)

        items += [
            pystray.MenuItem(f"Abrir {nombre}", lambda: abrir_producto(acceso, url_local(puerto)), default=True),
            pystray.MenuItem(lambda _: etiqueta_otros_puestos(_otros_puestos()), lambda: copiar(_otros_puestos())),
        ]
    items.append(pystray.MenuItem("Salir", lambda: icono.stop()))

    inicial = _resumen()
    icono = pystray.Icon(
        "libraedge", _icono(inicial.severidad), inicial.texto, menu=pystray.Menu(*items),
    )

    def _refrescar(_):
        icono.visible = True
        while icono.visible:
            actual = _resumen()
            icono.icon = _icono(actual.severidad)
            # El tooltip es lo que el operador lee: ahí va el detalle completo.
            icono.title = actual.texto
            # Los textos del menu (el estado, la IP de los otros puestos) se evaluan al rearmarlo.
            icono.update_menu()
            time.sleep(refresco_segundos)

    icono.run(setup=_refrescar)
    return 0
