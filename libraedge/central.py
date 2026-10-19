"""Las herramientas del central: `libraedge-central`.

Por ahora, la revision manual (``libraedge.sync.revision``)::

    libraedge-central revision listar [--nodo N] [--todas]
    libraedge-central revision reintentar <operation_id> --por <quien> \\
        --despachador app.sincronizacion:despachador
    libraedge-central revision descartar <operation_id> --por <quien> --motivo "..."

La base sale de ``--base`` o de ``LIBRAEDGE_DATABASE_URL``; el despachador, de
``--despachador`` o de ``LIBRAEDGE_DESPACHADOR`` (``modulo:objeto``, el mismo
que el producto le pasa a ``create_sync_router``). Corre en el servidor del
central, con el entorno de la instancia: ningun secreto pasa por la linea de
comandos.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from importlib import import_module


def _base(args) -> str:
    base = args.base or os.environ.get("LIBRAEDGE_DATABASE_URL")
    if not base:
        raise SystemExit("Falta la base: pasar --base o definir LIBRAEDGE_DATABASE_URL.")
    return base


def cargar_objeto(ruta: str):
    """``"paquete.modulo:objeto"`` -> el objeto."""
    modulo, _, nombre = (ruta or "").partition(":")
    if not modulo or not nombre:
        raise SystemExit(f"{ruta!r} no es 'modulo:objeto'.")
    return getattr(import_module(modulo), nombre)


def _conectar(base: str):
    from libracore.db import core  # noqa: PLC0415 - solo en el central

    core.configure(base)
    return core.get_connection()


def main(argv: list[str] | None = None, conectar=_conectar) -> int:
    parser = argparse.ArgumentParser(
        prog="libraedge-central", description="Herramientas del central de LibraEdge.")
    parser.add_argument("--base", default=None, help="URL de la base (o LIBRAEDGE_DATABASE_URL)")
    sub = parser.add_subparsers(dest="comando", required=True)
    revision = sub.add_parser("revision", help="operaciones que el central no pudo aplicar")
    acciones = revision.add_subparsers(dest="accion", required=True)

    listar = acciones.add_parser("listar", help="las operaciones en revision manual")
    listar.add_argument("--nodo", default=None, help="solo las de este node_id")
    listar.add_argument("--todas", action="store_true", help="incluir las descartadas")

    reintentar = acciones.add_parser("reintentar", help="volver a aplicarla")
    reintentar.add_argument("operation_id")
    reintentar.add_argument("--por", required=True, help="quien la reintenta")
    reintentar.add_argument("--despachador", default=None,
                            help="modulo:objeto (o LIBRAEDGE_DESPACHADOR)")

    descartar = acciones.add_parser("descartar", help="cerrarla sin aplicarla")
    descartar.add_argument("operation_id")
    descartar.add_argument("--por", required=True, help="quien la descarta")
    descartar.add_argument("--motivo", required=True, help="por que no se aplica")

    args = parser.parse_args(argv)

    from libraedge.sync import revision as rev

    conexion = conectar(_base(args))
    try:
        if args.accion == "listar":
            for operacion in rev.listar_en_revision(
                    conexion, node_id=args.nodo, incluir_descartadas=args.todas):
                print(json.dumps(asdict(operacion), ensure_ascii=False, default=str))
            return 0
        try:
            if args.accion == "reintentar":
                ruta = args.despachador or os.environ.get("LIBRAEDGE_DESPACHADOR")
                if not ruta:
                    raise SystemExit(
                        "Falta el despachador: pasar --despachador modulo:objeto o "
                        "definir LIBRAEDGE_DESPACHADOR.")
                resultado = rev.reintentar(
                    conexion, args.operation_id, cargar_objeto(ruta), por=args.por)
                print(json.dumps({"operation_id": args.operation_id,
                                  "result": resultado.result, "error": resultado.error},
                                 ensure_ascii=False))
                return 0 if resultado.result == "accepted" else 1
            rev.descartar(conexion, args.operation_id, motivo=args.motivo, por=args.por)
            print(f"{args.operation_id}: descartada")
            return 0
        except (LookupError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        conexion.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
