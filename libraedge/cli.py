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
| `LIBRAEDGE_TABLAS_SOLO_LECTURA` | Opcional: tablas **del espejo** que el producto no escribe (v0.17, ADR-019) |
| `LIBRAEDGE_ALTAS_EN_NODO` | Opcional: tablas del espejo con alta en el nodo, id provisorio (v0.18, ADR-021) |
| `LIBRAEDGE_ESTADO` | Donde escribir el estado para que lo lea una UI |
| `LIBRAEDGE_VERSION_ESQUEMA` | Opcional: `alembic` (default), `modulo:funcion` o `ninguna` |
| `LIBRAEDGE_CARGA_INICIAL` | Opcional: `modulo:funcion` que trae las existencias de la sucursal (v0.20, ADR-023) |

Una clave compuesta en `LIBRAEDGE_TABLAS_ESPEJO` va con `+`
(`party_roles:party_id+role`). `LIBRAEDGE_VERSION_ESQUEMA` dice como se calcula
la version del esquema que el nodo le declara al central (`sync/esquema.py`).

`LIBRAEDGE_CARGA_INICIAL` es **blanda** a proposito: si no se entiende o el producto instalado no trae el modulo, el
nodo arranca igual, sigue subiendo las ventas y lo dice en el estado (`carga_inicial`). Las demas variables frenan el
arranque porque sin ellas el nodo sincronizaria mal; esta no, y frenarlo dejaria una caja sin sistema por un dato que
se arregla actualizando.

`LIBRAEDGE_TABLAS_ESPEJO` no tiene default a proposito: sin lista, el aplicador
aceptaria cualquier tabla que el central mande, y esa lista **es** la frontera
que impide que el nodo escriba en tablas de su propia autoridad. Un default
vacio la volveria opcional en la practica.
"""

import argparse
import os
import sys
import time
from datetime import UTC

from libraedge.db.changelog import columnas_de_clave
from libraedge.sync.carga_inicial import VARIABLE as VARIABLE_CARGA_INICIAL


def _requerido(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        raise SystemExit(
            f"Falta la variable {nombre}. El nodo no arranca a medias: sin ella "
            f"sincronizaria contra el lugar equivocado o con la identidad "
            f"equivocada."
        )
    return valor


def tablas_espejo(crudo: str) -> dict[str, str | tuple[str, ...]]:
    """`"productos:id,clientes:id"` -> `{"productos": "id", "clientes": "id"}`.

    La `pk` se puede omitir y queda en `id`, que es la de casi todas las tablas
    de la familia. Una clave compuesta va con `+`:
    `"party_roles:party_id+role"` -> `{"party_roles": ("party_id", "role")}`.
    """
    tablas: dict[str, str | tuple[str, ...]] = {}
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        nombre, _, pk = parte.partition(":")
        pk = pk.strip() or "id"
        tablas[nombre.strip()] = (
            tuple(columna.strip() for columna in pk.split("+")) if "+" in pk else pk)
    if not tablas:
        raise SystemExit(
            "LIBRAEDGE_TABLAS_ESPEJO quedo vacia. Es el reparto de autoridad: "
            "sin ella el nodo aceptaria cambios sobre cualquier tabla suya."
        )
    return tablas


def tablas_solo_lectura(crudo: str | None, espejo) -> tuple[str, ...]:
    """`"productos,categorias"` -> `("categorias", "productos")`. Vacia o ausente: ninguna (sin guarda).

    Tienen que ser tablas del espejo: una de solo lectura que el central no manda quedaria bloqueada para siempre,
    sin que el espejo ni el producto pudieran escribirla.
    """
    nombres = tuple(sorted({parte.strip() for parte in (crudo or "").split(",") if parte.strip()}))
    sobran = [nombre for nombre in nombres if nombre not in espejo]
    if sobran:
        raise SystemExit(
            f"LIBRAEDGE_TABLAS_SOLO_LECTURA trae tablas que no estan en LIBRAEDGE_TABLAS_ESPEJO: "
            f"{', '.join(sobran)}. Una tabla que el nodo no espeja nadie la escribiria.")
    return nombres


def altas_en_nodo(crudo: str | None, espejo) -> tuple:
    """`"clients:cc_asientos.tercero_id@rol=cliente,parties"` -> las `AltaEnNodo` (ver `libraedge.altas`). Vacia o
    ausente: ninguna. Cada tabla tiene que ser del espejo, con una sola columna de clave, y el espejo tiene que traer
    el mapeo: sin el, el nodo dio de alta filas que nunca se reapuntan."""
    from libraedge.altas import parsear_altas
    from libraedge.mapeo import TABLA as TABLA_MAPEO

    try:
        altas = parsear_altas(crudo)
    except ValueError as exc:
        raise SystemExit(f"LIBRAEDGE_ALTAS_EN_NODO no se entiende: {exc}") from exc
    sobran = sorted({a.tabla for a in altas} - set(espejo))
    if sobran:
        raise SystemExit(
            f"LIBRAEDGE_ALTAS_EN_NODO trae tablas que no estan en LIBRAEDGE_TABLAS_ESPEJO: {', '.join(sobran)}. "
            "El nodo no recibiria nunca el id que les asigna el central.")
    compuestas = sorted(a.tabla for a in altas if len(columnas_de_clave(espejo[a.tabla])) != 1)
    if compuestas:
        raise SystemExit(f"LIBRAEDGE_ALTAS_EN_NODO trae tablas con clave compuesta: {', '.join(compuestas)}.")
    if altas and TABLA_MAPEO not in espejo:
        raise SystemExit(
            f"LIBRAEDGE_ALTAS_EN_NODO pide {TABLA_MAPEO} en LIBRAEDGE_TABLAS_ESPEJO y no esta: sin el mapeo el nodo no "
            "reapunta lo que dio de alta.")
    return altas


def version_esquema_del_nodo(conexion, especificacion: str | None) -> str | None:
    """La version del esquema que el nodo declara, segun `LIBRAEDGE_VERSION_ESQUEMA`.

    - `alembic` (o vacia): `libraedge.sync.esquema.version_de_alembic`, que da
      lo mismo en el nodo y en el central cuando los dos estan migrados igual.
    - `modulo:funcion`: una funcion del producto `(conexion) -> str`; tiene que
      ser la misma que el central le pasa al router como `version_esquema`.
    - `ninguna`: no se declara (un central que exige version contesta 409).

    Se calcula una vez al arrancar: actualizar el nodo reinicia el servicio.
    """
    from importlib import import_module

    from libraedge.sync.esquema import version_de_alembic

    especificacion = (especificacion or "alembic").strip()
    if especificacion == "ninguna":
        return None
    if especificacion == "alembic":
        return version_de_alembic(conexion)
    modulo, _, funcion = especificacion.partition(":")
    if not modulo or not funcion:
        raise SystemExit(
            f"LIBRAEDGE_VERSION_ESQUEMA={especificacion!r} no se entiende: usar "
            f"'alembic', 'ninguna' o 'modulo:funcion'.")
    return getattr(import_module(modulo), funcion)(conexion)


def construir_nodo():
    """Arma el nodo desde el entorno. Falla temprano y con un mensaje util."""
    from libracore.db import core  # noqa: PLC0415 - solo en el proceso del nodo

    from libraedge.db.guarda import instalar_guarda_de_espejo
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
    solo_lectura = tablas_solo_lectura(os.environ.get("LIBRAEDGE_TABLAS_SOLO_LECTURA"), tablas)
    altas = altas_en_nodo(os.environ.get("LIBRAEDGE_ALTAS_EN_NODO"), tablas)

    core.configure(base)
    conexion = core.get_connection()
    repositorio = NodeRepository(conexion)
    version = version_esquema_del_nodo(conexion, os.environ.get("LIBRAEDGE_VERSION_ESQUEMA"))
    # La guarda del espejo (ADR-019), en cada arranque y despues de las migraciones: el servicio del nodo arranca
    # cuando la base ya esta migrada. Converge: sin la variable saca las guardas que hubiera de una carga anterior.
    # Las tablas con alta en el nodo dejan pasar sus filas provisorias (id negativo): ADR-021.
    if altas:
        # El mapeo lo crea `init_schema()` con el arranque del producto; si el servicio del nodo se adelanta, que no
        # falle el primer ciclo por una tabla que todavia no esta (idempotente).
        from libraedge.mapeo import init_mapeo_schema

        init_mapeo_schema(conexion)
    guardadas = instalar_guarda_de_espejo(
        conexion, solo_lectura, {a.tabla: columnas_de_clave(tablas[a.tabla])[0] for a in altas})
    for faltante in sorted(set(solo_lectura) - set(guardadas)):
        print(f"AVISO: LIBRAEDGE_TABLAS_SOLO_LECTURA nombra {faltante!r} y esa tabla no esta en la base: "
              "sin guarda hasta que la cree una migracion.", file=sys.stderr)
    # La lectura del catalogo abre una transaccion en PostgreSQL: se cierra
    # aca para no dejar la conexion del nodo "idle in transaction".
    conexion.commit()

    return Nodo(
        repositorio, node_id,
        outbox_worker=OutboxWorker(
            repositorio, HttpSyncTransport(central, secreto, version_esquema=version)),
        pull_worker=PullWorker(
            repositorio,
            HttpPullTransport(central, node_id, secreto, version_esquema=version),
            MirrorApplier(conexion, tablas, node_id=node_id, altas=altas),
        ),
        ruta_estado=os.environ.get("LIBRAEDGE_ESTADO"),
        carga_inicial_worker=carga_inicial_del_nodo(
            repositorio, conexion, node_id, central, secreto, version, os.environ.get(VARIABLE_CARGA_INICIAL)),
    ), conexion


def carga_inicial_del_nodo(repositorio, conexion, node_id, central, secreto, version, especificacion):
    """El `CargaInicialWorker` si el producto declara `LIBRAEDGE_CARGA_INICIAL`; si no, `None` (ADR-023).

    No importa la funcion aca: se resuelve en cada intento y, si falla, queda en el estado del nodo (ver el docstring de
    `libraedge.sync.carga_inicial`)."""
    from libraedge.sync.carga_inicial import CargaInicialWorker, HttpCargaInicialTransport

    if not (especificacion or "").strip():
        return None
    return CargaInicialWorker(
        repositorio, conexion, node_id,
        HttpCargaInicialTransport(central, node_id, secreto, version_esquema=version), especificacion)


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

    revision = sub.add_parser(
        "revision", help="las operaciones que el central rechazo (revision manual)")
    revision_sub = revision.add_subparsers(dest="accion", required=True)
    listar = revision_sub.add_parser("listar", help="lista las operaciones en revision")
    listar.add_argument("--todas", action="store_true", help="incluir las descartadas")
    reenviar = revision_sub.add_parser(
        "reenviar", help="la vuelve a poner en cola (despues de resolverla en el central)")
    reenviar.add_argument("operation_id")
    descartar = revision_sub.add_parser(
        "descartar",
        help="la cierra sin mandarla nunca mas (el central la rechazo y no se va a corregir)")
    descartar.add_argument("operation_id")
    descartar.add_argument("--motivo", required=True, help="por que se descarta (obligatorio, no vacio)")

    carga = sub.add_parser(
        "carga-inicial",
        help="trae las existencias de la sucursal del central si todavia no se trajeron (soporte)")
    carga.add_argument(
        "--forzar", action="store_true",
        help="borra la marca y la hace ya. Cuidado: pisa las existencias del nodo con las del central")

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
                leer_estado(ruta), datetime.now(UTC), args.intervalo)
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

    if args.comando == "revision":
        return _revision_del_nodo(args)

    nodo, conexion = construir_nodo()
    try:
        if args.comando == "sincronizar":
            estado = nodo.sincronizar()
            print(estado.como_json())
            # Sale distinto segun el resultado para que un cron o un servicio
            # puedan alertar sin parsear el JSON.
            return 0 if estado.en_linea else 1

        if args.comando == "carga-inicial":
            return _carga_inicial_del_nodo(nodo, forzar=args.forzar)

        while True:
            estado = nodo.sincronizar()
            estado_texto = "en linea" if estado.en_linea else "fuera de linea"
            print(
                f"[{estado.ultimo_intento}] {estado_texto} | "
                f"pendientes={estado.pendientes} subidas={estado.operaciones_subidas} "
                f"bajados={estado.cambios_bajados}"
                + (f" | {estado.ultimo_error}" if estado.ultimo_error else "")
                + (f" | carga inicial {estado.carga_inicial}" if estado.carga_inicial else ""),
                flush=True,
            )
            time.sleep(args.intervalo)
    finally:
        conexion.close()


def _carga_inicial_del_nodo(nodo, forzar: bool) -> int:
    """`libraedge-nodo carga-inicial [--forzar]`: un intento, con el resultado en JSON.

    Sale 0 si quedo terminada (hecha, ya estaba, o el central no tiene nada que traer) y 1 si espera o fallo."""
    import json

    from libraedge.sync.carga_inicial import VARIABLE, YA_HECHA

    trabajador = nodo.carga_inicial_worker
    if trabajador is None:
        print(f"Este nodo no tiene {VARIABLE}: el producto no declara carga inicial de existencias.", file=sys.stderr)
        return 1
    resultado = trabajador.run_once(forzar=forzar)
    print(json.dumps({"estado": resultado.estado, "detalle": resultado.detalle,
                      "resultado": resultado.resultado}, ensure_ascii=False, indent=2, default=str))
    if resultado.estado == YA_HECHA:
        print("Ya estaba hecha. Para volver a traerla (pisa las existencias del nodo): carga-inicial --forzar.",
              file=sys.stderr)
    return 0 if resultado.terminada else 1


def _revision_del_nodo(args) -> int:
    """`libraedge-nodo revision listar|reenviar|descartar`. Solo necesita la base local.

    `descartar` (v0.21, ADR-024) sale 0 si la dejo en `discarded` **o si ya lo estaba** (avisa por stderr y no pisa el
    motivo: un reintento no es un error) y 1 si no existe, esta en otro estado o falta el motivo."""
    import json

    from libracore.db import core  # noqa: PLC0415 - solo en el proceso del nodo

    from libraedge.db.repository import NodeRepository, OperacionYaDescartada

    core.configure(_requerido("LIBRAEDGE_DATABASE_URL"))
    conexion = core.get_connection()
    try:
        repositorio = NodeRepository(conexion)
        if args.accion == "listar":
            operaciones = repositorio.listar_en_revision()
            if args.todas:
                operaciones += repositorio.listar_descartadas()
            for operacion in operaciones:
                print(json.dumps({
                    "operation_id": operacion.operation_id,
                    "estado": str(operacion.status),
                    "operation_type": operacion.operation_type,
                    "schema_version": operacion.schema_version,
                    "occurred_at": operacion.occurred_at,
                    "motivo": operacion.last_error,
                }, ensure_ascii=False))
            return 0
        if args.accion == "descartar":
            try:
                repositorio.descartar_operacion(args.operation_id, args.motivo)
            except OperacionYaDescartada as exc:
                print(f"{exc}: no se toca (el motivo original sigue)", file=sys.stderr)
                return 0
            except (LookupError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"{args.operation_id}: descartada")
            return 0
        try:
            repositorio.reenviar_operacion(args.operation_id)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{args.operation_id}: en cola otra vez")
        return 0
    finally:
        conexion.close()


if __name__ == "__main__":  # pragma: no cover - lo cubre el test del CLI
    sys.exit(main())
