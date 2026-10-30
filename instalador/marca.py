"""Arma la MARCA BLANCA del instalador y de la app de escritorio de un producto (ADR-029).

    python instalador/marca.py ventalibra --nombre "Comercial Modelo" --editor "Comercial Modelo SRL" --logo logo.png

Deja en `instalador/marca-blanca/` (o en `--salida`) todo lo que cambia cuando el revendedor vende el nodo con su
nombre:

- `producto.json`   la definicion del producto con `nombre` = la marca (el acceso directo, el `.exe` de
escritorio, la ventana y
                    el asistente salen de ahi: ADR-013 y ADR-015). Lo demas, igual: el `app_id` no se cambia nunca.
- `icono.ico/.png`  el icono de la marca (del logo), para el acceso directo, la app de escritorio y el instalador.
- `marca.svg`       la marca de las pantallas propias de la app (el logo, embebido).
- `marca.json`      lo que se armo (formato `libraedge-marca/1`) y el identificador de la app.
- `marca.isi`       las definiciones que lee `nodo.iss` con `/DConMarca` (UTF-8 con BOM: el nombre puede llevar
acentos).

Despues, en Windows (ver el README, seccion "Marca blanca"):

    python escritorio/armar.py ventalibra --marca instalador/marca-blanca
    ISCC.exe /DProducto=ventalibra /DConMarca nodo.iss

**Lo que cambia y lo que no.** Cambia lo que ve el cajero: nombre, icono, acceso directo, ventana, programa en
"Agregar o quitar
programas", editor y nombre del archivo. **No cambia** (a proposito, ADR-029): los servicios, las carpetas de
datos, el `AppId`, las
reglas de firewall y la tarea de respaldo. Renombrarlos rompe la reinstalacion encima, y solo los ve quien
administra la PC.

Solo se usa al armar (necesita Pillow, que ya esta en las dependencias de desarrollo); el instalador no lleva
nada de esto.
"""

import argparse
import base64
import io
import json
import pathlib
import re
import sys
import unicodedata

AQUI = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import armar_icono  # noqa: E402  (el mismo armado de .ico que usan los iconos de los productos)

PRODUCTOS = AQUI / "productos"
SALIDA = AQUI / "marca-blanca"
FORMATO = "libraedge-marca/1"

#: Lo que entra en el nombre: va a un nombre de archivo de Windows, a un `.lnk`, a un `taskkill /IM
#: "<nombre>.exe"` y a un `.isi`.
#: Letras (con acentos), numeros, espacio y `. _ -`. Sin comillas, barras, `&`, `%`, `{`, `;` ni nada que un
#: shell o el preprocesador
#: de Inno Setup interprete.
_PERMITIDOS = r"A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ"
NOMBRE = re.compile(rf"[{_PERMITIDOS}](?:[{_PERMITIDOS} ._-]{{0,38}}[{_PERMITIDOS}])?")
EDITOR = re.compile(rf"[{_PERMITIDOS}](?:[{_PERMITIDOS} ._,&-]{{0,58}}[{_PERMITIDOS}.])?")
#: Windows no acepta estos nombres de archivo, con o sin extension.
RESERVADOS = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}
MAX_LOGO = 2_000_000
LADOS = armar_icono.TAMANOS


class MarcaInvalida(ValueError):
    pass


def validar_nombre(nombre: str) -> str:
    nombre = " ".join(nombre.split())
    if not NOMBRE.fullmatch(nombre):
        raise MarcaInvalida(
            "El nombre va de 1 a 40 caracteres: letras, numeros, espacio y . _ - (empieza y termina en letra o numero)."
        )
    if nombre.lower() in RESERVADOS:
        raise MarcaInvalida(f"'{nombre}' es un nombre reservado de Windows.")
    return nombre


def validar_editor(editor: str) -> str:
    editor = " ".join(editor.split())
    if not EDITOR.fullmatch(editor):
        raise MarcaInvalida("El editor va de 1 a 60 caracteres: letras, numeros, espacio y . , & _ -")
    return editor


def slug(nombre: str) -> str:
    """El nombre en minusculas ASCII con guiones: para el nombre del instalador y el identificador de la app."""
    ascii_ = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-") or "marca"


def _png_cuadrado(logo: bytes, lado: int) -> bytes:
    from PIL import Image

    imagen = Image.open(io.BytesIO(logo)).convert("RGBA")
    # El logo entra entero (sin recortar) y centrado en un lienzo transparente cuadrado.
    imagen.thumbnail((lado, lado), Image.LANCZOS)
    lienzo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    lienzo.paste(imagen, ((lado - imagen.width) // 2, (lado - imagen.height) // 2), imagen)
    salida = io.BytesIO()
    lienzo.save(salida, format="PNG")
    return salida.getvalue()


def _svg(png: bytes) -> str:
    dato = base64.b64encode(png).decode()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 256 256" '
        'width="256" height="256"><image width="256" height="256" href="data:image/png;base64,' + dato + '" '
        'xlink:href="data:image/png;base64,' + dato + '"/></svg>\n'
    )


def _isi(nombre: str, editor: str, nombre_salida: str) -> str:
    """Las definiciones de `nodo.iss` (ADR-029). Los valores ya pasaron `validar_*`: no llevan comillas ni nada
    de ISPP."""
    return (
        "; Generado por instalador/marca.py: NO se edita ni se commitea (marca-blanca/ esta en .gitignore).\n"
        f'#define NombreProducto "{nombre}"\n'
        f'#define Editor "{editor}"\n'
        '#define NombreNodo "Nodo sin internet"\n'
        '#define PrefijoSalida "nodo-"\n'
        f'#define NombreSalida "{nombre_salida}"\n'
        '#define DirProducto "marca-blanca"\n'
        '#define ArchivoProducto "producto"\n'
        '#define DirIconos "marca-blanca"\n'
        '#define ArchivoIcono "icono"\n'
        '#define IconoInstalador "marca-blanca\\icono.ico"\n'
    )


def armar(producto: str, nombre: str, editor: str, logo: bytes, salida: pathlib.Path) -> dict:
    """Escribe la marca en `salida` y devuelve `marca.json`. Lanza `MarcaInvalida` si algo no sirve; no escribe
    nada en ese caso."""
    ruta = PRODUCTOS / f"{producto}.json"
    if not re.fullmatch(r"[a-z]+", producto) or not ruta.is_file():
        raise MarcaInvalida(f"No hay definicion para '{producto}' en {PRODUCTOS}.")
    nombre, editor = validar_nombre(nombre), validar_editor(editor)
    if len(logo) > MAX_LOGO:
        raise MarcaInvalida("El logo pesa mas de 2 MB.")
    try:
        png_256 = _png_cuadrado(logo, 256)
        pngs = {lado: _png_cuadrado(logo, lado) for lado in LADOS}
    except Exception as exc:  # Pillow no lee el archivo: no es una imagen
        raise MarcaInvalida(f"El logo no es una imagen que se pueda leer ({exc.__class__.__name__}).") from exc

    definicion = json.loads(ruta.read_text(encoding="utf-8"))
    definicion["nombre"] = nombre
    definicion["_marca"] = (
        "marca blanca (instalador/marca.py): solo cambia `nombre`; el app_id y el resto son los del producto"
    )
    identificador = f"nodo.{slug(nombre)}"
    resumen = {
        "formato": FORMATO,
        "producto": producto,
        "nombre": nombre,
        "editor": editor,
        "slug": slug(nombre),
        "identificador": identificador,
    }

    salida.mkdir(parents=True, exist_ok=True)
    (salida / "producto.json").write_text(json.dumps(definicion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (salida / "icono.ico").write_bytes(armar_icono.ico(pngs))
    (salida / "icono.png").write_bytes(png_256)
    (salida / "marca.svg").write_text(_svg(png_256), encoding="utf-8")
    (salida / "marca.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # UTF-8 con BOM: sin el, ISCC lee el archivo con la pagina de codigos del sistema y un acento sale roto.
    (salida / "marca.isi").write_bytes(b"\xef\xbb\xbf" + _isi(nombre, editor, slug(nombre)).encode("utf-8"))
    return resumen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("producto")
    parser.add_argument("--nombre", required=True, help="el nombre con el que el revendedor vende el sistema")
    parser.add_argument("--editor", required=True, help="el editor que figura en 'Agregar o quitar programas'")
    parser.add_argument(
        "--logo", type=pathlib.Path, required=True, help="PNG, JPEG o WEBP del logo (se centra en un cuadrado)"
    )
    parser.add_argument("--salida", type=pathlib.Path, default=SALIDA)
    args = parser.parse_args()
    try:
        resumen = armar(args.producto, args.nombre, args.editor, args.logo.read_bytes(), args.salida)
    except (MarcaInvalida, OSError) as exc:
        sys.exit(f"marca.py: {exc}")
    print(f"Marca '{resumen['nombre']}' ({resumen['producto']}) en {args.salida}")


if __name__ == "__main__":
    main()
