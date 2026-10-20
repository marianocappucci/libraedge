"""El icono de bandeja del nodo en Linux (ADR-012). Dibuja lo que decide ``libraedge/bandeja.py``.

En Windows la bandeja es ``libraedge bandeja`` con pystray, en el Python del nodo. En Linux pystray necesita
PyGObject (``gi``) para hablar con el area de notificacion (AppIndicator), y PyGObject no se instala con pip en el
Python propio del paquete: se compila contra las bibliotecas del sistema (girepository, GTK). Por eso este caparazon
corre con el **python3 del sistema** y su ``python3-gi`` (que el paquete recomienda), y **no importa libraedge**:

- la decision de que mostrar sigue siendo una sola, la de ``bandeja.py``: cada ``REFRESCO`` segundos se le pregunta
  al Python del nodo con ``libraedge-nodo bandeja --una-vez`` (existe desde v0.6, asi que sirve con cualquier
  version de LibraEdge que traiga el producto) y se dibuja lo que contesta;
- el python3 del sistema es 3.10 en Ubuntu 22.04: este archivo es **3.10 y solo biblioteca estandar** (un test lo
  compila con esa gramatica), y ``gi`` se importa recien en ``main``.

En GNOME (Ubuntu 22.04 y 24.04) el area de notificacion es la extension ``ubuntu-appindicators@ubuntu.com``
(paquete ``gnome-shell-extension-appindicator``), que la sesion «ubuntu» trae habilitada para todos los usuarios.
Arranca al iniciar sesion desde ``/etc/xdg/autostart/libraedge-nodo-bandeja.desktop``; si nadie inicia sesion no
hay icono, y esta bien: no hay a quien mostrarselo.

    /usr/bin/python3 /opt/libraedge-nodo/bandeja/bandeja_linux.py           el icono
    /usr/bin/python3 /opt/libraedge-nodo/bandeja/bandeja_linux.py --una-vez  lo que mostraria, y sale
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

RAIZ_NODO = "/opt/libraedge-nodo"
PYTHON_DEL_NODO = RAIZ_NODO + "/python/bin/python3"
DEFINICION = RAIZ_NODO + "/instalador/producto.json"
ESTADO = "/var/lib/libraedge-nodo/estado/estado.json"
#: Cada cuanto se relee el estado. No es cada cuanto sincroniza el nodo (``INTERVALO``): son dos relojes.
REFRESCO = 10
INTERVALO = 60

#: Los mismos tres colores que en Windows (``bandeja_windows.py``).
COLORES = {"ok": "#228b22", "atencion": "#da9100", "problema": "#b22222"}
SIN_ESTADO = "No se pudo leer el estado del nodo"


def interpretar(salida: str, codigo: int) -> tuple[str, str, str]:
    """``(severidad, titulo, detalle)`` de lo que imprime ``libraedge-nodo bandeja --una-vez``::

        [atencion] Sin conexion con el central
          Se sigue operando normalmente; lo pendiente se envia solo al volver

    Lo que no tenga esa forma (un error de Python, un nodo sin instalar) es un problema, con la ultima linea
    como detalle."""
    lineas = [linea for linea in salida.splitlines() if linea.strip()]
    if lineas and lineas[0].startswith("[") and "]" in lineas[0]:
        severidad, _, titulo = lineas[0][1:].partition("]")
        if severidad.strip() in COLORES and titulo.strip():
            return severidad.strip(), titulo.strip(), " ".join(linea.strip() for linea in lineas[1:])
    detalle = lineas[-1].strip() if lineas else f"salio con codigo {codigo}"
    return "problema", SIN_ESTADO, detalle


def consultar(python: str = PYTHON_DEL_NODO, estado: str = ESTADO, intervalo: int = INTERVALO,
              correr=subprocess.run) -> tuple[str, str, str]:
    """Le pregunta al Python del nodo. Nunca levanta: un fallo es un icono rojo con el motivo."""
    comando = [python, "-I", "-B", "-m", "libraedge", "bandeja", "--una-vez", "--estado", estado,
               "--intervalo", str(intervalo)]
    try:
        resultado = correr(comando, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return "problema", SIN_ESTADO, type(exc).__name__
    return interpretar((resultado.stdout or "") + (resultado.stderr or ""), resultado.returncode)


def svg(color: str) -> str:
    """Un circulo del color de la severidad, sin texto (a 16 px no se lee)."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
            f'<circle cx="32" cy="32" r="26" fill="{color}"/></svg>\n')


def escribir_iconos(carpeta: str) -> None:
    os.makedirs(carpeta, mode=0o700, exist_ok=True)
    for severidad, color in COLORES.items():
        with open(os.path.join(carpeta, f"libraedge-{severidad}.svg"), "w", encoding="utf-8") as archivo:
            archivo.write(svg(color))


def producto(ruta: str = DEFINICION) -> tuple[str, int]:
    """Nombre visible y puerto del producto, para el menu. Si no se puede leer, valores neutros."""
    try:
        with open(ruta, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return str(datos.get("nombre") or "LibraEdge"), int(datos.get("puerto") or 8000)
    except (OSError, ValueError, TypeError):
        return "LibraEdge", 8000


def _carpeta_de_la_sesion() -> str:
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/libraedge-{os.getuid()}", "libraedge-bandeja")


def _una_sola_instancia(carpeta: str):
    """Un icono por sesion: el autostart y el postinst pueden abrirla los dos."""
    import fcntl

    os.makedirs(carpeta, mode=0o700, exist_ok=True)
    candado = open(os.path.join(carpeta, "bandeja.lock"), "w")  # vive lo que vive el proceso
    try:
        fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return candado


def _cargar_gtk():
    import gi

    gi.require_version("Gtk", "3.0")
    for nombre in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(nombre, "0.1")
            modulo = __import__("gi.repository", fromlist=[nombre])
            from gi.repository import GLib, Gtk

            return Gtk, GLib, getattr(modulo, nombre)
        except (ValueError, ImportError):
            continue
    raise ImportError("no esta AyatanaAppIndicator3 (gir1.2-ayatanaappindicator3-0.1)")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--una-vez" in argv:
        severidad, titulo, detalle = consultar()
        print(f"[{severidad}] {titulo}" + (f"\n  {detalle}" if detalle else ""))
        return 0 if severidad == "ok" else 1

    carpeta = _carpeta_de_la_sesion()
    candado = _una_sola_instancia(carpeta)
    if candado is None:
        return 0  # ya hay una abierta en esta sesion
    try:
        gtk, glib, indicador_mod = _cargar_gtk()
    except (ImportError, ValueError) as exc:
        print(f"La bandeja de LibraEdge necesita python3-gi y gir1.2-ayatanaappindicator3-0.1: {exc}",
              file=sys.stderr)
        return 1

    escribir_iconos(carpeta)
    nombre, puerto = producto()
    indicador = indicador_mod.Indicator.new(
        "libraedge-nodo", "libraedge-problema", indicador_mod.IndicatorCategory.APPLICATION_STATUS)
    indicador.set_icon_theme_path(carpeta)
    indicador.set_status(indicador_mod.IndicatorStatus.ACTIVE)

    menu = gtk.Menu()
    item_titulo = gtk.MenuItem(label="")
    item_titulo.set_sensitive(False)
    item_detalle = gtk.MenuItem(label="")
    item_detalle.set_sensitive(False)
    abrir = gtk.MenuItem(label=f"Abrir {nombre}")
    abrir.connect("activate", lambda _: subprocess.Popen(["xdg-open", f"http://127.0.0.1:{puerto}"]))
    cerrar = gtk.MenuItem(label="Ocultar el icono")
    cerrar.connect("activate", lambda _: gtk.main_quit())
    for item in (item_titulo, item_detalle, gtk.SeparatorMenuItem(), abrir, cerrar):
        menu.append(item)
    menu.show_all()
    indicador.set_menu(menu)

    def refrescar() -> bool:
        severidad, titulo, detalle = consultar()
        if hasattr(indicador, "set_icon_full"):
            indicador.set_icon_full(f"libraedge-{severidad}", titulo)
        else:  # pragma: no cover - libappindicator viejo
            indicador.set_icon(f"libraedge-{severidad}")
        indicador.set_title(f"{nombre}: {titulo}")
        item_titulo.set_label(titulo)
        item_detalle.set_label(detalle)
        item_detalle.set_visible(bool(detalle))
        return True  # que GLib lo siga llamando

    refrescar()
    glib.timeout_add_seconds(REFRESCO, refrescar)
    gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
