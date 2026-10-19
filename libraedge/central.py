"""Las herramientas del central: `libraedge-central`.

El aprovisionamiento de nodos (``libraedge.aprovisionamiento``, ADR-008)::

    libraedge-central nodo publicar
    libraedge-central nodo sucursales
    libraedge-central nodo registrar <node_id> [--sucursal ID|NOMBRE]
    libraedge-central nodo dar-de-baja <node_id>
    libraedge-central nodo estado
    libraedge-central nodo vigilar [--umbral 15]

Lo del producto sale de ``--aprovisionamiento`` o de
``LIBRAEDGE_APROVISIONAMIENTO`` (``modulo:objeto``, un ``Aprovisionamiento``);
``estado``, ``dar-de-baja`` y ``vigilar`` no lo necesitan. Sin ``--base`` ni
``LIBRAEDGE_DATABASE_URL``, se usa el ``conectar()`` del aprovisionamiento o
``<PREFIJO>_DATABASE_URL``.

Y la revision manual (``libraedge.sync.revision``)::

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


def _base(args, prefijo: str | None = None) -> str:
    base = args.base or os.environ.get("LIBRAEDGE_DATABASE_URL")
    if not base and prefijo:
        base = os.environ.get(f"{prefijo}_DATABASE_URL")
    if not base:
        otra = f", {prefijo}_DATABASE_URL" if prefijo else ""
        raise SystemExit(f"Falta la base: pasar --base o definir LIBRAEDGE_DATABASE_URL{otra}.")
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
    _parser_de_nodo(sub)
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
    if args.comando == "nodo":
        return _nodo(args, conectar)

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


def _parser_de_nodo(sub) -> None:
    nodo = sub.add_parser("nodo", help="aprovisionar los nodos offline del producto")
    nodo.add_argument("--aprovisionamiento", default=None,
                      help="modulo:objeto del producto (o LIBRAEDGE_APROVISIONAMIENTO)")
    acciones = nodo.add_subparsers(dest="accion", required=True)
    acciones.add_parser("publicar", help="instala los triggers del changelog y siembra la referencia")
    acciones.add_parser("sucursales", help="las sucursales de la instancia y si ya tienen nodo")
    registrar = acciones.add_parser("registrar", help="da de alta un nodo y emite su secreto")
    registrar.add_argument("node_id")
    registrar.add_argument("--sucursal", default=None,
                           help="id o nombre de una sucursal activa; sin ella: la unica activa, o un "
                                "menu si hay varias y hay terminal")
    baja = acciones.add_parser("dar-de-baja", help="revoca un nodo (PC robada o reemplazada)")
    baja.add_argument("node_id")
    acciones.add_parser("estado", help="que hay publicado y que nodos existen")
    vigilar = acciones.add_parser("vigilar", help="sale con 1 si algun nodo activo dejo de dar senales")
    vigilar.add_argument("--umbral", type=int, default=15,
                         help="minutos sin noticias para darlo por callado (default: 15)")


#: Lo que no necesita saber nada del producto: solo lee y escribe `node_identity`.
_SIN_PRODUCTO = {"estado", "dar-de-baja", "vigilar"}


def _nodo(args, conectar) -> int:
    from libraedge import aprovisionamiento as ap

    ruta = args.aprovisionamiento or os.environ.get("LIBRAEDGE_APROVISIONAMIENTO")
    if not ruta and args.accion not in _SIN_PRODUCTO:
        raise SystemExit(
            "Falta el aprovisionamiento del producto: pasar --aprovisionamiento modulo:objeto "
            "o definir LIBRAEDGE_APROVISIONAMIENTO.")
    try:
        aprov = ap.cargar_aprovisionamiento(ruta) if ruta else None
    except ap.ErrorDeAprovisionamiento as exc:
        raise SystemExit(str(exc)) from None

    if aprov is not None and aprov.conectar is not None and not (
            args.base or os.environ.get("LIBRAEDGE_DATABASE_URL")):
        conexion = aprov.conectar()
    else:
        conexion = conectar(_base(args, aprov.prefijo if aprov else None))
    try:
        if args.accion == "vigilar":
            return ap.vigilar(conexion, args.umbral)
        if args.accion == "estado":
            return ap.estado(conexion)
        if args.accion == "dar-de-baja":
            return ap.dar_de_baja(conexion, args.node_id)
        if args.accion == "sucursales":
            return ap.sucursales(conexion, aprov)
        try:
            if args.accion == "publicar":
                return ap.publicar(conexion, aprov)
            alta = ap.registrar(conexion, aprov, args.node_id, args.sucursal,
                                interactivo=ap.es_interactivo())
        except ap.ErrorDeAprovisionamiento as exc:
            # Quien corre esto esta instalando en un local: el mensaje le dice
            # que hacer, un traceback no.
            print(f"\nERROR: {exc}\n", file=sys.stderr)
            return 1
        print(f"Nodo {alta.node_id!r} registrado en la sucursal {alta.sucursal.nombre!r} "
              f"(id {alta.sucursal.id}), prefijo {alta.prefijo}.")
        print()
        for linea in alta.entorno_del_nodo():
            print("  " + linea)
        print()
        print("🔴 El secreto se muestra UNA SOLA VEZ: solo se guarda su hash. Copiarlo ahora al")
        print("   archivo de entorno del nodo. Si se pierde, hay que volver a registrar el nodo,")
        print("   lo que invalida el anterior.")
        return 0
    finally:
        conexion.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
