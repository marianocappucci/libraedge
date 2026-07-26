"""FastAPI adapter for the central synchronization receiver."""

from libraedge.domain.sync import OutboxOperation


def create_sync_app(receiver, node_repository):
    """Build the HTTP app around an already-configured SyncReceiver.

    ``node_repository`` (a ``SqliteNodeRepository`` or anything exposing
    ``verify_node_secret(node_id, secret) -> bool``) gates every push:
    without it, this endpoint was unauthenticated -- anyone who could reach
    it could inject operations that ``receiver.operation_handler`` would
    materialize as real domain data (e.g. a confirmed sale). Each node
    authenticates with the per-node secret returned once by
    ``register_node()``, sent as ``Authorization: Bearer <secret>``.

    FastAPI is optional at library level; only the central deployment needs it.
    """
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI es opcional; instalar libraedge[server]"
        ) from exc

    app = FastAPI(title="LibraCommerce Sync API", version="0.1")

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

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(401, detail="missing bearer token")
        secret = auth_header.removeprefix("Bearer ")
        if not node_repository.verify_node_secret(payload["node_id"], secret):
            raise HTTPException(401, detail="invalid node credentials")

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
