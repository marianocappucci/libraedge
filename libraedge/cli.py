"""El proceso del nodo: `libraedge-nodo`.

Hasta la Fase 4 este paquete no tenia ni `__main__.py` ni `[project.scripts]`:
era una biblioteca que alguien tenia que invocar, y nadie lo hacia. Esto es lo
que se instala como servicio en la PC del cliente.

## Toda la configuracion viene del entorno

Es lo que el instalador escribe una vez y nadie vuelve a tocar. Nada se pasa por
linea de comandos salvo el intervalo, porque un servicio de Windows se define
con su linea de arranque y cambiarla despues es mas dificil que editar un
archivo de entorno.

| Variable | Que es |
|---|---|
| `LIBRAEDGE_NODE_ID` | El id de este nodo, el mismo con el que se registro en el central |
| `LIBRAEDGE_NODE_SECRET` | El secreto que devolvio `register_node()`, una sola vez |
| `LIBRAEDGE_CENTRAL_URL` | La base del central, sin la ruta |
| `LIBRAEDGE_DATABASE_URL` | La base del producto, donde vive el outbox |
| `LIBRAEDGE_TABLAS_ESPEJO` | `tabla:pk` separadas por coma. **Es el reparto de autoridad** |
| `LIBRAEDGE_ESTADO` | Donde escribir el estado para que lo lea una UI |

`LIBRAEDGE_TABLAS_ESPEJO` no tiene default a proposito: sin lista, el aplicador
aceptaria cualquier tabla que el central mande, y esa lista **es** la frontera
que impide que el nodo escriba en tablas de su propia autoridad. Un default
vacio la volveria opcional en la practica.
"""

import argparse
import os
import sys
import time


def _requerido(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        raise SystemExit(
            f"Falta la variable {nombre}. El nodo no arranca a medias: sin ella "
            f"sincronizaria contra el lugar equivocado o con la identidad "
            f"equivocada."
        )
    return valor


def tablas_espejo(crudo: str) -> dict[str, str]:
    """`"productos:id,clientes:id"` -> `{"productos": "id", "clientes": "id"}`.

    La `pk` se puede omitir y queda en `id`, que es la de casi todas las tablas
    de la familia.
    """
    tablas: dict[str, str] = {}
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        nombre, _, pk = parte.partition(":")
        tablas[nombre.strip()] = (pk.strip() or "id")
    if not tablas:
        raise SystemExit(
            "LIBRAEDGE_TABLAS_ESPEJO quedo vacia. Es el reparto de autoridad: "
            "sin ella el nodo aceptaria cambios sobre cualquier tabla suya."
        )
    return tablas


def construir_nodo():
    """Arma el nodo desde el entorno. Falla temprano y con un mensaje util."""
    from libracore.db import core  # noqa: PLC0415 - solo en el proceso del nodo

    from libraedge.db.repository import NodeRepository
    from libraedge.nodo import Nodo
    from libraedge.sync.http import HttpSyncTransport
    from libraedge.sync.pull import HttpPullTransport, MirrorApplier, PullWorker
    from libraedge.sync.worker import OutboxWorker

    node_id = _requerido("LIBRAEDGE_NODE_ID")
    secreto = _requerido("LIBRAEDGE_NODE_SECRET")
    central = _requerido("LIBRAEDGE_CENTRAL_URL")
    base = _requerido("LIBRAEDGE_DATABASE_URL")
    tablas = tablas_espejo(_requerido("LIBRAEDGE_TABLAS_ESPEJO"))

    core.configure(base)
    conexion = core.get_connection()
    repositorio = NodeRepository(conexion)

    return Nodo(
        repositorio, node_id,
        outbox_worker=OutboxWorker(repositorio, HttpSyncTransport(central, secreto)),
        pull_worker=PullWorker(
            repositorio,
            HttpPullTransport(central, node_id, secreto),
            MirrorApplier(conexion, tablas),
        ),
        ruta_estado=os.environ.get("LIBRAEDGE_ESTADO"),
    ), conexion


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="libraedge-nodo",
        description="El proceso del nodo offline: sube el outbox y espeja la referencia.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("sincronizar", help="un ciclo y sale")
    correr = sub.add_parser("correr", help="cicla hasta que lo paren (el servicio)")
    correr.add_argument(
        "--intervalo", type=int, default=60,
        help="segundos entre ciclos (default: 60)",
    )
    sub.add_parser("estado", help="imprime el ultimo estado escrito, sin sincronizar")
    bandeja = sub.add_parser(
        "bandeja", help="el icono de bandeja (Windows); --una-vez imprime y sale")
    bandeja.add_argument(
        "--una-vez", action="store_true",
        help="imprime lo que mostraria el tooltip y sale, sin abrir ninguna ventana",
    )
    bandeja.add_argument("--intervalo", type=int, default=60,
                         help="cada cuanto cicla el nodo, para decidir si esta caido")
    # 🔴 La ruta como ARGUMENTO, no solo por entorno. La bandeja no corre como
    # servicio --Windows no le deja dibujar a un servicio-- sino desde un acceso
    # directo del inicio de sesion del operador, y ese acceso directo no hereda
    # el entorno que NSSM le da a los servicios. Sin esto, la unica forma de que
    # la encuentre seria una variable de entorno de MAQUINA, que ensucia el
    # sistema entero para un dato de una sola aplicacion.
    bandeja.add_argument(
        "--estado", default=None,
        help="ruta del estado.json; si falta, se usa LIBRAEDGE_ESTADO",
    )

    args = parser.parse_args(argv)

    if args.comando == "bandeja":
        # Igual que `estado`: no arma el nodo ni toca la base. La bandeja tiene
        # que poder decir "el servicio no esta corriendo" cuando justamente no
        # esta corriendo.
        from datetime import datetime, timezone

        from libraedge.bandeja import resumen_para_la_bandeja
        from libraedge.nodo import leer_estado

        ruta = args.estado or os.environ.get("LIBRAEDGE_ESTADO")
        if not ruta:
            raise SystemExit(
                "No se sabe que estado mostrar: pasar --estado <ruta> o definir "
                "LIBRAEDGE_ESTADO."
            )

        if args.una_vez:
            resumen = resumen_para_la_bandeja(
                leer_estado(ruta), datetime.now(timezone.utc), args.intervalo)
            print(f"[{resumen.severidad}] {resumen.titulo}")
            if resumen.detalle:
                print(f"  {resumen.detalle}")
            return 0 if resumen.severidad == "ok" else 1

        from libraedge.bandeja_windows import correr

        return correr(ruta, intervalo_segundos=args.intervalo)

    if args.comando == "estado":
        # No arma el nodo ni toca la base: tiene que poder contestar con el
        # servicio caido, que es justamente cuando alguien lo pregunta.
        from libraedge.nodo import leer_estado

        ruta = os.environ.get("LIBRAEDGE_ESTADO")
        if not ruta:
            raise SystemExit("Falta LIBRAEDGE_ESTADO: no hay estado que leer.")
        estado = leer_estado(ruta)
        if estado is None:
            print('{"estado": "sin sincronizar todavia"}')
            return 1
        print(estado.como_json())
        return 0

    nodo, conexion = construir_nodo()
    try:
        if args.comando == "sincronizar":
            estado = nodo.sincronizar()
            print(estado.como_json())
            # Sale distinto segun el resultado para que un cron o un servicio
            # puedan alertar sin parsear el JSON.
            return 0 if estado.en_linea else 1

        while True:
            estado = nodo.sincronizar()
            estado_texto = "en linea" if estado.en_linea else "fuera de linea"
            print(
                f"[{estado.ultimo_intento}] {estado_texto} | "
                f"pendientes={estado.pendientes} subidas={estado.operaciones_subidas} "
                f"bajados={estado.cambios_bajados}"
                + (f" | {estado.ultimo_error}" if estado.ultimo_error else ""),
                flush=True,
            )
            time.sleep(args.intervalo)
    finally:
        conexion.close()


if __name__ == "__main__":  # pragma: no cover - lo cubre el test del CLI
    sys.exit(main())
