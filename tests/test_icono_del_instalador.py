"""El icono del instalador y de "Agregar o quitar programas" (la marca de LibraEdge, ADR-078 de libra-ui).

`instalador/libraedge.ico` sale de `instalador/marca/*.svg` con `instalador/armar_icono.py`, que necesita un Chrome
para rasterizar: por eso el `.ico` se commitea y lo que se prueba aca es el archivo, no el armado.
"""

import struct
from pathlib import Path

CARPETA = Path(__file__).resolve().parent.parent / "instalador"
ICO = CARPETA / "libraedge.ico"
CIAN = "#0e7490"
# De 16 a 32 la PC sola (la marca completa se empasta); de 40 a 256 la marca completa. Windows elige el mas cercano:
# 16 y 32 en la lista de programas, 32 a 256 en el Explorador y el escritorio.
TAMANOS = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _entradas():
    datos = ICO.read_bytes()
    reservado, tipo, cantidad = struct.unpack("<HHH", datos[:6])
    assert (reservado, tipo) == (0, 1), "no es un ICO"
    entradas = []
    for i in range(cantidad):
        ancho, alto, _, _, planos, bits, largo, desde = struct.unpack("<BBBBHHII", datos[6 + 16 * i : 22 + 16 * i])
        png = datos[desde : desde + largo]
        entradas.append(((ancho or 256), (alto or 256), planos, bits, png))
    return entradas


def test_el_ico_trae_todos_los_tamanos_cuadrados():
    assert sorted(ancho for ancho, *_ in _entradas()) == TAMANOS
    for ancho, alto, *_ in _entradas():
        assert ancho == alto


def test_cada_tamano_es_un_png_rgba_de_su_medida():
    for ancho, alto, planos, bits, png in _entradas():
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", png[16:24]) == (ancho, alto)
        # Tipo de color 6 = RGBA: las esquinas del cuadrado redondeado son transparentes.
        assert png[25] == 6
        assert (planos, bits) == (1, 32)


def test_la_marca_de_la_que_sale_es_la_de_libraedge():
    for nombre in ("libraedge.svg", "libraedge-favicon.svg"):
        svg = (CARPETA / "marca" / nombre).read_text(encoding="utf-8")
        assert "<title>LibraEdge</title>" in svg
        assert '<rect width="120" height="120" rx="' in svg and f'fill="{CIAN}"' in svg


def test_el_iss_lo_usa_en_el_instalador_y_al_desinstalar():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    assert "\nSetupIconFile=libraedge.ico\n" in iss
    assert "\nUninstallDisplayIcon={app}\\libraedge.ico\n" in iss
    # "Agregar o quitar programas" lee el icono del archivo instalado: si no se copia, Windows muestra el generico.
    assert (
        'Source: "libraedge.ico"' in iss
        and 'DestDir: "{app}"' in iss.split('Source: "libraedge.ico"')[1].splitlines()[0]
    )
