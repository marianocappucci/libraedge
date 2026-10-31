"""Las herramientas del central: `libraedge-central`.

El aprovisionamiento de nodos (``libraedge.aprovisionamiento``, ADR-008)::

    libraedge-central nodo publicar
    libraedge-central nodo sucursales
    libraedge-central nodo registrar <node_id> [--sucursal ID|NOMBRE]
        [--carga carga-nodo.json --central-url https://...] [--config-emisor RUTA]
    libraedge-central nodo dar-de-baja <node_id>
    libraedge-central nodo estado
    libraedge-central nodo vigilar [--umbral 15]
    libraedge-central nodo conservar --a nodos.json
    libraedge-central nodo restaurar --de nodos.json

Lo del producto sale de ``--aprovisionamiento`` o de
``LIBRAEDGE_APROVISIONAMIENTO`` (``modulo:objeto``, un ``Aprovisionamiento``);
``estado``, ``dar-de-baja``, ``vigilar`` y ``conservar`` no lo necesitan. Sin ``--base`` ni
``LIBRAEDGE_DATABASE_URL``, se usa el ``conectar()`` del aprovisionamiento o
``<PREFIJO>_DATABASE_URL``.

Con ``--carga``, ``registrar`` no imprime el entorno del nodo: escribe la
**carga del nodo** (permisos 600, sin pisar un archivo que exista) con ese
entorno, la URL del central y los datos del emisor del ``config.json`` de la
instancia (``$DATA_DIR/config.json``). Es el archivo que se le da al
instalador del nodo (ADR-010): el secreto no pasa por la pantalla ni por el
portapapeles.

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

from libraedge.instalador import (
    CLAVES_DEL_EMISOR_NECESARIAS,
    ErrorDeInstalacion,
    armar_carga,
    emisor_desde_config,
    escribir_carga,
    marca_desde_config,
    normalizar_url_del_central,
    ruta_config_del_central,
    validar_emisor,
    validar_marca,
)


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
    registrar.add_argument("--carga", default=None,
                           help="escribe la carga del nodo para el instalador en vez de imprimir el entorno")
    registrar.add_argument("--central-url", default=None,
                           help="con --carga: la URL publica de esta instancia (https://...)")
    registrar.add_argument("--config-emisor", default=None,
                           help="con --carga: el config.json de la instancia (default: $DATA_DIR/config.json)")
    baja = acciones.add_parser("dar-de-baja", help="revoca un nodo (PC robada o reemplazada)")
    baja.add_argument("node_id")
    acciones.add_parser("estado", help="que hay publicado y que nodos existen")
    conservar = acciones.add_parser(
        "conservar", help="guarda los nodos y el cursor del changelog en un archivo (antes de recrear la base)")
    conservar.add_argument("--a", dest="archivo", required=True,
                           help="el archivo a crear (permisos 600; no pisa uno que exista; tiene los hashes de los "
                                "secretos)")
    restaurar = acciones.add_parser(
        "restaurar", help="reinserta los nodos de `conservar`, sube el cursor del changelog y vuelve a publicar")
    restaurar.add_argument("--de", dest="archivo", required=True, help="el archivo que escribio `conservar`")
    vigilar = acciones.add_parser("vigilar", help="sale con 1 si algun nodo activo dejo de dar senales")
    vigilar.add_argument("--umbral", type=int, default=15,
                         help="minutos sin noticias para darlo por callado (default: 15)")


#: Lo que no necesita saber nada del producto: solo lee y escribe `node_identity`.
_SIN_PRODUCTO = {"estado", "dar-de-baja", "vigilar", "conservar"}


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
            if args.accion == "conservar":
                return ap.conservar(conexion, args.archivo)
            if args.accion == "restaurar":
                return ap.restaurar(conexion, aprov, args.archivo)
            preparada = _preparar_carga(args)
            alta = ap.registrar(conexion, aprov, args.node_id, args.sucursal,
                                interactivo=ap.es_interactivo())
        except (ap.ErrorDeAprovisionamiento, ErrorDeInstalacion) as exc:
            # Quien corre esto esta instalando en un local: el mensaje le dice
            # que hacer, un traceback no.
            print(f"\nERROR: {exc}\n", file=sys.stderr)
            return 1
        if preparada is not None:
            return _escribir_carga(alta, aprov, *preparada)
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


def _preparar_carga(args):
    """Lo que se puede validar ANTES de registrar: un alta emite un secreto, y si despues no se puede escribir la
    carga hay que volver a registrar. ``None`` sin ``--carga``."""
    if not args.carga:
        if args.central_url or args.config_emisor:
            raise ErrorDeInstalacion("--central-url y --config-emisor van con --carga.")
        return None
    if not args.central_url:
        raise ErrorDeInstalacion("Con --carga hace falta --central-url (la URL publica de esta instancia).")
    url = normalizar_url_del_central(args.central_url)
    if os.path.exists(args.carga):
        raise ErrorDeInstalacion(f"{args.carga} ya existe: no se pisa (puede ser la carga de otro nodo).")
    ruta_config = args.config_emisor or ruta_config_del_central()
    emisor = emisor_desde_config(ruta_config)
    if emisor is not None:
        validar_emisor(emisor)  # un CUIT mal cargado se ve ahora y no despues de emitir el secreto
    marca = marca_desde_config(ruta_config)
    if marca is not None:
        validar_marca(marca)  # y un logo que no es PNG, tambien
    return url, emisor, ruta_config, args.carga


def _escribir_carga(alta, aprov, url, emisor, ruta_config, ruta) -> int:
    entorno = dict(linea.split("=", 1) for linea in alta.entorno_del_nodo())
    try:
        carga = armar_carga(aprov.prefijo.lower(), entorno, url, emisor,
                            {"id": str(alta.sucursal.id), "nombre": alta.sucursal.nombre or ""},
                            marca_desde_config(ruta_config))
        escribir_carga(ruta, carga)
    except (ErrorDeInstalacion, OSError) as exc:
        print(f"\nERROR: el nodo {alta.node_id!r} quedo registrado pero no se pudo escribir la carga: {exc}\n"
              "Volver a registrar con el mismo node_id (emite un secreto nuevo e invalida este).", file=sys.stderr)
        return 1
    print(f"Nodo {alta.node_id!r} registrado en la sucursal {alta.sucursal.nombre!r} "
          f"(id {alta.sucursal.id}), prefijo {alta.prefijo}.")
    print(f"Carga del nodo escrita en {ruta} (permisos 600). Tiene el secreto: llevarla a la PC del local,")
    print("instalar con ella y borrarla de aca y de donde se haya copiado.")
    if emisor is None:
        print(f"AVISO: no hay {ruta_config}: la carga va sin los datos del emisor y el nodo no va a poder facturar.")
    else:
        faltan = [c for c in CLAVES_DEL_EMISOR_NECESARIAS if not str(emisor.get(c, "")).strip()]
        if faltan:
            print(f"AVISO: el config.json no tiene {', '.join(faltan)}: el nodo no va a poder facturar.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
