"""Arma instalador/libraedge.ico desde las dos marcas de libra-ui (ADR-078 del kit).

    python instalador/armar_icono.py <ruta a chrome o chromium>

De 16 a 32 px va la PC sola (`marca/libraedge-favicon.svg`): a ese tamano la marca completa se
empasta. De 40 en adelante, la marca completa (`marca/libraedge.svg`). Cada tamano es un PNG con
transparencia dentro del ICO, que Windows lee desde Vista. Chrome rasteriza el SVG para no sumar
una dependencia de imagenes solo para esto; el `.ico` se commitea, asi que el instalador no
necesita nada. Si cambia la marca en libra-ui, se copian los dos SVG a `marca/` y se vuelve a correr.
"""

import pathlib
import struct
import subprocess
import sys
import tempfile

AQUI = pathlib.Path(__file__).resolve().parent
CHICOS = (16, 20, 24, 32)
GRANDES = (40, 48, 64, 128, 256)
TAMANOS = CHICOS + GRANDES


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


def ico(pngs: dict[int, bytes]) -> bytes:
    cabecera = struct.pack("<HHH", 0, 1, len(pngs))
    entradas, datos = b"", b""
    desplazamiento = 6 + 16 * len(pngs)
    for lado, png in sorted(pngs.items()):
        # En el directorio del ICO, 256 se escribe 0.
        entradas += struct.pack("<BBBBHHII", lado % 256, lado % 256, 0, 0, 1, 32, len(png), desplazamiento + len(datos))
        datos += png
    return cabecera + entradas + datos


def main() -> None:
    chrome = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        pngs = {
            lado: rasterizar(
                chrome,
                AQUI / "marca" / ("libraedge-favicon.svg" if lado in CHICOS else "libraedge.svg"),
                lado,
                pathlib.Path(tmp) / f"{lado}.png",
            )
            for lado in TAMANOS
        }
    (AQUI / "libraedge.ico").write_bytes(ico(pngs))
    print(f"libraedge.ico: {len(pngs)} tamaños")


if __name__ == "__main__":
    main()
