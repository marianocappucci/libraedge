"""Un central mínimo de prueba para el CI de los instaladores (ADR-014): https de verdad y las rutas del motor.

``publicar-instalador.yml`` lo levanta en el runner (Windows y Ubuntu) para que el instalador se instale **con el
código de vinculación**, como en un local, contra el router de ``libraedge.pantalla_del_central``: no es una imitación
de la ruta de canje, es la misma, sobre SQLite y con un producto de juguete (una sucursal, una tabla de referencia).

    uv run --extra server --with trustme python tests/central_de_prueba.py --producto ventalibra --dir <carpeta>

Deja en ``<carpeta>``: ``ca.pem`` (la autoridad que firmó el certificado de ``localhost``, para instalarla en el
almacén del sistema del runner), ``central.db`` y ``listo`` cuando ya atiende. Rutas:

- ``POST /api/nodos-offline/vinculacion`` y el resto de ``create_nodos_router`` (sin guarda: es una prueba);
- ``POST /sync/v1/vinculacion``, el canje (``create_canje_router``), con su límite de intentos;
- ``POST /prueba/secreto`` ``{"node_id", "secreto"}`` -> ``{"ok": bool}``: si el secreto que quedó en el
  ``nodo.env`` es el que verifica el central. Sólo existe acá.

Datos ficticios: «Comercio de Prueba SA», CUIT 20-12345678-6 (dígito verificador válido, de nadie).
"""


import argparse
import contextlib
import json
import os
import sqlite3
from pathlib import Path

CUIT_FICTICIO = "20-12345678-6"


def armar_app(carpeta: Path, producto: str, central_url: str):
    """La app del central de prueba sobre ``carpeta/central.db``. La prueba también la suite."""
    from fastapi import FastAPI
    from pydantic import BaseModel

    from libraedge import aprovisionamiento as ap
    from libraedge.db.repository import NodeRepository
    from libraedge.db.schema import init_schema
    from libraedge.pantalla_del_central import create_canje_router, create_nodos_router

    carpeta.mkdir(parents=True, exist_ok=True)
    base = carpeta / "central.db"
    with contextlib.closing(sqlite3.connect(base)) as conexion:
        init_schema(conexion)
        conexion.execute("CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                         " active INTEGER NOT NULL)")
        conexion.execute("INSERT OR IGNORE INTO branches VALUES (1, 'Sucursal de Prueba', 1)")
        conexion.execute("CREATE TABLE IF NOT EXISTS categorias (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL)")
        conexion.commit()
    config = carpeta / "config.json"
    config.write_text(json.dumps({"empresa_nombre": "Comercio de Prueba SA", "empresa_cuit": CUIT_FICTICIO,
                                  "empresa_iva_condition": "RI"}), encoding="utf-8")

    @contextlib.contextmanager
    def abrir():
        conexion = sqlite3.connect(base, timeout=30, check_same_thread=False)
        try:
            yield conexion
        finally:
            conexion.close()

    def sucursales(conn):
        return [{"id": f[0], "nombre": f[1], "activa": bool(f[2])}
                for f in conn.execute("SELECT id, name, active FROM branches ORDER BY id").fetchall()]

    aprov = ap.Aprovisionamiento(prefijo=producto.upper(), tablas_de_referencia=["categorias"],
                                 listar_sucursales=sucursales)
    entorno = {"LIBRAEDGE_CENTRAL_URL": central_url}
    app = FastAPI(title="Central de prueba de LibraEdge")
    app.include_router(create_nodos_router(aprov, abrir, producto=producto, ruta_config=lambda: str(config),
                                           entorno=entorno))
    app.include_router(create_canje_router(aprov, abrir, producto=producto, ruta_config=lambda: str(config),
                                           entorno=entorno))

    class Secreto(BaseModel):
        node_id: str
        secreto: str

    @app.post("/prueba/secreto")
    def verificar_secreto(cuerpo: Secreto):
        with abrir() as conn:
            return {"ok": NodeRepository(conn).verify_node_secret(cuerpo.node_id, cuerpo.secreto)}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--producto", required=True)
    parser.add_argument("--dir", required=True, help="donde quedan ca.pem, central.db y listo")
    parser.add_argument("--puerto", type=int, default=8443)
    args = parser.parse_args(argv)

    import trustme
    import uvicorn

    carpeta = Path(args.dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    autoridad = trustme.CA(organization_name="LibraEdge prueba de CI")
    certificado = autoridad.issue_cert("localhost", "127.0.0.1")
    autoridad.cert_pem.write_to_path(str(carpeta / "ca.pem"))
    certificado.private_key_and_cert_chain_pem.write_to_path(str(carpeta / "servidor.pem"))
    app = armar_app(carpeta, args.producto, f"https://localhost:{args.puerto}")

    listo = carpeta / "listo"
    if listo.exists():
        listo.unlink()

    class Servidor(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            listo.write_text(str(os.getpid()), encoding="utf-8")

    servidor = Servidor(uvicorn.Config(app, host="127.0.0.1", port=args.puerto, log_level="info",
                                       ssl_certfile=str(carpeta / "servidor.pem")))
    servidor.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
