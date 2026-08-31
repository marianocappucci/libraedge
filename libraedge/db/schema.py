"""El schema del nodo: identidad, secuencias locales, outbox e inbox.

``conn`` es cualquier conexion DB-API -- decia ``sqlite3.Connection`` hasta la
Fase 1 del nodo espejo (2026-08-29), y desde que el nodo corre el producto
entero con PostgreSQL embebido eso pasó a ser mentira. Contra PostgreSQL se le
pasa la conexion de LibraCore, que es la misma del producto (estas tablas viven
en SU base, para que el enqueue entre en su transaccion): esa capa sabe de
``executescript``, traduce los ``?`` y saltea los ``PRAGMA``, asi que el DDL de
abajo sirve igual en los dos motores. Lo cubre la suite, parametrizada.
"""


def init_schema(conn) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS node_identity (
            node_id TEXT PRIMARY KEY,
            branch_id TEXT NOT NULL,
            installed_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            last_server_cursor TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            secret_hash TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS local_sequences (
            name TEXT PRIMARY KEY,
            next_value INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_outbox (
            operation_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT,
            acknowledged_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_outbox_node_sequence
            ON sync_outbox(node_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_sync_outbox_pending
            ON sync_outbox(status, next_attempt_at);

        CREATE TABLE IF NOT EXISTS sync_inbox (
            operation_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            applied_at TEXT,
            status TEXT NOT NULL DEFAULT 'received',
            last_error TEXT
        );
        """
    )

    # 🔴 Columnas agregadas DESPUÉS de la primera versión.
    #
    # `CREATE TABLE IF NOT EXISTS` no toca una tabla que ya existe: en una
    # instalación anterior la tabla está y la columna nueva **no**, y nada
    # falla — hasta que alguien la lee. Por eso el `ALTER` aparte.
    #
    # `PRAGMA table_info` y no un `ALTER ... IF NOT EXISTS`: eso último existe
    # en PostgreSQL y **no** en SQLite, y este DDL corre en los dos. La capa
    # dual de LibraCore traduce el PRAGMA a `information_schema.columns`.
    columnas = {fila[1] for fila in conn.execute("PRAGMA table_info(node_identity)").fetchall()}
    if "last_seen_at" not in columnas:
        # Cuándo fue la última vez que este nodo se identificó contra el
        # central. Es el dato del que vive todo el monitoreo: sin él, el central
        # no tiene forma de saber que una sucursal dejó de sincronizar — y un
        # nodo callado se ve exactamente igual que uno al día.
        conn.execute("ALTER TABLE node_identity ADD COLUMN last_seen_at TEXT")
