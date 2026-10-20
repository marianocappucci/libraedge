"""El código de vinculación: el nodo se activa con «dirección + código» en lugar del archivo de carga (ADR-014).

Hasta v0.13 el administrador bajaba ``carga-nodo.json`` de la pantalla «Sucursal sin internet» (ADR-011) y lo llevaba
a la PC del local. Desde v0.14 la pantalla muestra **la dirección del sistema y un código corto** que se escribe en el
instalador; el instalador canjea el código por la carga contra una ruta pública del central y la escribe con permisos
de secreto, como si la hubiera elegido. El archivo queda como alternativa para soporte.

Este módulo es la mitad que no depende de FastAPI: el código (alfabeto, formato, hash), lo que se guarda, el canje
atómico y el límite de intentos. El router está en ``pantalla_del_central``.

## El código

- **8 símbolos** de un alfabeto de 31 sin caracteres confusos (sin ``0 O 1 I L``), en dos grupos: ``K7Q4-M2XP``.
  31^8 ≈ 8,5 × 10^11 códigos (≈ 39,6 bits), generados con ``secrets``.
- **De un solo uso, vence a los 15 minutos** y se puede anular. Pedir otro para el mismo nodo anula los pendientes.
- **Se guarda sólo su hash** (SHA-256 del código normalizado), como los tokens de reseteo de libraauth: de la base no
  sale ningún código usable. Con 40 bits el hash solo no aguanta una búsqueda exhaustiva si alguien se lleva la base;
  lo que lo hace aceptable es que el código vale 15 minutos y una vez. Quien lee la base del central ya tiene más que
  eso.
- El canje **no distingue** «no existe», «vencido», «usado» ni «anulado»: un solo error, para que quien prueba códigos
  al azar no sepa cuál estuvo cerca (el mismo criterio que ``InvalidResetToken`` de libraauth).

## El límite de intentos

Cuenta los canjes **fallidos** en una ventana de 15 minutos, por IP (10) y en total (60), en la base del central
(``libraedge_canje_fallido``): lo ven todos los procesos y sobrevive a un reinicio. Pasado cualquiera de los dos, el
canje contesta 429 **sin mirar el código** (no se le confirma nada a quien está bloqueado) y ese intento no se anota.

Por qué alcanza: el tope global vale aunque el atacante reparta los intentos entre muchas IP. En los 15 minutos de vida
de un código entran, como mucho, dos ventanas: ≤ 120 intentos. La probabilidad de acertar un código vivo es
≤ 120 / 31^8 ≈ 1,4 × 10^-10; con diez códigos vivos a la vez, ≈ 1,4 × 10^-9. Sostenido un año entero sin pausa (≈ 2,1
millones de intentos) daría ≈ 2,5 × 10^-6 por código vivo permanente, y un código no vive más de 15 minutos.

El costo es que quien sature el tope global frena también al instalador legítimo durante 15 minutos (429 con
``Retry-After``). Para eso está el archivo de carga, que no pasa por esta ruta.

🔴 **Nada de esto se loguea con el código ni con el secreto.** Los logs dicen IP, resultado y ``node_id``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("libraedge.vinculacion")

#: Sin ``0 O 1 I L``: lo que se confunde al dictarlo o al leerlo en pantalla.
ALFABETO = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
#: Símbolos del código, sin el guion.
LARGO = 8
#: Cuánto vale un código desde que se emite.
VIGENCIA = timedelta(minutes=15)
#: La ventana del límite de intentos.
VENTANA = timedelta(minutes=15)
#: Canjes fallidos por IP dentro de la ventana.
MAXIMO_POR_IP = 10
#: Canjes fallidos en total dentro de la ventana: el que acota la probabilidad de adivinar (ver el docstring).
MAXIMO_GLOBAL = 60
#: Cuánto se guardan los códigos ya usados, vencidos o anulados antes de borrarlos.
RETENCION = timedelta(days=1)

#: La única respuesta de un canje que no sirve, sea cual sea el motivo.
MENSAJE_CODIGO_INVALIDO = (
    "El código no es válido o ya venció. Pedí otro en la pantalla «Sucursal sin internet» del sistema (vale 15 "
    "minutos y una sola vez).")


def init_vinculacion_schema(conn) -> None:
    """Las dos tablas del canje. Las llama ``init_schema``: viven en la base del producto, con ``node_identity``.

    En un nodo quedan vacías (el nodo nunca emite códigos), igual que ``sync_inbox`` en una instancia sin nodos.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS libraedge_vinculacion (
            codigo_hash TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            emitido_at TEXT NOT NULL,
            vence_at TEXT NOT NULL,
            usado_at TEXT,
            anulado_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_libraedge_vinculacion_node
            ON libraedge_vinculacion(node_id);

        CREATE TABLE IF NOT EXISTS libraedge_canje_fallido (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            intentado_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_libraedge_canje_fallido_at
            ON libraedge_canje_fallido(intentado_at);
        """
    )


# ── el código ───────────────────────────────────────────────────────────────


def generar_codigo() -> str:
    """``K7Q4-M2XP``: 8 símbolos de :data:`ALFABETO` con ``secrets``, en dos grupos de cuatro."""
    simbolos = "".join(secrets.choice(ALFABETO) for _ in range(LARGO))
    return f"{simbolos[:4]}-{simbolos[4:]}"


def normalizar_codigo(codigo: str) -> str | None:
    """Lo que escribió la persona, sin espacios ni guiones y en mayúsculas; ``None`` si no puede ser un código.

    No se «corrige» nada (una ``O`` no pasa a ser un ``0``): ninguno de los dos está en el alfabeto, y adivinar qué
    quiso escribir agranda el espacio que se le acepta a quien prueba al azar.
    """
    limpio = "".join(c for c in (codigo or "") if c not in " -\t").upper()
    if len(limpio) != LARGO or any(c not in ALFABETO for c in limpio):
        return None
    return limpio


def hash_del_codigo(codigo: str) -> str:
    """SHA-256 del código normalizado. ``ValueError`` si no es un código."""
    normalizado = normalizar_codigo(codigo)
    if normalizado is None:
        raise ValueError("no es un código de vinculación")
    return hashlib.sha256(normalizado.encode("ascii")).hexdigest()


def _marca(momento: datetime) -> str:
    """ISO 8601 en UTC con segundos: el mismo largo siempre, así que el orden de texto es el orden del tiempo."""
    return momento.astimezone(UTC).isoformat(timespec="seconds")


def _ahora(ahora: datetime | None) -> datetime:
    return (ahora or datetime.now(UTC)).astimezone(UTC)


# ── emitir, anular, consultar ───────────────────────────────────────────────


@dataclass(frozen=True)
class CodigoEmitido:
    """Lo que ve el administrador. ``codigo`` existe sólo en esta respuesta: la base guarda el hash."""

    codigo: str
    node_id: str
    vence: str


def anular_pendientes(conn, node_id: str, *, ahora: datetime | None = None) -> int:
    """Anula los códigos sin usar del nodo (vencidos o no). Devuelve cuántos. **No hace commit.**"""
    momento = _marca(_ahora(ahora))
    cursor = conn.execute(
        "UPDATE libraedge_vinculacion SET anulado_at = ? "
        "WHERE node_id = ? AND usado_at IS NULL AND anulado_at IS NULL",
        (momento, node_id),
    )
    return max(int(getattr(cursor, "rowcount", 0) or 0), 0)


def emitir(conn, node_id: str, *, ahora: datetime | None = None) -> CodigoEmitido:
    """Un código nuevo para ``node_id``: anula los pendientes del nodo y guarda el hash. **No hace commit.**

    Quien llama ya validó que el nodo existe y está activo, y hace el commit junto con lo demás del alta.
    """
    momento = _ahora(ahora)
    conn.execute("DELETE FROM libraedge_vinculacion WHERE vence_at < ?", (_marca(momento - RETENCION),))
    anular_pendientes(conn, node_id, ahora=momento)
    vence = _marca(momento + VIGENCIA)
    for _ in range(8):
        codigo = generar_codigo()
        codigo_hash = hash_del_codigo(codigo)
        ya = conn.execute("SELECT 1 FROM libraedge_vinculacion WHERE codigo_hash = ?", (codigo_hash,)).fetchone()
        if ya is None:
            conn.execute(
                "INSERT INTO libraedge_vinculacion (codigo_hash, node_id, emitido_at, vence_at) VALUES (?, ?, ?, ?)",
                (codigo_hash, node_id, _marca(momento), vence),
            )
            logger.info("Código de vinculación emitido para el nodo %s (vence %s).", node_id, vence)
            return CodigoEmitido(codigo, node_id, vence)
    raise RuntimeError("no se pudo generar un código de vinculación libre")  # pragma: no cover - 8 choques en 31^8


def pendientes(conn, *, ahora: datetime | None = None) -> dict[str, str]:
    """``{node_id: vence}`` de los códigos vivos (sin usar, sin anular, sin vencer). El último de cada nodo."""
    filas = conn.execute(
        "SELECT node_id, MAX(vence_at) FROM libraedge_vinculacion "
        "WHERE usado_at IS NULL AND anulado_at IS NULL AND vence_at > ? GROUP BY node_id",
        (_marca(_ahora(ahora)),),
    ).fetchall()
    return {str(f[0]): str(f[1]) for f in filas}


# ── el canje y el límite ────────────────────────────────────────────────────


class DemasiadosIntentos(Exception):
    """Se pasó el tope de canjes fallidos (por IP o global). ``reintentar_en`` son segundos."""

    def __init__(self, reintentar_en: int):
        super().__init__("demasiados intentos")
        self.reintentar_en = reintentar_en


def verificar_limite(conn, ip: str, *, ahora: datetime | None = None,
                     maximo_por_ip: int = MAXIMO_POR_IP, maximo_global: int = MAXIMO_GLOBAL) -> None:
    """``DemasiadosIntentos`` si la IP o el total pasaron el tope en la ventana. Se mira ANTES del código."""
    momento = _ahora(ahora)
    desde = _marca(momento - VENTANA)
    total = int(conn.execute(
        "SELECT COUNT(*) FROM libraedge_canje_fallido WHERE intentado_at > ?", (desde,)).fetchone()[0])
    de_la_ip = int(conn.execute(
        "SELECT COUNT(*) FROM libraedge_canje_fallido WHERE intentado_at > ? AND ip = ?", (desde, ip)).fetchone()[0])
    if de_la_ip >= maximo_por_ip or total >= maximo_global:
        filtro = "" if total >= maximo_global else " AND ip = ?"
        parametros = (desde,) if total >= maximo_global else (desde, ip)
        primero = conn.execute(
            f"SELECT MIN(intentado_at) FROM libraedge_canje_fallido WHERE intentado_at > ?{filtro}", parametros,
        ).fetchone()[0]
        libera = datetime.fromisoformat(str(primero)) + VENTANA if primero else momento + VENTANA
        segundos = max(int((libera - momento).total_seconds()) + 1, 1)
        logger.warning("Canje de código bloqueado por el límite de intentos (ip %s, %s de la ip, %s en total).",
                       ip, de_la_ip, total)
        raise DemasiadosIntentos(segundos)


def anotar_fallo(conn, ip: str, *, ahora: datetime | None = None) -> None:
    """Un canje fallido más (y se borran los que ya salieron de la ventana). **No hace commit.**"""
    momento = _ahora(ahora)
    conn.execute("DELETE FROM libraedge_canje_fallido WHERE intentado_at <= ?", (_marca(momento - VENTANA),))
    conn.execute("INSERT INTO libraedge_canje_fallido (ip, intentado_at) VALUES (?, ?)", (ip[:64], _marca(momento)))


def canjear(conn, codigo: str, *, ahora: datetime | None = None) -> str | None:
    """Marca el código como usado **en una sola sentencia** y devuelve su ``node_id``; ``None`` si no sirve.

    ``UPDATE ... WHERE usado_at IS NULL ... RETURNING``: con dos canjes simultáneos del mismo código, PostgreSQL
    bloquea la fila en el primero y el segundo, al reevaluar el ``WHERE`` después del commit, no encuentra nada
    (SQLite serializa las escrituras de toda la base). **No hace commit**: quien llama confirma junto con el registro
    del nodo, así que si eso falla el código no queda gastado.
    """
    try:
        codigo_hash = hash_del_codigo(codigo)
    except ValueError:
        return None
    fila = conn.execute(
        "UPDATE libraedge_vinculacion SET usado_at = ? "
        "WHERE codigo_hash = ? AND usado_at IS NULL AND anulado_at IS NULL AND vence_at > ? "
        "RETURNING node_id",
        (_marca(_ahora(ahora)), codigo_hash, _marca(_ahora(ahora))),
    ).fetchone()
    return None if fila is None else str(fila[0])
