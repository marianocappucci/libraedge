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
    if "prefijo" not in columnas:
        # Desde v0.8 (ADR-008): el prefijo de numeracion del nodo (`S02`), que
        # el central asigna al registrarlo y va adelante de cada numero de venta
        # del nodo. Es unico por NODO, no por sucursal, y no se reusa nunca: dos
        # PC de una sucursal, o una PC reemplazada, numerarian igual. Nulable:
        # los nodos de antes no lo tienen hasta que se los vuelve a registrar.
        conn.execute("ALTER TABLE node_identity ADD COLUMN prefijo TEXT")
    # Varios NULL conviven en un indice unico, en SQLite y en PostgreSQL.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_node_identity_prefijo ON node_identity(prefijo)")

    # Desde v0.7 el central guarda **la operacion entera** de lo que no pudo
    # aplicar, para poder revisarla, reintentarla o descartarla
    # (`libraedge.sync.revision`). Antes un rechazo no dejaba nada de este lado:
    # la unica copia estaba en el outbox del nodo, en la PC del local.
    columnas = {fila[1] for fila in conn.execute("PRAGMA table_info(sync_inbox)").fetchall()}
    for columna, tipo in _COLUMNAS_DE_REVISION:
        if columna not in columnas:
            conn.execute(f"ALTER TABLE sync_inbox ADD COLUMN {columna} {tipo}")

    # Desde v0.14 (ADR-014): los códigos de vinculación del central y sus canjes fallidos. Van acá y no en una función
    # aparte que cada producto tenga que acordarse de llamar: el arranque y la revisión `*_libraedge` de los tres
    # productos ya llaman a esta, así que las tablas aparecen en las instancias vivas sin una migración nueva.
    from libraedge.vinculacion import init_vinculacion_schema

    init_vinculacion_schema(conn)


#: Las columnas de revision manual de `sync_inbox`. Todas nulables: las filas
#: `applied` de antes de v0.7 no las tienen y no les hacen falta.
_COLUMNAS_DE_REVISION = (
    ("node_id", "TEXT"),
    ("operation_type", "TEXT"),
    ("schema_version", "INTEGER"),
    ("operation_json", "TEXT"),
    ("resuelto_por", "TEXT"),
    ("resuelto_at", "TEXT"),
    ("resolucion", "TEXT"),
)
