"""FastAPI adapter for the central synchronization receiver.

Dos endpoints, uno por direccion:

- ``POST /sync/v1/push`` recibe los eventos que el nodo genero (la subida).
- ``GET /sync/v1/pull`` entrega los cambios de los datos de referencia para que
  el nodo los espeje (la bajada, desde la Fase 2).

Los dos se autentican igual: el secreto por nodo como ``Authorization: Bearer``.
"""

from libraedge.db.changelog import listar_cambios
from libraedge.domain.sync import OutboxOperation
from libraedge.sync.pull import serializar_cambio

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


def create_sync_app(receiver, node_repository, changelog_conn=None):
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

        try:
            operation = operacion_desde_payload(payload)
        except ValueError as exc:
            raise HTTPException(422, detail="invalid operation") from exc
        result = receiver.accept(operation)
        return {"operation_id": operation.operation_id, "result": result.result, "error": result.error}

    return app


def create_sync_router(abrir_conexion, operation_handler=None,
                       supported_schema_version: int = 1):
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
    """
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
            # El handler de la familia toma (conexion, operacion); el receptor
            # llama con una sola. El cierre le pasa la conexion de ESTE request.
            handler = None
            if operation_handler is not None:
                def handler(op, _conexion=conexion):
                    return operation_handler(_conexion, op)

            receptor = SyncReceiver(
                conexion, operation_handler=handler,
                supported_schema_version=supported_schema_version,
            )
            resultado = receptor.accept(operacion)
        return {"operation_id": operacion.operation_id,
                "result": resultado.result, "error": resultado.error}

    @router.get("/sync/v1/pull")
    def pull(request: Request, node_id: str, cursor: int = 0, limit: int = 500):
        secreto = _secreto(request)
        with abrir_conexion() as conexion:
            _autenticar(conexion, node_id, secreto)
            limit = max(1, min(limit, 1000))
            cambios = listar_cambios(conexion, desde=cursor, limit=limit)
        return {"changes": [serializar_cambio(cambio) for cambio in cambios],
                "cursor": cambios[-1].cursor if cambios else cursor}

    return router
