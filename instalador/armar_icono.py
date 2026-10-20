"""Arma los iconos de Windows desde las marcas de libra-ui (`marcas/*.svg`, ADR-078 del kit).

    python instalador/armar_icono.py <ruta a chrome o chromium>

- `libraedge.ico`: el del instalador y de "Agregar o quitar programas".
- `iconos/<producto>.ico`, uno por `productos/<producto>.json`: el del acceso directo que abre el producto en modo
  aplicacion (ADR-013). En Linux el `.desktop` usa el SVG de la marca tal cual (`marca/<producto>.svg`).
- `iconos/<producto>.png` (256 px, la misma entrada PNG del `.ico`): el icono de la ventana de la app de
  escritorio en Linux (ADR-015; Tauri lo pide en PNG RGBA). Sin Chrome a mano, sale del `.ico` con
  `python instalador/armar_icono.py --png-desde-ico`.

De 16 a 32 px va la version chica (`marca/<marca>-favicon.svg`): a ese tamano la marca completa se
empasta. De 40 en adelante, la marca completa (`marca/<marca>.svg`).

De 16 a 48 cada entrada es un BMP de 32 bits (BGRA, con la mascara AND), y de 64 en adelante un PNG.
Explorer lee los dos, pero hay lectores viejos (System.Drawing.Icon, medido en la VM Windows 10 el
2026-10-19) que dibujan basura con un PNG chico: los tamanos que se ven en listas y barras van en
el formato que lee cualquiera, y los grandes en PNG para que el archivo no pese de mas.

Chrome rasteriza el SVG para no sumar una dependencia de imagenes solo para esto, y el PNG se
decodifica con la biblioteca estandar; el `.ico` se commitea, asi que el instalador no necesita
nada. Si cambia una marca en libra-ui, se copian sus dos SVG a `marca/` y se vuelve a correr. Un
producto nuevo: su `.json` en `productos/` y sus dos SVG en `marca/`.
"""

import pathlib
import struct
import subprocess
import sys
import tempfile
import zlib

AQUI = pathlib.Path(__file__).resolve().parent
CHICOS = (16, 20, 24, 32)
GRANDES = (40, 48, 64, 128, 256)
TAMANOS = CHICOS + GRANDES
#: Hasta este lado la entrada va en BMP; arriba, en PNG.
MAX_BMP = 48


def rasterizar(chrome: str, svg: pathlib.Path, lado: int, destino: pathlib.Path) -> bytes:
    pagina = destino.with_suffix(".html")
    pagina.write_text(
        f'<html><body style="margin:0;background:transparent">'
        f'<img src="{svg.as_uri()}" width="{lado}" height="{lado}" style="display:block"></body></html>'
    )
    subprocess.run(
        [
            chrome,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",
            f"--window-size={lado},{lado}",
            f"--screenshot={destino}",
            pagina.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return destino.read_bytes()


def rgba_de_png(png: bytes) -> tuple[int, int, bytes]:
    """(ancho, alto, pixeles RGBA de arriba abajo) de un PNG RGBA de 8 bits sin entrelazar, como los de Chrome."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ancho, alto = 8, b"", 0, 0
    while pos < len(png):
        largo, tipo = struct.unpack(">I4s", png[pos : pos + 8])
        cuerpo = png[pos + 8 : pos + 8 + largo]
        if tipo == b"IHDR":
            ancho, alto, bits, color, _, _, entrelazado = struct.unpack(">IIBBBBB", cuerpo)
            assert (bits, color, entrelazado) == (8, 6, 0), "se espera RGBA de 8 bits sin entrelazar"
        elif tipo == b"IDAT":
            idat += cuerpo
        pos += 12 + largo
    crudo, paso, filas, previa = zlib.decompress(idat), ancho * 4, [], bytearray(ancho * 4)
    for y in range(alto):
        filtro, fila = crudo[y * (paso + 1)], bytearray(crudo[y * (paso + 1) + 1 : (y + 1) * (paso + 1)])
        for i in range(paso):
            a = fila[i - 4] if i >= 4 else 0
            b = previa[i]
            c = previa[i - 4] if i >= 4 else 0
            if filtro == 1:
                fila[i] = (fila[i] + a) & 0xFF
            elif filtro == 2:
                fila[i] = (fila[i] + b) & 0xFF
            elif filtro == 3:
                fila[i] = (fila[i] + (a + b) // 2) & 0xFF
            elif filtro == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                fila[i] = (fila[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        filas.append(bytes(fila))
        previa = fila
    return ancho, alto, b"".join(filas)


def bmp_de_icono(png: bytes) -> bytes:
    """La entrada BMP de un ICO: BITMAPINFOHEADER con el alto doble, BGRA de abajo arriba y la mascara AND."""
    ancho, alto, rgba = rgba_de_png(png)
    cabecera = struct.pack("<IiiHHIIiiII", 40, ancho, alto * 2, 1, 32, 0, ancho * alto * 4, 0, 0, 0, 0)
    pixeles, mascara = b"", b""
    paso_mascara = ((ancho + 31) // 32) * 4
    for y in reversed(range(alto)):
        fila = rgba[y * ancho * 4 : (y + 1) * ancho * 4]
        pixeles += b"".join(bytes((fila[i + 2], fila[i + 1], fila[i], fila[i + 3])) for i in range(0, len(fila), 4))
        bits = 0
        for x in range(ancho):
            bits = (bits << 1) | (1 if fila[x * 4 + 3] == 0 else 0)
        mascara += (bits << (paso_mascara * 8 - ancho)).to_bytes(paso_mascara, "big")
    return cabecera + pixeles + mascara


def ico(pngs: dict[int, bytes]) -> bytes:
    cabecera = struct.pack("<HHH", 0, 1, len(pngs))
    entradas, datos = b"", b""
    desplazamiento = 6 + 16 * len(pngs)
    for lado, png in sorted(pngs.items()):
        entrada = bmp_de_icono(png) if lado <= MAX_BMP else png
        # En el directorio del ICO, 256 se escribe 0.
        entradas += struct.pack(
            "<BBBBHHII", lado % 256, lado % 256, 0, 0, 1, 32, len(entrada), desplazamiento + len(datos)
        )
        datos += entrada
    return cabecera + entradas + datos


def png_de_ico(datos: bytes, lado: int = 256) -> bytes:
    """La entrada PNG de ese lado de un `.ico` armado por `ico()` (de 64 en adelante van en PNG)."""
    _, tipo, cantidad = struct.unpack("<HHH", datos[:6])
    assert tipo == 1, "no es un .ico"
    for i in range(cantidad):
        ancho, _, _, _, _, _, largo, desde = struct.unpack("<BBBBHHII", datos[6 + 16 * i : 22 + 16 * i])
        if (ancho or 256) == lado:
            png = datos[desde : desde + largo]
            assert png[:8] == b"\x89PNG\r\n\x1a\n", f"la entrada de {lado} px no es PNG"
            return png
    raise ValueError(f"el .ico no tiene una entrada de {lado} px")


def productos() -> list[str]:
    return sorted(ruta.stem for ruta in (AQUI / "productos").glob("*.json"))


def destinos() -> dict[str, pathlib.Path]:
    """La marca de la que sale cada icono, y adonde va."""
    return {"libraedge": AQUI / "libraedge.ico", **{p: AQUI / "iconos" / f"{p}.ico" for p in productos()}}


def armar(chrome: str, marca: str, destino: pathlib.Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        pngs = {
            lado: rasterizar(
                chrome,
                AQUI / "marca" / (f"{marca}-favicon.svg" if lado in CHICOS else f"{marca}.svg"),
                lado,
                pathlib.Path(tmp) / f"{lado}.png",
            )
            for lado in TAMANOS
        }
    destino.parent.mkdir(exist_ok=True)
    destino.write_bytes(ico(pngs))
    if marca != "libraedge":
        destino.with_suffix(".png").write_bytes(pngs[256])
    return len(pngs)


def main() -> None:
    if sys.argv[1:] == ["--png-desde-ico"]:
        for producto in productos():
            ruta = AQUI / "iconos" / f"{producto}.ico"
            ruta.with_suffix(".png").write_bytes(png_de_ico(ruta.read_bytes()))
            print(f"{ruta.with_suffix('.png').relative_to(AQUI)}: 256 px")
        return
    chrome = sys.argv[1]
    for marca, destino in destinos().items():
        print(f"{destino.relative_to(AQUI)}: {armar(chrome, marca, destino)} tamaños")


if __name__ == "__main__":
    main()
