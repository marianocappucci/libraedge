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
        required = {
            "operation_id", "node_id", "sequence", "operation_type",
            "aggregate_type", "aggregate_id", "occurred_at",
            "schema_version", "payload",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise HTTPException(422, detail={"missing": missing})

        _autenticar(request, payload["node_id"])

        try:
            operation = OutboxOperation(
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
            raise HTTPException(422, detail="invalid operation") from exc
        result = receiver.accept(operation)
        return {"operation_id": operation.operation_id, "result": result.result, "error": result.error}

    return app
