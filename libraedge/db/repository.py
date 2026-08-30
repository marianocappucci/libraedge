import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from libraedge.domain.sync import OutboxOperation, SyncOperationStatus


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class NodeRepository:
    """Node identity and outbox persistence, on SQLite or PostgreSQL.

    Owns the durable state a LibraEdge node needs: local sequence
    generation and the outbox that ``OutboxWorker``/``SyncReceiver``
    operate on. Knows nothing about the domain of whichever product
    produced the operations.

    ``conn`` is any DB-API connection, and there is deliberately no
    dual-engine layer in here: the SQL below is qmark and portable enough
    for both, and against PostgreSQL the caller passes LibraCore's
    connection -- the very one the product uses in the node, since these
    tables live in the product's own database so that
    ``enqueue_operation()`` can run inside the product's transaction.
    That wrapper already translates qmark to psycopg's format style and
    re-raises psycopg errors as their ``sqlite3`` equivalents.
    """

    def __init__(self, conn):
        self._conn = conn

    def _escribir(self, sql: str, params=(), propia: bool = True):
        """Ejecuta una sentencia dejando la conexión utilizable si falla.

        🔴 **Esto es una diferencia de comportamiento entre motores, no de
        nombres.** En PostgreSQL una sentencia fallida **aborta la transacción
        entera**: sin este rollback, quien atrape la excepción se queda con una
        conexión envenenada y todo lo que siga muere con *"current transaction
        is aborted"*. En SQLite no pasa nada, y por eso una suite que sólo
        corría sobre SQLite no podía verlo -- se encontró el 2026-08-29 al
        parametrizar esta suite sobre los dos motores (Fase 1 del nodo espejo).

        Importa de verdad acá: el nodo encola dentro de la transacción del
        producto, así que una operación rechazada que dejara la conexión
        abortada se llevaría puesta la venta que la estaba abriendo.

        La excepción se re-lanza igual: el rollback deja la conexión sana, no
        se traga el error.

        `propia=False` dice que **la transacción es del llamador** — el caso de
        `enqueue_operation(commit=False)`, donde el producto encola dentro de su
        propia transacción. Ahí el rollback no nos corresponde: se lo llevaría
        puesto todo lo que el producto hizo antes, y **antes de que el producto
        se entere**. El dueño de la transacción es el que decide, y en el caso
        vivo —`cobrar_pedido()` de Restolibra— decide bien: su
        `except Exception: conn.rollback(); raise` deshace la venta entera, que
        es lo correcto si el outbox no pudo registrarla.
        """
        try:
            return self._conn.execute(sql, params)
        except Exception:
            if propia:
                self._conn.rollback()
            raise

    def register_node(self, node_id: str, branch_id: str, schema_version: int = 1) -> str:
        """Create a node identity and return its plaintext secret.

        The secret is shown here once -- only its hash is persisted, same
        pattern as an API key: whoever provisions the edge node copies it
        into ``HttpSyncTransport`` immediately, there's no way to recover
        it later (re-registering, i.e. calling this again for the same
        node_id, issues a new secret and invalidates the old one).
        """
        secret = secrets.token_urlsafe(32)
        self._escribir(
            """INSERT INTO node_identity
                (node_id, branch_id, installed_at, schema_version, secret_hash)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                secret_hash = excluded.secret_hash, active = 1""",
            (node_id, branch_id, datetime.now(timezone.utc).isoformat(),
             schema_version, _hash_secret(secret)),
        )
        self._conn.commit()
        return secret

    def verify_node_secret(self, node_id: str, secret: str) -> bool:
        """True only for an active node whose secret hash matches."""
        row = self._conn.execute(
            "SELECT secret_hash, active FROM node_identity WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None or not row[1]:
            return False
        return hmac.compare_digest(row[0], _hash_secret(secret))

    def deactivate_node(self, node_id: str) -> None:
        """Revoke a node -- e.g. a stolen/decommissioned mini PC. Its
        secret stops verifying immediately; re-registering issues a new
        one and reactivates it."""
        self._escribir(
            "UPDATE node_identity SET active = 0 WHERE node_id = ?", (node_id,)
        )
        self._conn.commit()

    def get_server_cursor(self, node_id: str) -> int:
        """Hasta dónde espejó este nodo los datos de referencia del central.

        Vive en `node_identity.last_server_cursor`, que estaba declarado en el
        esquema desde el primer día y **ningún archivo leía ni escribía** hasta
        la Fase 2 (2026-08-30) — LibraEdge era solo-subida.

        Un nodo que nunca sincronizó devuelve 0, que es lo mismo que "traeme
        todo": el snapshot inicial no es un mecanismo aparte, es pedir desde el
        principio de un changelog que fue sembrado. Ver `db/changelog.py`.
        """
        fila = self._conn.execute(
            "SELECT last_server_cursor FROM node_identity WHERE node_id = ?", (node_id,)
        ).fetchone()
        if fila is None or fila[0] is None:
            return 0
        return int(fila[0])

    def set_server_cursor(self, node_id: str, cursor: int) -> None:
        """Avanza el cursor. **Nunca lo retrocede.**

        El `WHERE ... < ?` no es una optimización: dos ciclos de bajada
        superpuestos —el del arranque y el periódico— pueden terminar fuera de
        orden, y el que termine último no puede hacer que el nodo vuelva a pedir
        cambios que ya aplicó. Un cursor que retrocede reaplica, que es
        inofensivo; pero uno que retrocede *mucho* rehace el snapshot entero
        sobre una base viva.
        """
        self._escribir(
            """UPDATE node_identity SET last_server_cursor = ?
               WHERE node_id = ?
                 AND (last_server_cursor IS NULL OR CAST(last_server_cursor AS INTEGER) < ?)""",
            (str(cursor), node_id, cursor),
        )
        self._conn.commit()

    def next_sequence(self, name: str = "operations", commit: bool = True) -> int:
        """La próxima secuencia local del nodo.

        `commit=False` la reserva **dentro de la transacción del llamador**, que
        además es más fuerte que commitearla aparte: si la venta que la pidió se
        deshace, la secuencia se deshace con ella y no queda un hueco.
        """
        row = self._conn.execute(
            "SELECT next_value FROM local_sequences WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            sequence = 1
            self._escribir(
                "INSERT INTO local_sequences (name, next_value) VALUES (?, ?)",
                (name, 2), propia=commit,
            )
        else:
            sequence = row[0]
            self._escribir(
                "UPDATE local_sequences SET next_value = ? WHERE name = ?",
                (sequence + 1, name), propia=commit,
            )
        if commit:
            self._conn.commit()
        return sequence

    def enqueue_operation(
        self, operation: OutboxOperation, commit: bool = True
    ) -> OutboxOperation:
        """Persist an outbox operation exactly once by operation_id.

        🔴 **`commit=False` es lo que hace posible la atomicidad del nodo**, y es
        el modo con el que lo usa un producto. `cobrar_pedido()` de Restolibra
        corre pedido, venta, ítems, pagos, caja, stock, turno y mesa en **una
        sola transacción**; si el enqueue commitea por su cuenta en el medio,
        publica esa venta a medio hacer. Con `commit=False` la operación del
        outbox entra en la misma transacción que la venta que la origina: o
        quedan las dos, o no queda ninguna.

        Esa atomicidad es justamente la ventaja que hizo elegir a Restolibra
        como piloto —el otro consumidor tiene el outbox en otra conexión y no
        puede tenerla—, y perderla por un `commit()` de más habría sido perder
        el motivo de haberlo elegido.

        El default sigue siendo `True` para no cambiarle el comportamiento a
        quien ya lo usaba con el repositorio dueño de su transacción.

        ``created_at`` se estampa **acá, en Python y en UTC**, no con el
        ``DEFAULT CURRENT_TIMESTAMP`` de la tabla. Ese default es reloj del
        servidor y los dos motores lo escriben distinto: SQLite pone
        ``2026-08-30 02:44:13`` (UTC, sin offset, sin microsegundos) y
        PostgreSQL ``2026-08-29 23:44:14.017409-03`` (hora de la sesión, con
        offset). Es el mismo instante escrito de tres formas distintas en una
        columna TEXT que después se lee como string, y en un nodo se mezclarían
        las dos formas en la misma tabla.

        Con esto queda como ``sent_at`` y ``acknowledged_at``, que ya se
        estampaban así: ISO-8601 en UTC, igual en los dos motores. El default
        del DDL se conserva sólo como red por si alguien inserta a mano.
        """
        self._escribir(
            """
            INSERT INTO sync_outbox
                (operation_id, node_id, sequence, operation_type, aggregate_type,
                 aggregate_id, occurred_at, schema_version, payload_json, status,
                 attempts, next_attempt_at, last_error, created_at, sent_at,
                 acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
            """,
            (operation.operation_id, operation.node_id, operation.sequence,
             operation.operation_type, operation.aggregate_type, operation.aggregate_id,
             operation.occurred_at, operation.schema_version, operation.payload_json(),
             str(operation.status), operation.attempts, operation.next_attempt_at,
             operation.last_error,
             operation.created_at or datetime.now(timezone.utc).isoformat(),
             operation.sent_at, operation.acknowledged_at),
            propia=commit,
        )
        if commit:
            self._conn.commit()
        return self.get_operation(operation.operation_id)

    def get_operation(self, operation_id: str) -> OutboxOperation | None:
        row = self._conn.execute(
            """SELECT operation_id, node_id, sequence, operation_type, aggregate_type,
                    aggregate_id, occurred_at, schema_version, payload_json, status,
                    attempts, next_attempt_at, last_error, created_at, sent_at,
                    acknowledged_at
             FROM sync_outbox WHERE operation_id = ?""", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        return OutboxOperation(
            operation_id=row[0], node_id=row[1], sequence=row[2],
            operation_type=row[3], aggregate_type=row[4], aggregate_id=row[5],
            occurred_at=row[6], schema_version=row[7], payload=json.loads(row[8]),
            status=SyncOperationStatus(row[9]), attempts=row[10],
            next_attempt_at=row[11], last_error=row[12], created_at=row[13],
            sent_at=row[14], acknowledged_at=row[15],
        )

    def reclamar_operaciones_colgadas(self) -> int:
        """Devuelve a `pending` lo que quedó en `sending`. Cuántas rescató.

        🔴 **Sin esto se pierden ventas, y en silencio.**
        `mark_operation_sending()` pasa la operación a `sending` **antes** de
        hablar con el central, y `list_pending_operations()` sólo mira `pending`
        y `retryable_error`. Si el proceso muere en el medio —corte de luz, la PC
        que alguien apagó, el servicio reiniciado— la operación queda en
        `sending` y **ningún worker la vuelve a mirar nunca**. La venta que el
        nodo cobró no llega al central y nada falla: se descubre en el arqueo,
        días después.

        Se apoya en que **el nodo corre un solo worker**: si al arrancar un ciclo
        hay algo en `sending`, es de una corrida que murió, porque nadie más pudo
        haberlo puesto ahí. Y aunque hubiera dos workers y uno reclamara lo que
        el otro está mandando, el peor caso es un envío repetido — que el
        receptor central deduplica por `operation_id`.
        """
        cursor = self._escribir(
            "UPDATE sync_outbox SET status = ?, last_error = ? WHERE status = ?",
            (str(SyncOperationStatus.PENDING),
             "reclamada: el proceso murió mientras se enviaba",
             str(SyncOperationStatus.SENDING)),
        )
        self._conn.commit()
        return cursor.rowcount if cursor is not None else 0

    def list_pending_operations(self, limit: int = 100) -> tuple[OutboxOperation, ...]:
        rows = self._conn.execute(
            """SELECT operation_id FROM sync_outbox
             WHERE status IN (?, ?)
             ORDER BY node_id, sequence LIMIT ?""",
            (str(SyncOperationStatus.PENDING), str(SyncOperationStatus.RETRYABLE_ERROR), limit),
        ).fetchall()
        return tuple(self.get_operation(row[0]) for row in rows)

    def mark_operation_sending(self, operation_id: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, attempts = attempts + 1 WHERE operation_id = ?",
            (str(SyncOperationStatus.SENDING), operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def retry_operation(
        self, operation_id: str, error: str, next_attempt_at: str | None = None
    ) -> OutboxOperation | None:
        self._escribir(
            """UPDATE sync_outbox SET status = ?, last_error = ?, next_attempt_at = ?
             WHERE operation_id = ?""",
            (str(SyncOperationStatus.RETRYABLE_ERROR), error, next_attempt_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def mark_operation_manual_review(self, operation_id: str, error: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, last_error = ? WHERE operation_id = ?",
            (str(SyncOperationStatus.MANUAL_REVIEW), error, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)

    def acknowledge_operation(self, operation_id: str, acknowledged_at: str) -> OutboxOperation | None:
        self._escribir(
            "UPDATE sync_outbox SET status = ?, acknowledged_at = ? WHERE operation_id = ?",
            (str(SyncOperationStatus.ACKNOWLEDGED), acknowledged_at, operation_id),
        )
        self._conn.commit()
        return self.get_operation(operation_id)


#: Nombre anterior, de cuando este repositorio era solo-SQLite. Lo importa
#: LibraCommerce (tests/test_offline_sync.py), asi que se conserva: renombrar
#: sin alias rompia al unico consumidor que hay.
SqliteNodeRepository = NodeRepository
