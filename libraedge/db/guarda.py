"""La guarda del espejo: en el nodo, las tablas que vienen del central no se escriben desde el producto (ADR-019).

El nodo tiene **dos caminos de escritura** sobre una tabla de referencia: el espejo (`MirrorApplier`, que baja lo que el
central decidio) y el producto (el POS, un endpoint, un script). La mitad de espejo del reparto de autoridad dice que el
producto **no** deberia escribir ahi, pero hasta v0.16 eso era una convencion: nada la hacia cumplir. Una escritura
del producto sobre una tabla espejada no falla ni se sube; vive en el nodo hasta que el proximo cambio del central la
pisa, y la diferencia se descubre en el arqueo.

## Que hace

`instalar_guarda_de_espejo(conn, tablas)` pone en cada tabla de la lista un trigger `BEFORE INSERT OR UPDATE OR DELETE`
que levanta `solo_en_el_central` (SQLSTATE `LE001`), salvo cuando la transaccion fijo `libraedge.aplicando = '1'`. Quien
lo fija es `MirrorApplier`, con `SET LOCAL` dentro de la transaccion en la que aplica el cambio: el espejo escribe, el
resto no.

## Es opt-in por tabla, a proposito

La lista la pasa el producto o el motor: **no** son todas las tablas de referencia. Las altas de clientes en el nodo
todavia no estan resueltas (hoy se crean en el nodo aunque `clientes` se espeje), y guardar `clientes` romperia el POS.
Se guarda lo que ya esta decidido que el nodo no toca --catalogo, precios, usuarios--, y la lista crece a medida que
cada tabla queda resuelta.

## Desde v0.18: las filas provisorias de una tabla con alta pasan (ADR-021)

Una tabla con alta en el nodo (`libraedge.altas`, p. ej. `clients`) puede guardarse igual: el trigger de esa tabla
recibe su columna clave y **deja pasar las filas con id negativo** (las altas provisorias del nodo y su edicion o
baja), y sigue rechazando todo lo que toque una fila del central. `UPDATE` exige que el id anterior **y** el nuevo sean
provisorios: no se puede convertir una fila del central en provisoria ni al reves.

## Es una red de seguridad, no una frontera de seguridad

Cualquiera con acceso a la base puede hacer `SET libraedge.aplicando = '1'` y escribir. La guarda atrapa el **error del
programador** (el endpoint que escribe donde no debe), no un atacante. Lo que si evita es que ese error pase en
silencio.

## Lo que hay que saber

- **Una migracion de datos sobre una tabla guardada falla** en el nodo. Las migraciones de esquema (`ALTER`,
  `CREATE`) no disparan triggers de fila; las que hacen `INSERT`/`UPDATE` sobre la tabla tienen que llamar a
  `marcar_aplicando(conn)` antes. `TRUNCATE` no se guarda.
- **Va solo en el nodo.** En el central la misma base se escribe desde el producto: guardarla ahi lo dejaria sin poder
  trabajar. La instala `libraedge-nodo` al arrancar, nunca `publicar`.
- **SQLite no tiene guarda.** El nodo real corre PostgreSQL; SQLite queda para tests y para el unico consumidor que
  lo usa asi (LibraCommerce). No tiene variable de sesion que consultar, y emularla pide una tabla bandera que el
  aplicador prenda y apague en cada cambio. Las funciones son no-op sobre SQLite y devuelven `()` en vez de fingir que
  protegen.
"""

import sqlite3

from libraedge.db.changelog import validar_identificador

#: Lo que dice el error. Es el texto **estable** que un test o un log puede buscar; no cambiarlo.
MENSAJE = "solo_en_el_central"
#: El SQLSTATE propio. Clase `LE` ("LibraEdge"): no es una clase estandar, asi que psycopg lo levanta como
#: `DatabaseError` pelado --no `IntegrityError`-- y un `except IntegrityError` del producto no se lo traga.
SQLSTATE = "LE001"
#: La variable de sesion que abre la guarda. `SET LOCAL` la limita a la transaccion de quien la fija.
VARIABLE = "libraedge.aplicando"

#: Un solo lugar para el sufijo: lo comparten instalar, desinstalar y el listado (mismo criterio que el changelog).
_SUFIJO_TRIGGER = "_libraedge_guarda"

_FUNCION_GUARDA = f"""
CREATE OR REPLACE FUNCTION libraedge_guarda_de_espejo() RETURNS trigger AS $libraedge$
DECLARE
    antes text;
    despues text;
BEGIN
    IF COALESCE(current_setting('{VARIABLE}', true), '') = '1' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    -- Una tabla con alta en el nodo trae su columna clave como argumento: las filas con id negativo pasan.
    IF TG_NARGS > 0 THEN
        IF TG_OP <> 'INSERT' THEN
            antes := to_jsonb(OLD) ->> TG_ARGV[0];
        END IF;
        IF TG_OP <> 'DELETE' THEN
            despues := to_jsonb(NEW) ->> TG_ARGV[0];
        END IF;
        IF (antes IS NULL OR antes ~ '^-[0-9]+$') AND (despues IS NULL OR despues ~ '^-[0-9]+$') THEN
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION '{MENSAJE}: la tabla % se espeja del central y no se escribe en el nodo (% rechazado)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = '{SQLSTATE}',
              HINT = 'La escribe el central; el nodo la recibe por el espejo (LibraEdge, ADR-019).';
END;
$libraedge$ LANGUAGE plpgsql;
"""


def _es_sqlite(conn) -> bool:
    """Igual que en `changelog.py`: se le pregunta al objeto en la mano, no a LibraCore (que no se importa)."""
    return isinstance(conn, sqlite3.Connection)


def _tabla_existe(conn, tabla: str) -> bool:
    return conn.execute("SELECT to_regclass(?) IS NOT NULL", (tabla,)).fetchone()[0]


def tablas_con_guarda(conn) -> tuple[str, ...]:
    """Las tablas que HOY tienen instalada la guarda, leidas del catalogo de PostgreSQL y no de una lista en codigo."""
    if _es_sqlite(conn):
        return ()
    filas = conn.execute(
        """SELECT c.relname
             FROM pg_trigger t
             JOIN pg_class c ON c.oid = t.tgrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal AND n.nspname = current_schema() AND t.tgname LIKE ?
            ORDER BY c.relname""",
        ("%\\_libraedge\\_guarda",),
    ).fetchall()
    return tuple(fila[0] for fila in filas)


def desinstalar_guarda_de_espejo(conn, tabla: str) -> None:
    """Saca la guarda de `tabla`. Idempotente (`DROP TRIGGER IF EXISTS`)."""
    if _es_sqlite(conn):
        return
    tabla = validar_identificador(tabla)
    conn.execute(f"DROP TRIGGER IF EXISTS {tabla}{_SUFIJO_TRIGGER} ON {tabla}")


def instalar_guarda_de_espejo(conn, tablas, claves_de_alta=None) -> tuple[str, ...]:
    """Pone la guarda en cada tabla de `tablas` y **converge**: la saca de las que ya no estan en la lista.

    `claves_de_alta` (`{tabla: columna_clave}`, v0.18) son las tablas **con alta en el nodo**: su guarda deja pasar las
    filas con id negativo. Una que no esta en `tablas` no se guarda (la lista de solo lectura manda).

    Devuelve las tablas que quedaron guardadas, en orden. Una tabla que no esta en esta base (el producto la declara y
    una migracion todavia no la creo) se saltea y **no** aparece en el resultado: quien llama compara contra lo que
    pidio y avisa. Idempotente, como `instalar_trigger`: se corre en cada arranque del nodo.

    No commitea: es DDL transaccional en PostgreSQL y el commit es de quien llama. Sobre SQLite no hace nada y devuelve
    `()` (ver el docstring del modulo).

    Sacar una tabla de la lista **desinstala** su guarda. Sin esa convergencia, una tabla que el producto resuelve y
    saca de la lista seguiria bloqueada hasta que alguien tocara el trigger a mano.
    """
    pedidas = tuple(sorted({validar_identificador(tabla) for tabla in tablas}))
    claves_de_alta = {validar_identificador(t): validar_identificador(c) for t, c in (claves_de_alta or {}).items()}
    if _es_sqlite(conn):
        return ()
    guardadas = []
    for tabla in pedidas:
        if not _tabla_existe(conn, tabla):
            continue
        if not guardadas:
            conn.execute(_FUNCION_GUARDA)
        conn.execute(f"DROP TRIGGER IF EXISTS {tabla}{_SUFIJO_TRIGGER} ON {tabla}")
        argumento = f"'{claves_de_alta[tabla]}'" if tabla in claves_de_alta else ""
        conn.execute(
            f"CREATE TRIGGER {tabla}{_SUFIJO_TRIGGER}"
            f" BEFORE INSERT OR UPDATE OR DELETE ON {tabla}"
            f" FOR EACH ROW EXECUTE FUNCTION libraedge_guarda_de_espejo({argumento})"
        )
        guardadas.append(tabla)
    for sobrante in tablas_con_guarda(conn):
        if sobrante not in pedidas:
            desinstalar_guarda_de_espejo(conn, sobrante)
    return tuple(guardadas)


def marcar_aplicando(conn) -> None:
    """Abre la guarda **para la transaccion en curso** (`SET LOCAL`). No-op sobre SQLite.

    La llama `MirrorApplier.aplicar` antes de cada escritura. Tambien es para una migracion de datos sobre una tabla
    guardada. Vale hasta el `COMMIT` o `ROLLBACK`: `PullWorker` commitea despues de cada cambio, asi que se vuelve a
    fijar en cada `aplicar` y la guarda nunca queda abierta mas alla de lo que el espejo esta escribiendo.
    """
    if _es_sqlite(conn):
        return
    conn.execute(f"SET LOCAL {VARIABLE} = '1'")
