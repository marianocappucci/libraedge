"""El changelog del central: de donde sale la bajada.

Esto es lo que le da al nodo la mitad de **espejo**. Hasta la Fase 2 (2026-08-30)
LibraEdge era solo-subida: el campo `node_identity.last_server_cursor` existia en
el esquema desde el primer dia y **ningun archivo lo leia ni lo escribia**, asi
que un nodo podia emitir ventas pero nunca enterarse de un precio nuevo.

## Por que un changelog y no `updated_at > cursor`

Comparar contra una marca de tiempo parece mas simple y tiene tres agujeros que
no se ven hasta que pasan:

- **Obliga a que cada tabla de referencia tenga `updated_at`**, y a mantenerlo en
  cada camino de escritura. Falta uno y esa tabla deja de espejarse en silencio.
- **Dos transacciones pueden commitear fuera de orden de timestamp.** La que
  empezo antes puede hacerse visible despues, asi que un nodo que ya avanzo el
  cursor se saltea esa fila para siempre.
- **No captura los DELETE.** La fila ya no esta para ser consultada.

Un `BIGSERIAL` que solo crece no tiene ninguno de los tres problemas.

## Por que por trigger y no llamando a una funcion en cada escritura

El reparto de autoridad son **26 tablas de referencia** entre LibraCore,
LibraCommerce y el producto. Publicarlas a mano significa encontrar cada camino
de escritura de cada una; el que se olvide no falla, deja de espejarse y nadie se
entera. El trigger no puede saltearse un camino de escritura porque no los
conoce: los intercepta a todos.

El trigger se instala en el **central, al aprovisionar**, y no en las migraciones
del producto: el nodo corre las mismas migraciones y no publica nada.
"""

import decimal
import json
import sqlite3

from libraedge.domain.sync import ReferenceChange, ReferenceOperation


def _es_sqlite(conn) -> bool:
    """Si la conexión es SQLite nativo.

    No se le pregunta a LibraCore —que tiene `is_postgres()`— porque este
    paquete no lo importa en runtime: es dependencia sólo de test, y eso es lo
    que lo mantiene sin deploy key propia. Preguntarle al objeto que ya tenemos
    en la mano alcanza.
    """
    return isinstance(conn, sqlite3.Connection)

#: Los identificadores que aceptamos como nombre de tabla o de columna. Todo lo
#: que se interpola en SQL --y aca se interpola, porque un nombre de tabla no
#: puede ir como parametro-- pasa por esto primero.
_VALIDO = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def validar_identificador(nombre: str) -> str:
    """Devuelve `nombre` si es un identificador SQL seguro, o revienta.

    No es paranoia de mas: los nombres de tabla y columna llegan desde el
    changelog --o sea desde el otro lado de la red-- y se interpolan en el SQL
    del aplicador del nodo, donde un parametro no sirve.
    """
    if not nombre or not set(nombre) <= _VALIDO or nombre[0].isdigit():
        raise ValueError(f"identificador SQL no valido: {nombre!r}")
    return nombre


def cargar_payload(texto: str | None) -> dict | None:
    """Lee el payload de un cambio **sin perder precision decimal**.

    🔴 `json.loads` por defecto convierte todo numero con punto a `float`, y los
    precios de la referencia son `NUMERIC`. Un `19.99` que viaja como numero JSON
    y vuelve como float ya no es exactamente `19.99`, y ese es el dato que el
    nodo va a usar para cobrar durante el corte. `parse_float=Decimal` conserva
    los digitos tal como vinieron en el texto.
    """
    if not texto:
        return None
    return json.loads(texto, parse_float=decimal.Decimal)


def init_changelog_schema(conn) -> None:
    """Crea `sync_changelog`. Idempotente, como el resto del esquema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_changelog (
            cursor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            row_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_json TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_changelog_tabla ON sync_changelog(table_name)"
    )


#: La funcion que ejecuta el trigger. Escribe la fila entera como JSON, asi el
#: nodo la espeja sin saber que significa, y el `row_id` sale de la PK que se le
#: pasa como argumento al trigger.
#:
#: `recorded_at` se arma en UTC explicito y no se deja al `DEFAULT
#: CURRENT_TIMESTAMP` de la tabla: ese default se escribe distinto en cada motor
#: --es el mismo defecto que tenia `created_at` del outbox, encontrado en la
#: Fase 1-- y aca ademas convivirian en la misma tabla las filas del trigger con
#: las de `sembrar()`.
#: El sufijo del trigger, en UN solo lugar. Lo comparten instalar, desinstalar y
#: el listado: si cada uno lo escribiera aparte, desinstalar podría no encontrar
#: lo que instalar dejó puesto, y el listado no vería ninguno de los dos.
_SUFIJO_TRIGGER = "_libraedge_changelog"

_FUNCION_TRIGGER = """
CREATE OR REPLACE FUNCTION libraedge_registrar_cambio() RETURNS trigger AS $libraedge$
DECLARE
    fila jsonb;
    op text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        fila := to_jsonb(OLD); op := 'delete';
    ELSE
        fila := to_jsonb(NEW); op := 'upsert';
    END IF;
    INSERT INTO sync_changelog (table_name, row_id, operation, payload_json, recorded_at)
    VALUES (
        TG_TABLE_NAME, fila ->> TG_ARGV[0], op, fila::text,
        to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
    );
    RETURN NULL;
END;
$libraedge$ LANGUAGE plpgsql;
"""


def instalar_trigger(conn, tabla: str, pk: str = "id") -> None:
    """Publica `tabla` al changelog. Solo PostgreSQL, y solo en el central.

    Idempotente: se puede correr en cada aprovisionamiento sin duplicar el
    trigger ni los cambios que emite.
    """
    tabla = validar_identificador(tabla)
    pk = validar_identificador(pk)
    conn.execute(_FUNCION_TRIGGER)
    conn.execute(f"DROP TRIGGER IF EXISTS {tabla}{_SUFIJO_TRIGGER} ON {tabla}")
    conn.execute(
        f"CREATE TRIGGER {tabla}{_SUFIJO_TRIGGER}"
        f" AFTER INSERT OR UPDATE OR DELETE ON {tabla}"
        f" FOR EACH ROW EXECUTE FUNCTION libraedge_registrar_cambio('{pk}')"
    )


def tablas_publicadas(conn) -> tuple[str, ...]:
    """Las tablas que HOY tienen instalado el trigger del changelog.

    Se lee del catálogo de PostgreSQL y no de una lista en código, por el mismo
    motivo por el que la PK se lee de la base: lo que está instalado es un hecho
    del servidor, y una lista escrita a mano se desactualiza en silencio.
    """
    if _es_sqlite(conn):
        return ()
    filas = conn.execute(
        "SELECT DISTINCT event_object_table FROM information_schema.triggers"
        " WHERE trigger_name LIKE ?"
        " ORDER BY event_object_table",
        ("%" + _SUFIJO_TRIGGER,),
    ).fetchall()
    return tuple(fila[0] for fila in filas)


def desinstalar_trigger(conn, tabla: str) -> None:
    """Deja de publicar `tabla` al changelog.

    🔴 La contracara de `instalar_trigger`, y no existía. Sacar una tabla de la
    lista de un producto **no desinstala** lo que un aprovisionamiento anterior
    ya dejó puesto: el trigger sigue ahí, escribiendo al changelog de una tabla
    que el nodo ya no espera. Pasó en el central de demo de Restolibra el
    2026-08-31, con tres tablas.

    Idempotente: `DROP TRIGGER IF EXISTS`, así que correrlo de más no rompe.
    """
    tabla = validar_identificador(tabla)
    conn.execute(
        f"DROP TRIGGER IF EXISTS {tabla}{_SUFIJO_TRIGGER} ON {tabla}"
    )


def sembrar(conn, tabla: str, pk: str = "id", ahora: str | None = None) -> int:
    """Vuelca el estado actual de `tabla` al changelog, como `upsert`.

    **Este es el snapshot inicial, y a proposito no es un mecanismo aparte.** Un
    nodo nuevo arranca con cursor 0 y pide todo desde ahi; si el changelog trae
    la foto de la tabla al momento de sembrarla, el alta del nodo y una
    actualizacion cualquiera recorren exactamente el mismo camino. Un segundo
    mecanismo para el arranque seria un segundo lugar donde equivocarse, y el que
    menos se ejercita.

    Devuelve cuantas filas se sembraron.
    """
    from datetime import datetime, timezone

    tabla = validar_identificador(tabla)
    pk = validar_identificador(pk)
    ahora = ahora or datetime.now(timezone.utc).isoformat()

    if _es_sqlite(conn):
        cursor = conn.execute(f"SELECT * FROM {tabla}")
        columnas = [descripcion[0] for descripcion in cursor.description]
        filas = [
            (str(dict(zip(columnas, fila))[pk]),
             json.dumps(dict(zip(columnas, fila)), default=str, sort_keys=True))
            for fila in cursor.fetchall()
        ]
    else:
        # 🔴 **La misma via que el trigger, y no `SELECT *`, a proposito.**
        # La capa de conexion de LibraCore degrada los `NUMERIC` a `float` al
        # leer -- es una decision deliberada de la familia, documentada en
        # `_como_en_sqlite`: todo el codigo hace aritmetica con float y mezclar
        # `Decimal` revienta--. Pero `to_jsonb(...)::text` sale como TEXTO y no
        # pasa por esa conversion, asi que conserva los digitos exactos.
        #
        # Sin esto, el snapshot inicial traeria los precios redondeados por
        # float y las actualizaciones posteriores --que vienen del trigger--
        # exactos: **el mismo precio escrito de dos formas segun por que camino
        # llego**, y el redondeado seria el que queda en un nodo recien
        # instalado.
        filas = [
            (fila[0], fila[1])
            for fila in conn.execute(
                f"SELECT (to_jsonb(t) ->> '{pk}'), to_jsonb(t)::text FROM {tabla} t"
            ).fetchall()
        ]

    for row_id, payload_json in filas:
        conn.execute(
            """INSERT INTO sync_changelog
                   (table_name, row_id, operation, payload_json, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tabla, row_id, str(ReferenceOperation.UPSERT), payload_json, ahora),
        )
    return len(filas)


def listar_cambios(conn, desde: int = 0, limit: int = 500) -> tuple[ReferenceChange, ...]:
    """Los cambios posteriores a `desde`, en orden de cursor.

    El orden **es** la garantia: si el nodo aplica en este orden y guarda el
    cursor del ultimo aplicado, no se saltea ninguno.
    """
    filas = conn.execute(
        """SELECT cursor_id, table_name, row_id, operation, payload_json, recorded_at
           FROM sync_changelog WHERE cursor_id > ?
           ORDER BY cursor_id LIMIT ?""",
        (desde, limit),
    ).fetchall()
    return tuple(
        ReferenceChange(
            cursor=fila[0], table_name=fila[1], row_id=fila[2],
            operation=ReferenceOperation(fila[3]),
            payload=cargar_payload(fila[4]),
            recorded_at=fila[5],
        )
        for fila in filas
    )
