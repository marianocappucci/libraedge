import sqlite3


def init_schema(conn: sqlite3.Connection) -> None:
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
            secret_hash TEXT NOT NULL DEFAULT ''
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
