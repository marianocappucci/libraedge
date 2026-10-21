"""FastAPI adapter for the central synchronization receiver.

Dos endpoints, uno por direccion:

- ``POST /sync/v1/push`` recibe los eventos que el nodo genero (la subida).
- ``GET /sync/v1/pull`` entrega los cambios de los datos de referencia para que
  el nodo los espeje (la bajada, desde la Fase 2).

Los dos se autentican igual: el secreto por nodo como ``Authorization: Bearer``.

Desde v0.17 ``create_sync_router`` suma ``GET /sync/v1/salud`` y ``/salud/{node_id}``, la salud de los nodos para el
monitor «HTTP + keyword» de uptime-kuma (ADR-018). Esas dos NO usan el secreto de un nodo sino un token de monitoreo
de la instancia.
"""

import hmac
import inspect
import logging
import os

from libraedge.db.changelog import listar_cambios
from libraedge.domain.sync import OutboxOperation
from libraedge.sync import esquema
from libraedge.sync import ticket as _ticket
from libraedge.sync.pull import serializar_cambio

logger = logging.getLogger(__name__)

#: El token de monitoreo de la instancia (ADR-018). Sin esta variable las rutas de salud no existen (404).
VARIABLE_TOKEN_MONITOREO = "LIBRAEDGE_TOKEN_MONITOREO"
RUTA_SALUD = "/sync/v1/salud"
#: Las palabras que busca el monitor «HTTP + keyword». **Estables: son un contrato con el monitor**, no se renombran.
#: `PALABRA_OK` esta solo en las respuestas 200 y `PALABRA_ALERTA` solo en las 503, asi el monitor sirve igual
#: configurado para buscar una o para buscar que la otra falte.
PALABRA_OK = "LIBRAEDGE_OK"
PALABRA_ALERTA = "LIBRAEDGE_ALERTA"
#: Tope del `?umbral=`: una semana. Mas que eso ya no es un umbral, es apagar el monitor.
UMBRAL_MAXIMO_MINUTOS = 7 * 24 * 60

#: Lo que un push tiene que traer. Vive afuera de las dos fabricas para que no
#: se les vaya desincronizando una de la otra.
CAMPOS_DEL_PUSH = frozenset({
    "operation_id", "node_id", "sequence", "operation_type",
    "aggregate_type", "aggregate_id", "occurred_at", "schema_version", "payload",
})


def operacion_desde_payload(payload: dict) -> OutboxOperation:
    """El JSON de un push, como `OutboxOperation`. Levanta `ValueError` si no."""
    try:
        return OutboxOperation(
            operation_id=payload["operation_id"], node_id=payload["node_id"],
            sequence=int(payload["sequence"]),
            operation_type=payload["operation_type"],
            aggregate_type=payload["aggregate_type"],
            aggregate_id=payload["aggregate_id"],
            occurred_at=payload["occurred_at"],
            schema_version=int(payload["schema_version"]),
            payload=payload["payload"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid operation") from exc


def _verificar_esquema(request, version_esquema, conexion, HTTPException) -> None:
    """409 si el central declara version de esquema y la del nodo no es esa.

    Va **despues** de autenticar: a quien no es un nodo no se le cuenta en que
    version esta el central. Ver ``libraedge.sync.esquema``.
    """
    if version_esquema is None:
        return
    esperada = esquema.resolver(version_esquema, conexion)
    if esperada is None:
        return
    recibida = request.headers.get(esquema.CABECERA)
    if recibida != esperada:
        raise HTTPException(409, detail=esquema.detalle_409(esperada, recibida))


def create_sync_app(receiver, node_repository, changelog_conn=None,
                    version_esquema=None):
    """Build the HTTP app around an already-configured SyncReceiver.

    ``node_repository`` (a ``NodeRepository`` or anything exposing
    ``verify_node_secret(node_id, secret) -> bool``) gates every request:
    without it, the push endpoint was unauthenticated -- anyone who could
    reach it could inject operations that ``receiver.operation_handler``
    would materialize as real domain data (e.g. a confirmed sale). Each
    node authenticates with the per-node secret returned once by
    ``register_node()``, sent as ``Authorization: Bearer <secret>``.

    ``changelog_conn`` es la conexion desde la que se sirve la bajada. Es
    **opcional** para no romper a quien ya construia esta app con dos
    argumentos: sin ella, ``GET /sync/v1/pull`` responde 501 en vez de no
    existir. Un 404 diria "este central no tiene ese endpoint" y un nodo mal
    configurado lo leeria como una version vieja; el 501 dice que el endpoint
    existe y que a este central le falta el changelog.

    ``version_esquema`` (texto o ``(conexion) -> str``) prende el control de
    version entre nodo y central; ver ``libraedge.sync.esquema``. El invocable
    recibe la conexion del receptor.

    FastAPI is optional at library level; only the central deployment needs it.
    """
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI es opcional; instalar libraedge[server]"
        ) from exc

    app = FastAPI(title="LibraCommerce Sync API", version="0.1")

    def _autenticar(request, node_id: str) -> None:
        """El mismo gate para las dos direcciones."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(401, detail="missing bearer token")
        secret = auth_header.removeprefix("Bearer ")
        if not node_repository.verify_node_secret(node_id, secret):
            raise HTTPException(401, detail="invalid node credentials")

    @app.get("/sync/v1/pull")
    def pull(request: Request, node_id: str, cursor: int = 0, limit: int = 500):
        """Los cambios de referencia posteriores a ``cursor``, en orden.

        El nodo manda el cursor que tiene guardado; el central no lleva estado
        de por donde va cada nodo. Eso es a proposito: el nodo es el unico que
        sabe que alcanzo a **aplicar**, y un cursor del lado del central diria
        "ya se lo mande" cuando lo que importa es "ya lo escribio".
        """
        _autenticar(request, node_id)
        _verificar_esquema(request, version_esquema, receiver.conn, HTTPException)
        if changelog_conn is None:
            raise HTTPException(
                501, detail="este central no tiene changelog configurado"
            )
        limit = max(1, min(limit, 1000))
        cambios = listar_cambios(changelog_conn, desde=cursor, limit=limit)
        return {
            "changes": [serializar_cambio(cambio) for cambio in cambios],
            "cursor": cambios[-1].cursor if cambios else cursor,
        }

    @app.post("/sync/v1/push")
    def push(payload: dict, request: Request):
        missing = sorted(CAMPOS_DEL_PUSH - payload.keys())
        if missing:
            raise HTTPException(422, detail={"missing": missing})

        _autenticar(request, payload["node_id"])
        _verificar_esquema(request, version_esquema, receiver.conn, HTTPException)

        try:
            operation = operacion_desde_payload(payload)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid operation") from exc
        result = receiver.accept(operation)
        return {"operation_id": operation.operation_id, "result": result.result, "error": result.error}

    return app


def emisor_desde_payload(payload: dict):
    """El ``emisor_id`` de un pedido de ticket: ``None`` (el emisor por defecto) o un entero.

    Levanta ``ValueError`` si no es ninguna de las dos cosas."""
    valor = payload.get("emisor_id")
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise ValueError("emisor_id invalido")
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.strip().isdigit():
        return int(valor.strip())
    raise ValueError("emisor_id invalido")


def detalle_de_error_de_prestamo(exc: Exception) -> tuple[int, dict]:
    """El status y el ``detail`` con que el central contesta si ``prestar_ticket`` levanta.

    - Una excepción con ``detalle() -> dict`` (``NodoSinArca`` de libracore): 422 con ese detalle.
    - ``ValueError`` o ``LookupError`` (emisor que no existe): 422, sin el texto de la excepción.
    - Cualquier otra (ARCA no autentica, red): 502. El nodo la lee como «sin conexión» y reintenta.

    Nunca se usa ``str(exc)`` de una excepción cualquiera: podría traer el ticket.
    """
    detalle = getattr(exc, "detalle", None)
    if callable(detalle):
        try:
            datos = detalle()
        except Exception:
            datos = None
        if isinstance(datos, dict) and datos.get("codigo"):
            return 422, datos
    if isinstance(exc, (ValueError, LookupError)):
        return 422, {"codigo": _ticket.CODIGO_NO_DISPONIBLE,
                     "mensaje": "El central no tiene con qué prestar un ticket de ARCA para ese emisor."}
    return 502, {"codigo": _ticket.CODIGO_ARCA_NO_RESPONDE,
                 "mensaje": "El central no pudo obtener el ticket de ARCA."}


async def _llamar(funcion, *args):
    """``funcion(*args)``, sea ``async`` o no. La sincrónica corre en un hilo para no frenar el loop."""
    from starlette.concurrency import run_in_threadpool

    llamada = type(funcion).__call__ if not inspect.isfunction(funcion) else None
    if inspect.iscoroutinefunction(funcion) or inspect.iscoroutinefunction(llamada):
        return await funcion(*args)
    resultado = await run_in_threadpool(funcion, *args)
    if inspect.isawaitable(resultado):
        resultado = await resultado
    return resultado


def _entrada_de_salud(nodo: dict, umbral: int) -> dict:
    """Un nodo activo como lo ve el monitor. **Solo datos de salud**: nunca el hash del secreto ni el prefijo."""
    from libraedge.pantalla_del_central import _estado_del_nodo

    minutos = nodo["minutos_sin_verse"]
    return {
        "node_id": nodo["node_id"],
        "sucursal_id": str(nodo["branch_id"]),
        "estado": _estado_del_nodo(True, minutos, umbral),
        "minutos_sin_verse": None if minutos is None else round(minutos, 1),
        "ultima_sincronizacion": nodo["ultima_vez"],
    }


def _cuerpo_de_salud(estado: str, entrada: dict | None, umbral: int, **extra) -> tuple[int, dict]:
    """El status y el cuerpo: `{estado, palabra_clave, minutos_sin_verse, ultima_sincronizacion, umbral_minutos}`."""
    sano = estado in ("al_dia", "sin_nodos")
    cuerpo = {
        "estado": estado,
        "palabra_clave": PALABRA_OK if sano else PALABRA_ALERTA,
        "minutos_sin_verse": entrada["minutos_sin_verse"] if entrada else None,
        "ultima_sincronizacion": entrada["ultima_sincronizacion"] if entrada else None,
        "umbral_minutos": umbral,
        **extra,
    }
    return (200 if sano else 503), cuerpo


def salud_de_todos(nodos: list[dict], umbral: int) -> tuple[int, dict]:
    """El resumen de los nodos **activos** (los dados de baja no se vigilan, como en ``aprovisionamiento.vigilar``).

    El estado es el del peor: ``sin_noticias`` si alguno se callo, si no ``nunca`` si alguno todavia no hablo, si no
    ``al_dia``. Sin nodos activos es ``sin_nodos`` y es 200, como ``vigilar`` (que sale 0): no hay nada que vigilar.
    ``minutos_sin_verse`` y ``ultima_sincronizacion`` son los del nodo peor, que es el que explica el estado."""
    entradas = [_entrada_de_salud(nodo, umbral) for nodo in nodos if nodo["activo"]]
    if not entradas:
        return _cuerpo_de_salud("sin_nodos", None, umbral, nodos_vigilados=0, nodos=[])
    gravedad = {"al_dia": 0, "nunca": 1, "sin_noticias": 2}
    peor = max(entradas, key=lambda e: (gravedad[e["estado"]], e["minutos_sin_verse"] or 0))
    return _cuerpo_de_salud(peor["estado"], peor, umbral, nodos_vigilados=len(entradas), nodos=entradas)


def salud_de_un_nodo(nodos: list[dict], node_id: str, umbral: int) -> tuple[int, dict] | None:
    """La salud de ``node_id``, o ``None`` si no existe o esta dado de baja (el router contesta 404)."""
    nodo = next((n for n in nodos if n["node_id"] == node_id and n["activo"]), None)
    if nodo is None:
        return None
    entrada = _entrada_de_salud(nodo, umbral)
    return _cuerpo_de_salud(entrada["estado"], entrada, umbral, node_id=entrada["node_id"],
                            sucursal_id=entrada["sucursal_id"])


def create_sync_router(abrir_conexion, operation_handler=None,
                       supported_schema_version: int = 1, *, despachador=None,
                       version_esquema=None, prestar_ticket=None):
    """Las mismas dos rutas, para montar **dentro del producto** con
    ``include_router()``.

    🔴 **La diferencia con create_sync_app no es cosmetica: es la conexion.**
    Aquella recibe conexiones **fijas** al construirse, lo cual sirve para un
    proceso dedicado y esta mal dentro de un servidor web: una sola conexion
    compartida entre requests concurrentes no es segura, y ademas envejece --una
    caida de la base la deja inservible para siempre, sin que nadie la reponga--.

    Aca se recibe ``abrir_conexion``, un invocable que devuelve un **context
    manager** de conexion (la firma de ``libracore.db.core.get_connection``), y
    se abre una por request. El producto ya sabe administrar sus conexiones;
    esto no se mete.

    El ``operation_handler`` lo pone el producto: LibraEdge nunca sabe que es una
    venta. El changelog sale de la misma conexion, porque en el central las
    tablas de los dos conviven en la misma base -- por eso aca no hay un
    equivalente del 501 de ``create_sync_app``: si hay conexion, hay changelog.

    🔴 **La firma del handler es ``(conexion, operacion)``, no ``(operacion)``.**
    Es la que ya tienen los dos handlers de la familia --
    ``apply_confirmed_sale_operation`` y ``aplicar_pedido_cobrado``-- porque un
    handler necesita la conexion para escribir las filas de dominio.
    ``SyncReceiver`` llama con **un** argumento, herencia de cuando el receptor
    era el dueno de la conexion; aca la conexion es del request, asi que se le
    pasa al handler en un cierre.

    Sin eso, un producto que montara el router con su handler de siempre recibia
    ``missing 1 required positional argument`` **por cada operacion**, y el
    receptor lo traducia a ``rejected``: cada venta de cada nodo terminaba en
    revision manual, con el nodo reportandose en linea y sin errores. Se
    encontro al cablearlo de verdad contra el producto -- los tests que llamaban
    al handler directo no podian verlo.

    ## Desde v0.7: ``despachador`` y ``version_esquema``

    - ``despachador`` (un ``libraedge.sync.despacho.Despachador``) registra un
      manejador por ``(operation_type, schema_version)``. Lo que no tiene
      manejador va a revision manual en vez de quedar ``applied``. Es excluyente
      con ``operation_handler``, que se conserva tal cual para quien ya lo usa.
    - ``version_esquema`` (texto o ``(conexion) -> str``, normalmente
      ``libraedge.sync.esquema.version_de_alembic``) hace que las dos rutas
      contesten 409 si el esquema del nodo no es el del central.

    ## Desde v0.9: ``prestar_ticket`` (ADR-009)

    ``prestar_ticket(emisor_id) -> dict`` (puede ser ``async``; normalmente
    ``libracore.nodo_offline.ta_para_el_nodo``) prende ``POST
    /sync/v1/ticket-arca``: un nodo activo, autenticado con su secreto y con el
    esquema al dia, recibe el ticket de ARCA vigente del central para facturar
    sin tener la clave. Sin la costura la ruta contesta 501 (despues de
    autenticar). El ticket nunca se loguea. Ver ``libraedge.sync.ticket``.

    ## Desde v0.17: la salud de los nodos para uptime-kuma (ADR-018)

    ``GET /sync/v1/salud`` (resumen de los nodos activos) y ``GET /sync/v1/salud/{node_id}``. 200 si esta al dia, 503 si
    ``sin_noticias`` o ``nunca``, 404 si el nodo esta dado de baja o no existe. El cuerpo trae ``estado``,
    ``minutos_sin_verse``, ``ultima_sincronizacion`` y una ``palabra_clave`` estable (``LIBRAEDGE_OK`` /
    ``LIBRAEDGE_ALERTA``) para el monitor «HTTP + keyword». Se autentican con el token de monitoreo de la instancia
    (``LIBRAEDGE_TOKEN_MONITOREO``), por ``Authorization: Bearer`` o ``?token=``; **sin la variable las dos rutas
    contestan 404** (apagadas). ``?umbral=`` son los minutos sin noticias (default: el de ``nodo vigilar``).
    """
    if despachador is not None and operation_handler is not None:
        raise ValueError("pasar despachador u operation_handler, no los dos")
    try:
        from fastapi import APIRouter, HTTPException, Request
    except ImportError as exc:
        raise RuntimeError("FastAPI es opcional; instalar libraedge[server]") from exc

    from libraedge.db.repository import NodeRepository
    from libraedge.sync.receiver import SyncReceiver

    router = APIRouter(tags=["sync"])

    def _secreto(request) -> str:
        cabecera = request.headers.get("authorization", "")
        if not cabecera.startswith("Bearer "):
            raise HTTPException(401, detail="missing bearer token")
        return cabecera.removeprefix("Bearer ")

    def _autenticar(conexion, node_id: str, secreto: str) -> None:
        """Verifica el secreto **y anota el contacto**. Las dos cosas juntas.

        🔴 El latido va acá y no en cada `endpoint`, a propósito: éste es el
        único punto por el que pasa todo nodo que se identifica, así que una
        ruta nueva no se lo puede olvidar. Es la diferencia entre un mecanismo
        que hay que acordarse de invocar y uno que no se puede saltear.

        Se anota **después** de verificar: un secreto inválido no es un nodo
        vivo, es alguien golpeando la puerta, y contarlo como contacto dejaría
        a un nodo revocado viéndose sano para siempre.
        """
        repositorio = NodeRepository(conexion)
        if not repositorio.verify_node_secret(node_id, secreto):
            raise HTTPException(401, detail="invalid node credentials")
        repositorio.registrar_contacto(node_id)

    @router.post("/sync/v1/push")
    def push(payload: dict, request: Request):
        faltan = sorted(CAMPOS_DEL_PUSH - payload.keys())
        if faltan:
            raise HTTPException(422, detail={"missing": faltan})
        secreto = _secreto(request)
        try:
            operacion = operacion_desde_payload(payload)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid operation") from exc

        with abrir_conexion() as conexion:
            _autenticar(conexion, payload["node_id"], secreto)
            _verificar_esquema(request, version_esquema, conexion, HTTPException)
            # El handler de la familia toma (conexion, operacion); el receptor
            # llama con una sola. El cierre le pasa la conexion de ESTE request.
            handler = None
            if operation_handler is not None:
                def handler(op, _conexion=conexion):
                    return operation_handler(_conexion, op)

            receptor = SyncReceiver(
                conexion, operation_handler=handler,
                supported_schema_version=supported_schema_version,
                despachador=despachador,
            )
            resultado = receptor.accept(operacion)
        return {"operation_id": operacion.operation_id,
                "result": resultado.result, "error": resultado.error}

    def _autorizar_monitoreo(request, token_de_query: str | None) -> None:
        """El token de monitoreo de la instancia. 404 si no esta definido, 401 si no coincide.

        🔴 **Se lee del entorno en cada pedido**, no al construir el router: apagar el monitoreo o rotar el token es
        cambiar la variable y reiniciar, y no depende del orden en que el producto monte las cosas. Sin variable
        (o vacia) la ruta **no existe**: un 401 diria que hay un monitoreo y cual es la puerta. El 404 es el mismo
        cuerpo que da FastAPI para una ruta inexistente.

        Se compara con ``hmac.compare_digest`` y en bytes (con ``str`` y un caracter no ASCII levanta ``TypeError``).
        El token nunca se loguea. ``?token=`` queda en los access logs del proxy: es para el monitor que no puede
        mandar cabeceras, y la cabecera es lo que hay que preferir."""
        esperado = os.environ.get(VARIABLE_TOKEN_MONITOREO, "").strip()
        if not esperado:
            raise HTTPException(404, detail="Not Found")
        cabecera = request.headers.get("authorization", "")
        presentado = cabecera.removeprefix("Bearer ") if cabecera.startswith("Bearer ") else token_de_query
        if not presentado or not hmac.compare_digest(presentado.strip().encode("utf-8"), esperado.encode("utf-8")):
            raise HTTPException(401, detail="invalid monitoring token", headers={"WWW-Authenticate": "Bearer"})

    def _umbral(crudo: str | None) -> int:
        """``?umbral=`` en minutos. Se parsea a mano y **despues** de autenticar: con ``umbral: int`` en la firma,
        FastAPI contesta 422 antes de entrar, y una ruta apagada dejaria de parecer inexistente."""
        if crudo is None or not crudo.strip():
            from libraedge.pantalla_del_central import UMBRAL_MINUTOS

            return UMBRAL_MINUTOS
        try:
            minutos = int(crudo)
        except ValueError:
            minutos = 0
        if not 1 <= minutos <= UMBRAL_MAXIMO_MINUTOS:
            raise HTTPException(422, detail=f"umbral: minutos enteros de 1 a {UMBRAL_MAXIMO_MINUTOS}")
        return minutos

    def _respuesta_de_salud(status: int, cuerpo: dict):
        from fastapi.responses import JSONResponse

        return JSONResponse(cuerpo, status_code=status, headers={"Cache-Control": "no-store"})

    def _nodos_para_vigilar() -> list[dict]:
        with abrir_conexion() as conexion:
            nodos = NodeRepository(conexion).nodos_para_vigilar()
            conexion.commit()  # la lectura abre una transaccion en PostgreSQL: no se la deja abierta
        return nodos

    @router.get(RUTA_SALUD)
    def salud(request: Request, token: str | None = None, umbral: str | None = None):
        """Resumen para el monitor de uptime-kuma. Ver ``create_sync_router``, «Desde v0.17»."""
        _autorizar_monitoreo(request, token)
        minutos = _umbral(umbral)
        return _respuesta_de_salud(*salud_de_todos(_nodos_para_vigilar(), minutos))

    @router.get(RUTA_SALUD + "/{node_id}")
    def salud_del_nodo(node_id: str, request: Request, token: str | None = None, umbral: str | None = None):
        """La salud de un nodo. 404 si esta dado de baja o no existe."""
        _autorizar_monitoreo(request, token)
        minutos = _umbral(umbral)
        resultado = salud_de_un_nodo(_nodos_para_vigilar(), node_id, minutos)
        if resultado is None:
            raise HTTPException(404, detail="node not found")
        return _respuesta_de_salud(*resultado)

    @router.get("/sync/v1/pull")
    def pull(request: Request, node_id: str, cursor: int = 0, limit: int = 500):
        secreto = _secreto(request)
        with abrir_conexion() as conexion:
            _autenticar(conexion, node_id, secreto)
            _verificar_esquema(request, version_esquema, conexion, HTTPException)
            limit = max(1, min(limit, 1000))
            cambios = listar_cambios(conexion, desde=cursor, limit=limit)
        return {"changes": [serializar_cambio(cambio) for cambio in cambios],
                "cursor": cambios[-1].cursor if cambios else cursor}

    @router.post(_ticket.RUTA)
    async def ticket_arca(payload: dict, request: Request):
        """El ticket de ARCA del central, prestado a un nodo activo. Ver ``libraedge.sync.ticket``."""
        from fastapi.responses import JSONResponse
        from starlette.concurrency import run_in_threadpool

        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise HTTPException(422, detail={"missing": ["node_id"]})
        try:
            emisor_id = emisor_desde_payload(payload)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid emisor_id") from exc
        secreto = _secreto(request)

        def _autorizar():
            # Mismo gate que push y pull: secreto de un nodo ACTIVO, latido y esquema.
            with abrir_conexion() as conexion:
                _autenticar(conexion, node_id, secreto)
                _verificar_esquema(request, version_esquema, conexion, HTTPException)

        await run_in_threadpool(_autorizar)
        if prestar_ticket is None:
            raise HTTPException(501, detail={
                "codigo": _ticket.CODIGO_SIN_COSTURA,
                "mensaje": "Este central no presta el ticket de ARCA a los nodos."})
        try:
            ticket = await _llamar(prestar_ticket, emisor_id)
        except HTTPException:
            raise
        except Exception as exc:
            status, detalle = detalle_de_error_de_prestamo(exc)
            # Sólo el tipo: el texto de una excepción cualquiera podría traer el ticket.
            logger.warning("Ticket de ARCA: no se pudo prestar al nodo %s (emisor %s): %s",
                           node_id, emisor_id, type(exc).__name__)
            raise HTTPException(status, detail=detalle) from None
        if not (isinstance(ticket, dict) and ticket.get("token") and ticket.get("sign")):
            logger.error("Ticket de ARCA: prestar_ticket devolvio algo sin token o sign (nodo %s)", node_id)
            raise HTTPException(502, detail={
                "codigo": _ticket.CODIGO_ARCA_NO_RESPONDE,
                "mensaje": "El central no pudo obtener el ticket de ARCA."})
        logger.info("Ticket de ARCA prestado al nodo %s (emisor %s, vence %s)",
                    node_id, emisor_id, ticket.get("expiracion"))
        return JSONResponse(ticket, headers={"Cache-Control": "no-store"})

    return router
