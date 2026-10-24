"""Compila la app de escritorio de uno o varios productos (ADR-015).

    python escritorio/armar.py [--version X.Y.Z] [--salida DIR] contalibra ventalibra restolibra libracargo libraclub

Un solo codigo fuente; el producto se elige al compilar (`LIBRAEDGE_PRODUCTO`, que lee `build.rs`): nombre,
icono, identificador y la definicion del instalador. Deja en DIR (por defecto `escritorio/salida`):

- Windows: `<producto>.exe` (el `.iss` lo instala como `{app}\\escritorio\\<Nombre>.exe`).
- Linux:   `libraedge-nodo-<producto>` (el `.deb` lo instala en `/opt/libraedge-nodo/escritorio/`; el nombre es
  el del `.desktop`, asi GNOME agrupa la ventana con el favorito del dock).

Despues de compilar cada uno, corre `--version` y verifica que sea ese producto (todos se compilan en el
mismo `target/`: si cargo no recompilara el crate, saldria el nombre del anterior).

Firma de codigo (todavia no hay certificado): si `LIBRAEDGE_FIRMA_CMD` tiene un comando con `{archivo}` (por
ejemplo `signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f cert.pfx /p ... {archivo}`), se
corre sobre cada binario de Windows antes de copiarlo. Sin la variable no se firma.

Hace falta: Rust estable (rustup) y, en Linux, las bibliotecas de desarrollo de WebKitGTK 4.1
(`libwebkit2gtk-4.1-dev`, ver el README del instalador).
"""

import argparse
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

AQUI = pathlib.Path(__file__).resolve().parent
PRODUCTOS = AQUI.parent / "instalador" / "productos"


def nombre_del_binario(producto: str, windows: bool) -> str:
    return f"{producto}.exe" if windows else f"libraedge-nodo-{producto}"


def armar(producto: str, version: str, salida: pathlib.Path, windows: bool) -> pathlib.Path:
    definicion = json.loads((PRODUCTOS / f"{producto}.json").read_text(encoding="utf-8"))
    entorno = dict(os.environ, LIBRAEDGE_PRODUCTO=producto, LIBRAEDGE_VERSION=version)
    print(f"== {definicion['nombre']} {version}", flush=True)
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=AQUI, env=entorno, check=True)
    compilado = AQUI / "target" / "release" / ("libraedge-escritorio.exe" if windows else "libraedge-escritorio")
    firma = os.environ.get("LIBRAEDGE_FIRMA_CMD", "").strip()
    if firma and windows:
        partes = [p.replace("{archivo}", str(compilado)) for p in shlex.split(firma, posix=False)]
        subprocess.run(partes, check=True)
        print("   firmado", flush=True)
    salida.mkdir(parents=True, exist_ok=True)
    destino = salida / nombre_del_binario(producto, windows)
    shutil.copy2(compilado, destino)
    if not windows:
        destino.chmod(0o755)
    texto = subprocess.run([str(destino), "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    print(f"   {destino.name}: {texto}", flush=True)
    if not texto.startswith(f"{definicion['nombre']} {version} ") or f"producto {producto}," not in texto:
        sys.exit(f"{destino} no es {definicion['nombre']} {version}: '{texto}'")
    return destino


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("productos", nargs="+")
    parser.add_argument("--version", default="0.0.0")
    parser.add_argument("--salida", type=pathlib.Path, default=AQUI / "salida")
    args = parser.parse_args()
    windows = sys.platform == "win32"
    for producto in args.productos:
        if not (PRODUCTOS / f"{producto}.json").is_file():
            sys.exit(f"No hay definicion para '{producto}' en {PRODUCTOS}.")
        destino = armar(producto, args.version, args.salida.resolve(), windows)
        print(f"   {destino} ({destino.stat().st_size // 1024} KB)", flush=True)


if __name__ == "__main__":
    main()
