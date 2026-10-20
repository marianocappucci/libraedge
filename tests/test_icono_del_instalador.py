"""Los iconos de Windows: el del instalador y de "Agregar o quitar programas" (la marca de LibraEdge, ADR-078 de
libra-ui) y el del acceso directo de cada producto (su marca, ADR-013).

`instalador/libraedge.ico` e `instalador/iconos/<producto>.ico` salen de `instalador/marca/*.svg` con
`instalador/armar_icono.py`, que necesita un Chrome para rasterizar: por eso los `.ico` se commitean y lo que se prueba
aca son los archivos, no el armado.
"""

import importlib.util
import json
import struct
from pathlib import Path

import pytest

CARPETA = Path(__file__).resolve().parent.parent / "instalador"
ICO = CARPETA / "libraedge.ico"
PRODUCTOS = sorted(p.stem for p in (CARPETA / "productos").glob("*.json"))
ICOS = [ICO] + [CARPETA / "iconos" / f"{p}.ico" for p in PRODUCTOS]
CIAN = "#0e7490"
# De 16 a 32 la PC sola (la marca completa se empasta); de 40 a 256 la marca completa. Windows elige el mas cercano:
# 16 y 32 en la lista de programas, 32 a 256 en el Explorador y el escritorio.
TAMANOS = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def _entradas(ico: Path = ICO):
    datos = ico.read_bytes()
    reservado, tipo, cantidad = struct.unpack("<HHH", datos[:6])
    assert (reservado, tipo) == (0, 1), "no es un ICO"
    entradas = []
    for i in range(cantidad):
        ancho, alto, _, _, planos, bits, largo, desde = struct.unpack("<BBBBHHII", datos[6 + 16 * i : 22 + 16 * i])
        png = datos[desde : desde + largo]
        entradas.append(((ancho or 256), (alto or 256), planos, bits, png))
    return entradas


@pytest.mark.parametrize("ico", ICOS, ids=lambda p: p.name)
def test_el_ico_trae_todos_los_tamanos_cuadrados(ico):
    assert sorted(ancho for ancho, *_ in _entradas(ico)) == TAMANOS
    for ancho, alto, *_ in _entradas(ico):
        assert ancho == alto


@pytest.mark.parametrize("ico", ICOS, ids=lambda p: p.name)
def test_de_16_a_48_bmp_de_32_bits_y_de_64_en_adelante_png(ico):
    # Los chicos en BMP: hay lectores viejos (System.Drawing.Icon) que dibujan basura con un PNG chico (VM Windows 10,
    # 2026-10-19). Los grandes en PNG, para que el archivo no pese de mas.
    for ancho, alto, planos, bits, datos in _entradas(ico):
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


@pytest.mark.parametrize("ico", ICOS, ids=lambda p: p.name)
def test_los_bmp_tienen_las_esquinas_transparentes_y_el_centro_opaco(ico):
    for ancho, _, _, _, datos in _entradas(ico):
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


# ── la marca de cada producto, para su acceso directo (ADR-013) ─────────────


@pytest.mark.parametrize("producto", PRODUCTOS)
def test_cada_producto_tiene_su_marca_y_su_ico(producto):
    nombre = json.loads((CARPETA / "productos" / f"{producto}.json").read_text(encoding="utf-8"))["nombre"]
    for svg in (f"{producto}.svg", f"{producto}-favicon.svg"):
        # Las de libra-ui (marcas/*.svg) tal cual: su titulo es el nombre visible del producto.
        texto = (CARPETA / "marca" / svg).read_text(encoding="utf-8")
        assert texto.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">')
        assert f"<title>{nombre}</title>" in texto
    assert (CARPETA / "iconos" / f"{producto}.ico").is_file()


def test_armar_icono_arma_el_de_libraedge_y_uno_por_producto():
    spec = importlib.util.spec_from_file_location("armar_icono", CARPETA / "armar_icono.py")
    armar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(armar)
    assert armar.destinos() == {"libraedge": ICO, **{p: CARPETA / "iconos" / f"{p}.ico" for p in PRODUCTOS}}
    # Y el PNG de 256 px de cada uno, para la ventana de la app de escritorio en Linux (ADR-015).
    esperados = sorted([f"{p}.ico" for p in PRODUCTOS] + [f"{p}.png" for p in PRODUCTOS])
    assert sorted(p.name for p in (CARPETA / "iconos").iterdir()) == esperados


def test_el_iss_instala_el_ico_del_producto():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    assert 'Source: "iconos\\{#Producto}.ico"' in iss
    linea = iss.split('Source: "iconos\\{#Producto}.ico"')[1].splitlines()[0]
    assert 'DestDir: "{app}"' in linea and 'DestName: "producto.ico"' in linea
