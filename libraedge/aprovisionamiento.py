"""El aprovisionamiento del nodo, del lado del CENTRAL (ADR-008).

Los pasos que se dan una vez por sucursal, antes de instalar nada en la PC del
local, ahora genericos para toda la suite::

    libraedge-central nodo publicar                    # triggers del changelog y siembra
    libraedge-central nodo sucursales                  # las sucursales y cual ya tiene nodo
    libraedge-central nodo registrar node-1 --sucursal Centro   # alta y secreto (una vez)
    libraedge-central nodo estado                      # que hay publicado y que nodos existen
    libraedge-central nodo dar-de-baja node-1          # PC robada o reemplazada
    libraedge-central nodo vigilar --umbral 15         # sale 1 si un nodo activo se callo

Nacio en ``restolibra/scripts/nodo_offline.py`` (el piloto) y casi todo era
generico. Lo unico que es del producto entra por una costura chica y
declarativa, ``Aprovisionamiento``:

- **las tablas de referencia** -- el reparto de autoridad: que manda el central
  y el nodo solo espeja. LibraEdge no puede saberlo y esa ignorancia es lo que
  lo hace reusable. La clave se puede declarar (simple o compuesta) o dejar que
  se lea de la base; si se declara, **se verifica contra la base**: el piloto
  aprendio que una PK escrita a mano se equivoca en silencio (``units`` tiene PK
  ``code``, no ``id``);
- **como listar las sucursales** de la instancia (``id``, ``nombre``, ``activa``);
- **la guarda del adicional** ``offline`` (libracore ADR-048). Por defecto la de
  libracore, importada recien al registrar: LibraEdge no depende de libracore en
  runtime;
- **el prefijo** de las variables del producto (``RESTOLIBRA`` ->
  ``RESTOLIBRA_DATABASE_URL``, ``RESTOLIBRA_EDGE_NODE_ID``).

Lo que no se publica no es un olvido: las tablas de autoridad del nodo (ventas,
caja, stock) las genera el nodo y las sube; publicarlas haria que el central se
las devolviera y el nodo se pisara sus propios datos. Las de fuera de alcance
(ARCA, Mercado Pago) tampoco: espejarlas daria la impresion de que andan sin
conexion.
"""

import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from libraedge.db.changelog import columnas_de_clave, validar_identificador

Salida = Callable[[str], None]


# ── errores ─────────────────────────────────────────────────────────────────

class ErrorDeAprovisionamiento(RuntimeError):
    """Un paso del aprovisionamiento no se puede dar. ``str(exc)`` es para quien instala."""


class SucursalInvalida(ErrorDeAprovisionamiento):
    """La sucursal pedida no existe, esta inactiva, es ambigua o no se pudo elegir."""


class SegundoNodoEnLaSucursal(ErrorDeAprovisionamiento):
    """Se intento registrar un segundo nodo activo en una sucursal que ya tiene."""


class AltaRechazada(ErrorDeAprovisionamiento):
    """La guarda del producto (el adicional ``offline``) no deja dar el alta."""


# ── la costura del producto ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Tabla:
    """Una tabla de referencia. ``clave=None``: se lee de la base."""

    nombre: str
    clave: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Sucursal:
    id: str
    nombre: str
    activa: bool = True
    #: El nodo activo de la sucursal (el menor ``node_id`` si hubiera varios).
    nodo: str | None = None


def sin_guarda(conn, node_id: str, branch_id: str) -> None:
    """Para un producto que no cobra el offline como adicional: no frena nada."""


def guarda_offline_de_libracore(plans: Any = "plans", clave: str = "offline",
                                cfg: Callable[[], dict] | None = None):
    """La guarda del adicional ``offline`` de libracore (ADR-048), como callable.

    - Sin el modulo prendido en ``modulos``: no se registra ningun nodo.
    - Si la sucursal ya tiene un nodo activo (otro, o este mismo al
      re-registrarlo) no suma unidad: solo cuenta que el modulo siga prendido.
      Si el contrato se achico por debajo de lo instalado, reemplazar la PC de
      una sucursal que ya estaba no puede quedar bloqueado.
    - Si es una sucursal nueva, las sucursales distintas con nodo + 1 no pueden
      superar las contratadas. Sin cantidad cargada no hay tope.

    ``plans`` es el modulo de planes del producto o su nombre (se importa al
    registrar). ``cfg`` devuelve el ``config.json`` de la instancia; sin pasarlo
    lo lee libracore. Los modulos se leen con ``conn``, la conexion del alta,
    para que la cuenta y el alta vean lo mismo.
    """

    def verificar(conn, node_id: str, branch_id: str) -> None:
        try:
            from libracore.adicionales import AdicionalError, verificar_alta  # noqa: PLC0415
        except ImportError as exc:
            raise AltaRechazada(
                "No hay guarda del adicional offline: libracore no esta instalado. "
                "El producto tiene que pasar `verificar_alta` (o `sin_guarda` si no "
                "cobra el offline).") from exc
        modulo_plans = import_module(plans) if isinstance(plans, str) else plans
        modulos = {fila[0]: bool(fila[1])
                   for fila in conn.execute("SELECT modulo, habilitado FROM modulos").fetchall()}
        ya_tiene = sucursal_tiene_nodo(conn, branch_id)
        usadas = 0 if ya_tiene else sucursales_con_nodo(conn)
        try:
            verificar_alta(modulo_plans, clave, usadas, sumar=0 if ya_tiene else 1,
                           cfg=None if cfg is None else cfg(), modulos=modulos)
        except AdicionalError as exc:
            raise AltaRechazada(str(exc)) from exc

    return verificar


@dataclass
class Aprovisionamiento:
    """Lo que el producto declara para que el central aprovisione sus nodos.

    ``tablas_de_referencia`` acepta nombres (``"units"``), pares
    ``(nombre, clave)`` con clave ``"id"``, ``"a+b"`` o ``("a", "b")``, un
    ``{nombre: clave | None}`` o ``Tabla``. ``listar_sucursales(conn)`` devuelve
    ``Sucursal`` o dicts con ``id``, ``nombre`` (o ``name``) y ``activa`` (o
    ``active``): ``erp.catalogo.get_all_sucursales`` de LibraCommerce sirve tal cual.
    ``verificar_alta(conn, node_id, branch_id)`` levanta ``AltaRechazada``.
    ``conectar()`` es opcional: sin ``--base`` el CLI lo usa antes que el entorno.
    """

    prefijo: str
    tablas_de_referencia: Any
    listar_sucursales: Callable[[Any], Iterable]
    verificar_alta: Callable[[Any, str, str], None] = field(
        default_factory=guarda_offline_de_libracore)
    conectar: Callable[[], Any] | None = None
    tablas: tuple[Tabla, ...] = field(init=False)

    def __post_init__(self):
        from libraedge.identidad import variable_del_producto

        variable_del_producto(self.prefijo)  # valida el prefijo
        self.prefijo = self.prefijo.strip().upper()
        self.tablas = normalizar_tablas(self.tablas_de_referencia)

    @property
    def nombres(self) -> frozenset[str]:
        return frozenset(t.nombre for t in self.tablas)


def normalizar_tablas(declaradas) -> tuple[Tabla, ...]:
    """La lista del producto en ``Tabla``, validada y sin repetidas."""
    if isinstance(declaradas, Mapping):
        declaradas = list(declaradas.items())
    tablas: dict[str, Tabla] = {}
    for item in declaradas:
        if isinstance(item, Tabla):
            nombre, clave = item.nombre, item.clave
        elif isinstance(item, str):
            nombre, clave = item, None
        else:
            nombre, clave = item
        nombre = validar_identificador(nombre)
        if nombre in tablas:
            raise ValueError(f"la tabla {nombre!r} esta dos veces en las tablas de referencia")
        tablas[nombre] = Tabla(nombre, None if clave is None else columnas_de_clave(clave))
    if not tablas:
        raise ValueError("las tablas de referencia quedaron vacias: no hay nada que espejar")
    return tuple(tablas[n] for n in sorted(tablas))


def cargar_aprovisionamiento(ruta: str) -> Aprovisionamiento:
    """``"app.offline:aprovisionamiento"`` -> el objeto (o lo que devuelve, si es una funcion)."""
    modulo, _, nombre = (ruta or "").partition(":")
    if not modulo or not nombre:
        raise ErrorDeAprovisionamiento(f"{ruta!r} no es 'modulo:objeto'.")
    objeto = getattr(import_module(modulo), nombre)
    if callable(objeto) and not isinstance(objeto, Aprovisionamiento):
        objeto = objeto()
    if not isinstance(objeto, Aprovisionamiento):
        raise ErrorDeAprovisionamiento(f"{ruta!r} no es un Aprovisionamiento.")
    return objeto


# ── el catalogo de la base ──────────────────────────────────────────────────

def _es_sqlite(conn) -> bool:
    return isinstance(conn, sqlite3.Connection)


def tabla_existe(conn, tabla: str) -> bool:
    if _es_sqlite(conn):
        fila = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (tabla,)
        ).fetchone()
    else:
        fila = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_schema = current_schema() AND table_name = ?", (tabla,)
        ).fetchone()
    return bool(fila and fila[0])


def clave_primaria(conn, tabla: str) -> tuple[str, ...] | None:
    """Las columnas de la PK de ``tabla`` segun la base, en orden; ``None`` sin PK.

    Desde v0.7 el espejo aplica claves compuestas, asi que una PK de varias
    columnas ya no se saltea (el piloto las salteaba).
    """
    tabla = validar_identificador(tabla)
    if _es_sqlite(conn):
        filas = [f for f in conn.execute(f"PRAGMA table_info({tabla})").fetchall() if f[5]]
        filas.sort(key=lambda f: f[5])
        columnas = tuple(f[1] for f in filas)
    else:
        columnas = tuple(f[0] for f in conn.execute(
            """SELECT a.attname
                 FROM pg_index i
                 JOIN pg_attribute a
                   ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = ?::regclass AND i.indisprimary
                ORDER BY array_position(i.indkey::int2[], a.attnum)""",
            (tabla,),
        ).fetchall())
    return columnas or None


def _dependencias(conn, tablas: list[str]) -> dict[str, set[str]]:
    dependencias = {tabla: set() for tabla in tablas}
    if _es_sqlite(conn):
        pares = [(hijo, f[2]) for hijo in tablas
                 for f in conn.execute(f"PRAGMA foreign_key_list({hijo})").fetchall()]
    else:
        pares = conn.execute(
            "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text"
            " FROM pg_constraint c WHERE c.contype = 'f'"
        ).fetchall()
    for hijo, padre in pares:
        if hijo in dependencias and padre in dependencias and hijo != padre:
            dependencias[hijo].add(padre)
    return dependencias


def orden_de_siembra(conn, tablas: list[str]) -> list[str]:
    """``tablas`` ordenadas para que un padre se siembre antes que su hijo.

    🔴 Sin esto el nodo recibe el producto antes que su categoria y rechaza la
    fila por la FK: la bajada queda trabada en ese cursor. Solo hace falta para
    la siembra: despues, la misma FK obliga al central a escribir el padre
    primero, asi que su cambio tiene cursor menor. Una dependencia circular no
    se puede ordenar: esas van al final, en orden estable.
    """
    pendientes = _dependencias(conn, list(tablas))
    ordenadas: list[str] = []
    while pendientes:
        libres = sorted(t for t, deps in pendientes.items() if not (deps - set(ordenadas)))
        if not libres:
            ordenadas.extend(sorted(pendientes))
            break
        ordenadas.extend(libres)
        for tabla in libres:
            pendientes.pop(tabla)
    return ordenadas


@dataclass(frozen=True)
class PlanDePublicacion:
    #: ``(tabla, clave)`` en orden de siembra.
    publicables: tuple[tuple[str, tuple[str, ...]], ...]
    #: Declaradas que no estan en esta base (se saltean e informan).
    faltantes: tuple[str, ...]
    #: ``(tabla, motivo)``: estan pero el espejo no podria aplicarlas. Hacen fallar.
    salteadas: tuple[tuple[str, str], ...]


def plan_de_publicacion(conn, aprov: Aprovisionamiento) -> PlanDePublicacion:
    """Que se publica, con que clave y en que orden. No escribe nada."""
    presentes, faltantes = [], []
    for tabla in aprov.tablas:
        (presentes if tabla_existe(conn, tabla.nombre) else faltantes).append(tabla)
    claves: dict[str, tuple[str, ...]] = {}
    salteadas = []
    for tabla in presentes:
        en_la_base = clave_primaria(conn, tabla.nombre)
        if en_la_base is None:
            salteadas.append((tabla.nombre, "sin clave primaria, el espejo del nodo no podria aplicarla"))
        elif tabla.clave is not None and set(tabla.clave) != set(en_la_base):
            salteadas.append((tabla.nombre, f"la clave declarada es {'+'.join(tabla.clave)} y la "
                                            f"de la base es {'+'.join(en_la_base)}"))
        else:
            claves[tabla.nombre] = tabla.clave or en_la_base
    return PlanDePublicacion(
        publicables=tuple((t, claves[t]) for t in orden_de_siembra(conn, list(claves))),
        faltantes=tuple(t.nombre for t in faltantes),
        salteadas=tuple(salteadas),
    )


def espejo_del_nodo(conn, aprov: Aprovisionamiento) -> dict[str, tuple[str, ...]]:
    """``{tabla: clave}`` de lo que este central publica: lo que el nodo espeja.

    Sale de la **misma** fuente que ``publicar``: si el central publica una tabla
    que el nodo no espeja, el aplicador la rechaza y la bajada se traba.
    """
    return dict(plan_de_publicacion(conn, aprov).publicables)


def formato_tablas_espejo(espejo: Mapping[str, tuple[str, ...]]) -> str:
    """El valor de ``LIBRAEDGE_TABLAS_ESPEJO``: ``a:id,party_roles:party_id+role``."""
    return ",".join(f"{tabla}:{'+'.join(clave)}" for tabla, clave in sorted(espejo.items()))


# ── publicar ────────────────────────────────────────────────────────────────

def publicar(conn, aprov: Aprovisionamiento, salida: Salida = print) -> int:
    """Instala el trigger del changelog en cada tabla de referencia y la siembra.

    Idempotente: el trigger se reinstala sin duplicarse y solo se siembra lo que
    todavia no tiene nada en el changelog (sembrar de nuevo son upserts
    inofensivos, pero engordan el changelog). **Converge**: retira el trigger de
    las tablas que salieron de la lista, leidas del catalogo de PostgreSQL y no
    de otra lista. Sale 1 si alguna tabla presente no se pudo publicar: un
    agujero en el espejo no puede pasar como una linea mas del listado.
    """
    from libraedge.db.changelog import (
        desinstalar_trigger,
        init_changelog_schema,
        instalar_trigger,
        sembrar,
        tablas_publicadas,
    )

    if _es_sqlite(conn):
        raise ErrorDeAprovisionamiento(
            "publicar corre en el central, que es PostgreSQL: el trigger del changelog no existe en SQLite.")

    init_changelog_schema(conn)
    plan = plan_de_publicacion(conn, aprov)
    if plan.faltantes:
        salida(f"  (no estan en esta base, se saltean: {', '.join(plan.faltantes)})")
    for tabla, clave in plan.publicables:
        texto = "+".join(clave)
        instalar_trigger(conn, tabla, clave)
        ya = conn.execute(
            "SELECT COUNT(*) FROM sync_changelog WHERE table_name = ?", (tabla,)).fetchone()[0]
        if ya:
            salida(f"  {tabla}: trigger ok (pk {texto}), ya tenia {ya} cambios")
            continue
        salida(f"  {tabla}: trigger ok (pk {texto}), sembradas {sembrar(conn, tabla, clave)} filas")
    for tabla, motivo in plan.salteadas:
        salida(f"  {tabla}: SALTEADA — {motivo}")

    sobrantes = [t for t in tablas_publicadas(conn) if t not in aprov.nombres]
    for tabla in sobrantes:
        desinstalar_trigger(conn, tabla)
        salida(f"  {tabla}: trigger RETIRADO — ya no esta en la lista")
    conn.commit()

    presentes = len(plan.publicables) + len(plan.salteadas)
    salida(f"Publicadas {len(plan.publicables)} de {presentes} tablas presentes.")
    if sobrantes:
        salida(f"Retirados {len(sobrantes)} triggers de tablas que salieron de la lista.")
    if plan.salteadas:
        nombres = ", ".join(t for t, _ in plan.salteadas)
        salida("")
        salida(f"🔴 {len(plan.salteadas)} tabla(s) sin publicar: {nombres}.")
        salida("El nodo va a espejar una copia INCOMPLETA: o la tabla no hace falta y sale")
        salida("de las tablas de referencia, o hay que corregir su clave.")
        return 1
    return 0


# ── sucursales y alta ───────────────────────────────────────────────────────

def sucursales_con_nodo(conn) -> int:
    """Cuantas sucursales DISTINTAS tienen al menos un nodo activo.

    Es lo que el adicional ``offline`` cobra y limita: varios nodos en una
    sucursal no suman, y uno dado de baja no cuenta.
    """
    fila = conn.execute(
        "SELECT COUNT(DISTINCT branch_id) FROM node_identity WHERE active = 1").fetchone()
    return int(fila[0])


def sucursal_tiene_nodo(conn, branch_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM node_identity WHERE branch_id = ? AND active = 1 LIMIT 1", (branch_id,)
    ).fetchone() is not None


def _como_sucursal(item) -> Sucursal:
    if isinstance(item, Sucursal):
        return item
    nombre = item.get("nombre", item.get("name"))
    activa = item.get("activa", item.get("active", True))
    return Sucursal(id=str(item["id"]), nombre=str(nombre or ""), activa=bool(activa))


def listar_sucursales(conn, aprov: Aprovisionamiento) -> list[Sucursal]:
    """Las sucursales del producto, con el nodo activo de cada una."""
    nodos = {
        fila[0]: fila[1] for fila in conn.execute(
            "SELECT branch_id, MIN(node_id) FROM node_identity WHERE active = 1 GROUP BY branch_id"
        ).fetchall()
    }
    resultado = []
    for item in aprov.listar_sucursales(conn):
        s = _como_sucursal(item)
        resultado.append(Sucursal(s.id, s.nombre, s.activa, nodos.get(s.id)))
    return resultado


def _rotulo(branch_id: str, nombre: str | None) -> str:
    return f"{nombre!r} (id {branch_id})" if nombre else repr(branch_id)


def _lista_para_error(activas: list[Sucursal]) -> str:
    if not activas:
        return ("  (esta instancia no tiene ninguna sucursal activa: crearla primero desde la\n"
                "   pantalla de sucursales de la app)")
    return "\n".join(f"  {s.id:>4}  {s.nombre}" for s in activas)


def _menu(activas: list[Sucursal], entrada: Callable[[str], str], salida: Salida) -> Sucursal:
    salida("Sucursales activas:")
    for numero, s in enumerate(activas, start=1):
        nodo = f"  (ya tiene el nodo {s.nodo!r})" if s.nodo else ""
        salida(f"  {numero}) {s.nombre} (id {s.id}){nodo}")
    while True:
        try:
            respuesta = entrada(f"Elegi la sucursal del nodo [1-{len(activas)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            salida("")
            raise SucursalInvalida("No se eligio ninguna sucursal.") from None
        if respuesta.isdigit() and 1 <= int(respuesta) <= len(activas):
            return activas[int(respuesta) - 1]
        salida(f"  Opcion invalida: {respuesta!r}. Escribi un numero entre 1 y {len(activas)}.")


def elegir_sucursal(conn, aprov: Aprovisionamiento, pedida: str | None, *,
                    interactivo: bool = False, entrada: Callable[[str], str] = input,
                    salida: Salida = print) -> Sucursal:
    """Resuelve ``--sucursal`` a una sucursal **existente y activa**. Nada se adivina ni se crea.

    ``pedida`` es el id o el nombre (sin distinguir mayusculas; el id gana).
    Sin ella: una sola activa se usa (y se avisa), varias se eligen en un menu si
    hay terminal, y sin terminal es un error que lista las validas. Un texto
    libre se equivocaba en silencio: ``Centro`` y ``centro`` eran dos sucursales
    y cada una se llevaba su nodo y su unidad del adicional.
    """
    todas = listar_sucursales(conn, aprov)
    activas = [s for s in todas if s.activa]
    validas = "Sucursales validas (id y nombre):\n" + _lista_para_error(activas)

    if pedida is None:
        if not activas:
            raise SucursalInvalida(
                "Esta instancia no tiene ninguna sucursal activa.\n\n" + _lista_para_error(activas))
        if len(activas) == 1:
            unica = activas[0]
            salida(f"Sin --sucursal: se usa la unica sucursal activa, {_rotulo(unica.id, unica.nombre)}.")
            return unica
        if interactivo:
            return _menu(activas, entrada, salida)
        raise SucursalInvalida(
            "Falta --sucursal y hay mas de una sucursal activa; sin terminal interactiva no se "
            "puede preguntar.\n" + validas)

    texto = pedida.strip()
    coinciden = [s for s in todas if s.id == texto]
    if not coinciden:
        coinciden = [s for s in todas if s.nombre.strip().casefold() == texto.casefold()]
    if len(coinciden) > 1:
        raise SucursalInvalida(f"El nombre {pedida!r} es de mas de una sucursal; usar el id.\n" + validas)
    if not coinciden:
        raise SucursalInvalida(f"La sucursal {pedida!r} no existe en esta instancia.\n" + validas)
    if not coinciden[0].activa:
        raise SucursalInvalida(
            f"La sucursal {_rotulo(coinciden[0].id, coinciden[0].nombre)} esta inactiva.\n" + validas)
    return coinciden[0]


def rechazar_segundo_nodo(conn, node_id: str, branch_id: str, nombre: str | None = None) -> None:
    """🔴 Una sucursal tiene UN nodo; los demas POS son terminales suyas.

    Dos nodos en el mismo local numeran ventas desde su propia base y terminan
    chocando en el central con el ticket ya impreso. Re-registrar el MISMO
    ``node_id`` si es legitimo: es como se reemplaza una PC, y emite un secreto
    nuevo que invalida el anterior. La guarda es del central porque es el unico
    que ve la sucursal entera.
    """
    fila = conn.execute(
        """SELECT node_id FROM node_identity
            WHERE branch_id = ? AND active = 1 AND node_id <> ?
            ORDER BY node_id LIMIT 1""",
        (branch_id, node_id),
    ).fetchone()
    if fila is None:
        return
    raise SegundoNodoEnLaSucursal(
        f"La sucursal {_rotulo(branch_id, nombre)} ya tiene un nodo activo: {fila[0]!r}.\n\n"
        f"Una sucursal tiene UN nodo; los demas POS son terminales que le apuntan por la red\n"
        f"interna, sin instalar nada. Dos nodos numeran ventas en paralelo y terminan chocando\n"
        f"en el central, con el ticket ya impreso.\n\n"
        f"Si de verdad se reemplaza el nodo, dar de baja el anterior primero. Si es la MISMA\n"
        f"PC, registrar con el mismo node_id ({fila[0]!r}): emite un secreto nuevo e invalida el viejo.")


# ── el prefijo de numeracion del nodo ───────────────────────────────────────

#: Lo que acepta ``erp.offline.NodoLocal`` de LibraCommerce: mayusculas y digitos, de 1 a 8.
LARGO_DEL_PREFIJO = 8
_LETRAS = "BCDEFGHIJKLMNOPQRSTUVWXYZ"


def prefijo_base(branch_id: str) -> str:
    """``"2"`` -> ``"S02"``; un id no numerico -> ``"S"`` + sus letras y digitos en mayusculas.

    La base deja lugar para dos letras de sufijo dentro de los 8 caracteres.
    """
    texto = str(branch_id).strip()
    if texto.isdigit():
        cuerpo = f"{int(texto):02d}"
    else:
        cuerpo = "".join(c for c in texto.upper() if c.isalnum() and c.isascii())
    return ("S" + (cuerpo or "X"))[:LARGO_DEL_PREFIJO - 2]


def _candidatos(base: str):
    yield base
    yield from (base + letra for letra in _LETRAS)
    yield from (base + a + b for a in _LETRAS for b in _LETRAS)


def asignar_prefijo(conn, node_id: str, branch_id: str) -> str:
    """El prefijo de numeracion de ``node_id``: el que ya tenia, o uno nuevo.

    🔴 Unico por NODO, no por sucursal, y **nunca se reusa** el de un nodo dado
    de baja: el numero de venta es ``f"{prefijo}-{numerador}"`` y el numerador
    de una PC nueva arranca de cero, asi que un prefijo repetido repite numeros
    que el central ya tiene. El primero de la sucursal 2 es ``S02``; los
    siguientes, ``S02B``, ``S02C``... Re-registrar el mismo ``node_id`` (la
    misma PC con secreto nuevo) conserva el suyo: sigue su propia numeracion.
    """
    fila = conn.execute("SELECT prefijo FROM node_identity WHERE node_id = ?", (node_id,)).fetchone()
    if fila is not None and fila[0]:
        return fila[0]
    usados = {f[0] for f in conn.execute(
        "SELECT prefijo FROM node_identity WHERE prefijo IS NOT NULL").fetchall()}
    for candidato in _candidatos(prefijo_base(branch_id)):
        if candidato not in usados:
            return candidato
    raise ErrorDeAprovisionamiento(  # pragma: no cover - 1 + 25 + 625 nodos en una sucursal
        f"No quedan prefijos libres para la sucursal {branch_id!r}.")


@dataclass(frozen=True)
class Alta:
    node_id: str
    sucursal: Sucursal
    secreto: str
    espejo: dict[str, tuple[str, ...]]
    prefijo: str

    def entorno_del_nodo(self) -> list[str]:
        """Las lineas que van al archivo de entorno del nodo."""
        return [f"LIBRAEDGE_NODE_ID={self.node_id}",
                f"LIBRAEDGE_NODE_SECRET={self.secreto}",
                f"LIBRAEDGE_PREFIJO={self.prefijo}",
                f"LIBRAEDGE_TABLAS_ESPEJO={formato_tablas_espejo(self.espejo)}"]


def _validar_node_id(node_id: str) -> str:
    node_id = (node_id or "").strip()
    if not node_id or any(c.isspace() for c in node_id) or len(node_id) > 64:
        raise ErrorDeAprovisionamiento(f"node_id no valido: {node_id!r} (sin espacios, hasta 64 caracteres)")
    return node_id


def registrar(conn, aprov: Aprovisionamiento, node_id: str, sucursal: str | None = None, *,
              interactivo: bool = False, entrada: Callable[[str], str] = input,
              salida: Salida = print) -> Alta:
    """Da de alta el nodo y devuelve su secreto, **que no se puede recuperar despues**.

    Orden: la sucursal (existente y activa), la guarda del segundo nodo (la mas
    especifica), la del adicional, el prefijo de numeracion y recien ahi se escribe. La guarda del
    adicional solo frena ALTAS: un nodo ya registrado sigue sincronizando
    aunque se de de baja el adicional, porque tiene ventas sin subir.
    """
    from libraedge.db.repository import NodeRepository

    node_id = _validar_node_id(node_id)
    elegida = elegir_sucursal(conn, aprov, sucursal, interactivo=interactivo, entrada=entrada, salida=salida)
    rechazar_segundo_nodo(conn, node_id, elegida.id, elegida.nombre)
    aprov.verificar_alta(conn, node_id, elegida.id)
    prefijo = asignar_prefijo(conn, node_id, elegida.id)
    secreto = NodeRepository(conn).register_node(node_id, branch_id=elegida.id, prefijo=prefijo)
    espejo = espejo_del_nodo(conn, aprov)
    conn.commit()
    return Alta(node_id, elegida, secreto, espejo, prefijo)


def dar_de_baja(conn, node_id: str, salida: Salida = print) -> int:
    """Revoca un nodo: su secreto deja de verificar. No borra nada (outbox e historico quedan)."""
    from libraedge.db.repository import NodeRepository

    fila = conn.execute("SELECT active FROM node_identity WHERE node_id = ?", (node_id,)).fetchone()
    if fila is None:
        salida(f"No hay ningun nodo {node_id!r}. Ver `estado` para la lista.")
        return 1
    if not fila[0]:
        salida(f"El nodo {node_id!r} ya estaba dado de baja.")
        return 0
    NodeRepository(conn).deactivate_node(node_id)
    conn.commit()
    salida(f"Nodo {node_id!r} dado de baja: su secreto deja de verificar.")
    return 0


# ── estado y vigilancia ─────────────────────────────────────────────────────

def minutos_desde(marca: str | None, ahora: datetime) -> float | None:
    """Minutos entre ``marca`` (ISO; sin zona = UTC) y ``ahora``; ``None`` si falta o no se lee."""
    if not marca:
        return None
    try:
        cuando = datetime.fromisoformat(marca)
    except ValueError:
        return None
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=UTC)
    return (ahora - cuando).total_seconds() / 60


def hace_cuanto(marca: str | None, ahora: datetime | None = None) -> str:
    if not marca:
        return "NUNCA (registrado pero nunca sincronizo)"
    minutos = minutos_desde(marca, ahora or datetime.now(UTC))
    if minutos is None:
        return f"fecha ilegible ({marca!r})"
    if minutos < 2:
        return "recien"
    if minutos < 120:
        return f"hace {minutos:.0f} min"
    return f"hace {minutos / 60:.1f} h"


def estado(conn, salida: Salida = print, ahora: datetime | None = None) -> int:
    """Que hay publicado y que nodos existen."""
    from libraedge.db.changelog import init_changelog_schema

    init_changelog_schema(conn)
    publicadas = conn.execute(
        "SELECT table_name, COUNT(*) FROM sync_changelog GROUP BY table_name ORDER BY table_name"
    ).fetchall()
    nodos = conn.execute(
        "SELECT node_id, branch_id, active, last_server_cursor, last_seen_at, prefijo"
        " FROM node_identity ORDER BY node_id"
    ).fetchall()
    conn.commit()

    salida("Tablas publicadas al changelog:")
    if not publicadas:
        salida("  (ninguna — falta correr `publicar`)")
    for fila in publicadas:
        salida(f"  {fila[0]}: {fila[1]} cambios")
    salida("")
    salida("Nodos registrados:")
    if not nodos:
        salida("  (ninguno — falta correr `registrar`)")
    for fila in nodos:
        activo = "activo" if fila[2] else "REVOCADO"
        salida(f"  {fila[0]} (sucursal {fila[1]}, prefijo {fila[5] or 'SIN PREFIJO'}) — {activo},"
               f" cursor {fila[3] or 0}, visto {hace_cuanto(fila[4], ahora)}")
    return 0


def sucursales(conn, aprov: Aprovisionamiento, salida: Salida = print) -> int:
    """Las sucursales: id, nombre, si esta activa y si ya tiene nodo; y los nodos huerfanos."""
    lista = listar_sucursales(conn, aprov)
    ids = {s.id for s in lista}
    huerfanos = [fila for fila in conn.execute(
        "SELECT node_id, branch_id FROM node_identity WHERE active = 1 ORDER BY node_id").fetchall()
        if fila[1] not in ids]
    salida("Sucursales de la instancia:")
    if not lista:
        salida("  (ninguna — crearla primero desde la pantalla de sucursales de la app)")
    for s in lista:
        marca = "activa" if s.activa else "INACTIVA"
        nodo = f"nodo {s.nodo}" if s.nodo else "sin nodo"
        salida(f"  {s.id:>4}  {s.nombre} — {marca}, {nodo}")
    if huerfanos:
        salida("")
        salida("Nodos activos cuya sucursal no es una sucursal de la instancia:")
        for node_id, branch_id in huerfanos:
            salida(f"  {node_id} (sucursal {branch_id!r})")
    return 0


def vigilar(conn, umbral_minutos: int, salida: Salida = print, ahora: datetime | None = None) -> int:
    """Avisa que nodos activos dejaron de hablar. **Sale con 1 si hay alguno.**

    🔴 El codigo de salida es el punto: un comando que siempre sale 0 es un
    informe que hay que acordarse de leer; con 1 lo usa el cron o un
    healthcheck. El umbral se pasa porque depende del intervalo de cada
    instalacion (el nodo cicla cada 60 s por defecto). Lee
    ``node_identity.last_seen_at``, que anota la autenticacion de push y pull.

    Un nodo callado NO son ventas perdidas: siguen en su outbox. Es que nadie
    sabe cuanto hay esperando ni desde cuando.
    """
    from libraedge.db.repository import NodeRepository

    ahora = ahora or datetime.now(UTC)
    nodos = NodeRepository(conn).nodos_para_vigilar(ahora=ahora)
    conn.commit()
    if not nodos:
        salida("No hay nodos registrados: no hay nada que vigilar.")
        return 0

    callados = 0
    vigilados = 0
    for nodo in nodos:
        rotulo = f"  {nodo['node_id']} (sucursal {nodo['branch_id']})"
        if not nodo["activo"]:
            salida(f"{rotulo} — REVOCADO, no se vigila")
            continue
        vigilados += 1
        minutos = nodo["minutos_sin_verse"]
        visto = hace_cuanto(nodo["ultima_vez"], ahora)
        if minutos is not None and minutos <= umbral_minutos:
            salida(f"{rotulo} — al dia, visto {visto}")
        else:
            callados += 1
            salida(f"{rotulo} — SIN NOTICIAS, visto {visto}")

    salida("")
    if callados:
        salida(f"{callados} nodo(s) sin dar senales en {umbral_minutos} minutos.")
        salida("Puede ser la conexion del local, la PC apagada o el servicio caido.")
        salida("Lo cobrado sin internet sigue en el nodo; el problema es que nadie")
        salida("sabe cuanto hay esperando ni desde cuando.")
        return 1
    # Se cuentan los VIGILADOS: contar los registrados daba un falso verde
    # cuando el unico nodo estaba revocado (demo de RestoLibra, 2026-08-31).
    if not vigilados:
        salida("No hay nodos activos: todos los registrados estan revocados.")
        return 0
    sujeto = "El nodo dio" if vigilados == 1 else f"Los {vigilados} nodos dieron"
    salida(f"{sujeto} senales dentro de los {umbral_minutos} minutos.")
    return 0


def es_interactivo() -> bool:
    """¿Hay una persona del otro lado? Con un pipe, un cron o el CI no se puede preguntar."""
    return sys.stdin.isatty()
