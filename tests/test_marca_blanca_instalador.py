"""La marca blanca del instalador y de la app de escritorio (ADR-029): `instalador/marca.py`, y que `nodo.iss`,
`build.rs` y `armar.py` la usen.

Lo que se mide acá es lo que se puede medir sin Windows: la herramienta que arma la carpeta de la marca
(validación, archivos, `.ico`,
`.isi`) y que el `.iss`, `build.rs` y `armar.py` estén cableados a esos archivos. **No se compila** el `.iss`
(Inno Setup) ni la app
(Rust/Tauri): eso lo prueba el CI de Windows (ver la sección «Marca blanca» del README del instalador).
"""

import io
import json
import pathlib
import re
import struct
import subprocess
import sys

import pytest
from PIL import Image

RAIZ = pathlib.Path(__file__).resolve().parent.parent
CARPETA = RAIZ / "instalador"
sys.path.insert(0, str(CARPETA))
import marca as m  # noqa: E402

from libraedge import instalador as ins  # noqa: E402


def _png(ancho=300, alto=120) -> bytes:
    b = io.BytesIO()
    Image.new("RGBA", (ancho, alto), (14, 116, 144, 255)).save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def salida(tmp_path):
    return tmp_path / "marca-blanca"


def _armar(salida, nombre="Clínica Modelo", editor="Clínica Modelo SRL", producto="ventalibra", logo=None):
    return m.armar(producto, nombre, editor, logo or _png(), salida)


# ── el nombre y el editor ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("nombre", ["Modelo", "Clínica Modelo", "Ñandú 2", "A", "Acme-Sur_1.0", "Comercial  Modelo"])
def test_nombres_que_sirven(nombre):
    assert m.validar_nombre(nombre) == " ".join(nombre.split())


@pytest.mark.parametrize(
    "nombre",
    [
        "",
        " ",
        'Mi "marca"',
        "A\\B",
        "A/B",
        "A:B",
        "A%PATH%",
        "A{x}",
        "A;B",
        "A&B",
        "A|B",
        "<A>",
        "-A",
        "A-",
        ".A",
        "A.",
        "x" * 41,
        "CON",
        "nul",
        "Lpt1",
        "A\x00B",
        "A$B",
        "A`B",
    ],
)
def test_nombres_que_no_sirven(nombre):
    with pytest.raises(m.MarcaInvalida):
        m.validar_nombre(nombre)


@pytest.mark.parametrize("editor", ["Modelo SRL", "Modelo S.R.L.", "Pérez & Hijos", "Modelo, Sur"])
def test_editores_que_sirven(editor):
    assert m.validar_editor(editor) == editor


@pytest.mark.parametrize("editor", ["", 'Modelo "SRL"', "A\\B", "A;B", "A%B", "A{B}", "x" * 61])
def test_editores_que_no_sirven(editor):
    with pytest.raises(m.MarcaInvalida):
        m.validar_editor(editor)


def test_el_slug_es_ascii_y_sirve_de_identificador():
    assert m.slug("Clínica Modelo") == "clinica-modelo"
    assert m.slug("Ñandú 2.0") == "nandu-2-0"
    assert m.slug("---") == "marca"


# ── lo que arma ───────────────────────────────────────────────────────────────────────────────────


def test_arma_todos_los_archivos(salida):
    resumen = _armar(salida)
    assert sorted(p.name for p in salida.iterdir()) == [
        "icono.ico",
        "icono.png",
        "marca.isi",
        "marca.json",
        "marca.svg",
        "producto.json",
    ]
    assert resumen["formato"] == "libraedge-marca/1"
    assert json.loads((salida / "marca.json").read_text(encoding="utf-8")) == resumen
    assert resumen["identificador"] == "nodo.clinica-modelo"
    assert re.fullmatch(r"[A-Za-z0-9.-]+", resumen["identificador"]), (
        "Tauri sólo acepta letras, números, guiones y puntos"
    )


def test_la_definicion_cambia_el_nombre_y_nada_mas(salida):
    _armar(salida)
    original = json.loads((CARPETA / "productos" / "ventalibra.json").read_text(encoding="utf-8"))
    marcada = json.loads((salida / "producto.json").read_text(encoding="utf-8"))
    assert marcada["nombre"] == "Clínica Modelo"
    diferencias = {k for k in set(original) | set(marcada) if original.get(k) != marcada.get(k)}
    assert diferencias == {"nombre", "_marca"}
    # El app_id no se cambia nunca: es lo que hace que una reinstalación actualice (y que el nodo siga siendo el
    # mismo producto).
    assert marcada["app_id"] == original["app_id"]


def test_la_definicion_de_la_marca_la_lee_el_instalador(salida):
    _armar(salida)
    definicion = ins.leer_definicion(str(salida / "producto.json"))
    assert definicion.nombre == "Clínica Modelo" and definicion.producto == "ventalibra"


@pytest.mark.parametrize("producto", sorted(p.stem for p in (CARPETA / "productos").glob("*.json")))
def test_sirve_para_cada_producto(salida, producto):
    resumen = m.armar(producto, "Modelo", "Modelo SRL", _png(), salida)
    assert resumen["producto"] == producto
    assert (
        json.loads((salida / "producto.json").read_text(encoding="utf-8"))["app_id"]
        == json.loads((CARPETA / "productos" / f"{producto}.json").read_text(encoding="utf-8"))["app_id"]
    )


def test_el_ico_tiene_todos_los_tamanos_y_el_formato_que_lee_cualquiera(salida):
    _armar(salida)
    datos = (salida / "icono.ico").read_bytes()
    reservado, tipo, cantidad = struct.unpack("<HHH", datos[:6])
    assert (reservado, tipo) == (0, 1) and cantidad == len(m.LADOS)
    lados = []
    for i in range(cantidad):
        ancho, _, _, _, _, bits, largo, desplazamiento = struct.unpack(
            "<BBBBHHII", datos[6 + 16 * i : 6 + 16 * (i + 1)]
        )
        lado = ancho or 256
        lados.append(lado)
        entrada = datos[desplazamiento : desplazamiento + largo]
        # Lo que dice instalador/armar_icono.py: hasta 48 px va BMP (lo leen los lectores viejos), de ahí en
        # adelante PNG.
        assert entrada.startswith(b"\x89PNG") == (lado > 48), lado
    assert sorted(lados) == sorted(m.LADOS)


def test_el_png_es_cuadrado_de_256_y_el_logo_entra_entero(salida):
    _armar(salida, logo=_png(400, 100))
    imagen = Image.open(salida / "icono.png")
    assert imagen.size == (256, 256) and imagen.mode == "RGBA"
    assert imagen.getpixel((128, 128))[3] == 255, "el centro es el logo"
    assert imagen.getpixel((128, 4))[3] == 0, "arriba y abajo queda transparente: no se recorta el logo"


def test_el_svg_lleva_el_logo_y_ningun_nombre_de_libra(salida):
    _armar(salida)
    svg = (salida / "marca.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg") and "data:image/png;base64," in svg
    sin_datos = re.sub(r"base64,[A-Za-z0-9+/=]+", "base64,", svg)
    assert "libra" not in sin_datos.lower()


def test_el_isi_va_en_utf8_con_bom_y_define_lo_que_el_iss_pisa(salida):
    _armar(salida)
    crudo = (salida / "marca.isi").read_bytes()
    assert crudo.startswith(b"\xef\xbb\xbf"), "sin BOM, ISCC lee un acento con la página de códigos del sistema"
    texto = crudo.decode("utf-8-sig")
    definidas = dict(re.findall(r'^#define (\w+)\s+"([^"]*)"$', texto, re.M))
    assert definidas["NombreProducto"] == "Clínica Modelo" and definidas["Editor"] == "Clínica Modelo SRL"
    assert definidas["NombreSalida"] == "clinica-modelo"
    # Nada de ISPP adentro de un valor: ni llaves, ni comillas, ni #.
    assert all(not re.search(r'[{}#"]', valor) for valor in definidas.values())
    # Tiene que definir EXACTAMENTE lo que el .iss deshace antes de incluirlo.
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    bloque = iss.split("#ifdef ConMarca")[1].split("#endif")[0]
    assert sorted(re.findall(r"#undef (\w+)", bloque)) == sorted(definidas)


def test_nada_se_escribe_si_algo_no_sirve(salida):
    with pytest.raises(m.MarcaInvalida):
        _armar(salida, nombre='Mi "marca"')
    with pytest.raises(m.MarcaInvalida):
        _armar(salida, logo=b"no es una imagen")
    with pytest.raises(m.MarcaInvalida):
        _armar(salida, producto="noexiste")
    with pytest.raises(m.MarcaInvalida):
        _armar(salida, producto="../ventalibra")
    with pytest.raises(m.MarcaInvalida):
        _armar(salida, logo=b"\x00" * (m.MAX_LOGO + 1))
    assert not salida.exists()


# ── el .iss, build.rs y armar.py usan lo que arma ──────────────────────────────────────────────────────


def _expandir(iss: str, producto: str, con_marca: bool, isi: str = "") -> dict:
    """Un ISPP de juguete: sólo `#define`, `#undef`, `#ifdef ConMarca` y el `#include` de la marca. Devuelve las
    macros finales."""
    macros: dict = {"Producto": producto}
    activo = [True]
    for linea in iss.splitlines():
        t = linea.strip()
        if t.startswith("#ifdef ConMarca"):
            activo.append(con_marca)
        elif t.startswith("#endif") and len(activo) > 1:
            activo.pop()
        elif not all(activo):
            continue
        elif t.startswith('#include "marca-blanca'):
            for l2 in isi.splitlines():
                mm = re.match(r'#define (\w+)\s+"([^"]*)"$', l2)
                if mm:
                    macros[mm.group(1)] = mm.group(2)
        elif t.startswith("#undef"):
            macros.pop(t.split()[1], None)
        elif t.startswith("#define"):
            nombre, _, valor = t[len("#define") :].strip().partition(" ")
            valor = valor.strip()
            macros[nombre] = valor[1:-1] if valor.startswith('"') else macros.get(valor, valor)
    return macros


def test_sin_marca_el_iss_sale_igual_que_siempre():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    macros = _expandir(iss, "ventalibra", con_marca=False)
    assert macros["NombreNodo"] == "Nodo LibraEdge" and macros["Editor"] == "Mariano Cappucci"
    assert f"{macros['PrefijoSalida']}{macros['NombreSalida']}" == "libraedge-nodo-ventalibra"
    assert (macros["DirProducto"], macros["ArchivoProducto"]) == ("productos", "ventalibra")
    assert (macros["DirIconos"], macros["ArchivoIcono"]) == ("iconos", "ventalibra")
    assert macros["IconoInstalador"] == "libraedge.ico"


def test_con_marca_el_iss_toma_todo_de_marca_blanca(salida):
    _armar(salida)
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    macros = _expandir(iss, "ventalibra", con_marca=True, isi=(salida / "marca.isi").read_text(encoding="utf-8-sig"))
    assert macros["NombreProducto"] == "Clínica Modelo" and macros["Editor"] == "Clínica Modelo SRL"
    assert f"{macros['PrefijoSalida']}{macros['NombreSalida']}" == "nodo-clinica-modelo"
    assert (macros["DirProducto"], macros["ArchivoProducto"]) == ("marca-blanca", "producto")
    assert (macros["DirIconos"], macros["ArchivoIcono"]) == ("marca-blanca", "icono")
    assert macros["IconoInstalador"] == "marca-blanca\\icono.ico"
    # Y los archivos a los que apuntan los genera marca.py.
    for relativo in (
        f"{macros['DirProducto']}\\{macros['ArchivoProducto']}.json",
        f"{macros['DirIconos']}\\{macros['ArchivoIcono']}.ico",
        macros["IconoInstalador"],
    ):
        assert (salida / relativo.split("\\")[-1]).is_file(), relativo
    assert "{" not in "".join(v for k, v in macros.items() if k not in ("Producto",))


def test_el_iss_no_nombra_el_producto_ni_libra_en_el_nombre_visible_con_marca():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    # AppName y los accesos salen de macros que la marca redefine; ninguno de esos usos escribe «LibraEdge» a mano.
    for linea in iss.splitlines():
        if linea.startswith(("AppName=", "AppPublisher=", "OutputBaseFilename=")):
            assert "LibraEdge" not in linea and "Libra" not in linea.replace("PrefijoSalida", ""), linea


def test_el_iss_con_marca_no_cambia_lo_que_no_debe_cambiar():
    """Servicios, carpetas de datos, AppId y reglas de firewall: iguales con y sin marca (ADR-029). Sólo se pisan
    las macros del bloque."""
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    bloque = iss.split("#ifdef ConMarca")[1].split("#endif")[0]
    pisadas = set(re.findall(r"#undef (\w+)", bloque))
    assert "AppIdProducto" not in pisadas and "Producto" not in pisadas
    assert "DefaultDirName={autopf}\\LibraEdge" in iss and 'Name: "{commonappdata}\\LibraEdge\\datos"' in iss
    assert "AppId={{{#AppIdProducto}}" in iss


def test_build_rs_y_armar_py_pasan_la_marca():
    build = (RAIZ / "escritorio" / "build.rs").read_text(encoding="utf-8")
    armar = (RAIZ / "escritorio" / "armar.py").read_text(encoding="utf-8")
    assert "LIBRAEDGE_MARCA_DIR" in build and "LIBRAEDGE_MARCA_DIR" in armar
    for archivo in ("producto.json", "icono.", "marca.svg", "marca.json"):
        assert archivo in build, archivo
    # Sin la variable, el identificador y las rutas son los de siempre.
    assert 'format!("ar.com.librasuite.{producto}")' in build
    assert 'instalador.join("productos")' in build and 'instalador.join("iconos")' in build


def test_armar_py_rechaza_una_marca_incompleta_o_de_otro_producto(salida, tmp_path):
    armar = [sys.executable, str(RAIZ / "escritorio" / "armar.py")]
    _armar(salida)
    dos = subprocess.run(armar + ["--marca", str(salida), "ventalibra", "restolibra"], capture_output=True, text=True)
    assert dos.returncode != 0 and "un solo producto" in dos.stderr
    otro = subprocess.run(armar + ["--marca", str(salida), "restolibra"], capture_output=True, text=True)
    assert otro.returncode != 0 and "no es del producto" in otro.stderr
    (salida / "icono.ico").unlink()
    falta = subprocess.run(armar + ["--marca", str(salida), "ventalibra"], capture_output=True, text=True)
    assert falta.returncode != 0 and "icono.ico" in falta.stderr


def test_la_carpeta_de_la_marca_no_se_commitea():
    assert "instalador/marca-blanca/" in (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_la_pagina_de_puestos_no_dice_libraedge_a_mano():
    html = (RAIZ / "escritorio" / "paginas" / "index.html").read_text(encoding="utf-8")
    texto = re.sub(r"<title>.*?</title>", "", html, flags=re.S)
    assert "LibraEdge" not in texto
