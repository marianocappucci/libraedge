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


def test_de_16_a_48_bmp_de_32_bits_y_de_64_en_adelante_png():
    # Los chicos en BMP: hay lectores viejos (System.Drawing.Icon) que dibujan basura con un PNG chico (VM Windows 10,
    # 2026-10-19). Los grandes en PNG, para que el archivo no pese de mas.
    for ancho, alto, planos, bits, datos in _entradas():
        assert (planos, bits) == (1, 32)
        if ancho <= 48:
            tamano, w, h, planos_dib, bits_dib, compresion = struct.unpack("<IiiHHI", datos[:20])
            # BITMAPINFOHEADER con el alto doble (imagen + mascara AND), sin comprimir.
            assert (tamano, w, h, planos_dib, bits_dib, compresion) == (40, ancho, 2 * alto, 1, 32, 0)
            paso_mascara = ((ancho + 31) // 32) * 4
            assert len(datos) == 40 + ancho * alto * 4 + paso_mascara * alto
        else:
            assert datos[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", datos[16:24]) == (ancho, alto)
            # Tipo de color 6 = RGBA: las esquinas del cuadrado redondeado son transparentes.
            assert datos[25] == 6


def test_los_bmp_tienen_las_esquinas_transparentes_y_el_centro_opaco():
    for ancho, _, _, _, datos in _entradas():
        if ancho > 48:
            continue

        # Las filas van de abajo arriba, en BGRA: el primer pixel es la esquina inferior izquierda.
        def alfa(x, y_desde_abajo, datos=datos, ancho=ancho):
            return datos[40 + (y_desde_abajo * ancho + x) * 4 + 3]

        assert alfa(0, 0) == 0 and alfa(ancho - 1, ancho - 1) == 0
        assert alfa(ancho // 2, ancho // 2) == 255
        # La mascara AND marca la esquina como transparente (bit en 1) y el centro como opaco.
        paso = ((ancho + 31) // 32) * 4
        mascara = datos[40 + ancho * ancho * 4 :]
        assert mascara[0] & 0x80
        fila_media = mascara[(ancho // 2) * paso : (ancho // 2 + 1) * paso]
        assert not (int.from_bytes(fila_media, "big") >> (paso * 8 - 1 - ancho // 2)) & 1


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
