"""La parte del instalador del nodo que no es de Windows (ADR-010).

El instalador (``instalador/nodo.iss`` y sus scripts de PowerShell) es el mismo
para todos los productos: lo propio de cada uno es una **definicion**
(``instalador/productos/<producto>.json``) y lo propio de cada nodo es su
**carga** (``carga-nodo.json``, la escribe ``libraedge-central nodo registrar
--carga``). Este modulo lee y valida las dos y arma el entorno del nodo, que es
lo que se puede probar fuera de Windows.

🔴 **Solo biblioteca estandar, y sin importar nada de ``libraedge``.** El
instalador copia este archivo tal cual al lado de sus scripts y lo corre con el
Python embebido del producto (``python libraedge_instalador.py ...``): el
producto trae la version de LibraEdge que tenga pineada, que puede ser anterior
a este modulo, y el instalador no puede depender de eso.

Los secretos (el del nodo, la clave de PostgreSQL, ``SECRET_KEY``, la clave del
admin local) **no se imprimen nunca** ni van en el texto de un error: los
mensajes nombran la variable, no el valor. Y ninguno viaja por la linea de
comandos: el instalador pasa rutas de archivos, porque Inno Setup escribe la
linea de comandos en su log y ``Start-Transcript`` tambien.

Uso (lo invocan los scripts del instalador)::

    python libraedge_instalador.py plan --definicion producto.json
    python libraedge_instalador.py validar --definicion producto.json --carga carga-nodo.json
    python libraedge_instalador.py entorno --definicion ... --carga ... --clave-pg ARCHIVO \\
        --estado RUTA --data-dir RUTA --salida nodo.env
    python libraedge_instalador.py config-emisor --carga ... --config RUTA\\config.json
    python libraedge_instalador.py revisar-entorno --definicion ... --entorno nodo.env
    python libraedge_instalador.py ejecutar libracore-migrar upgrade --prefijo ventalibra
    python libraedge_instalador.py ejecutar --entorno /etc/libraedge-nodo/nodo.env --como libraedge \\
        libraedge-nodo sincronizar          (Linux, ADR-012)

En Linux (``instalador/linux/``, ADR-012) lo usa ``instalar.sh`` igual que ``instalar.ps1`` en Windows; lo unico
que agrega es que ``ejecutar`` cargue el ``nodo.env`` y baje al usuario del servicio **dentro** del proceso: asi
el entorno, que tiene los secretos, no pasa por ``env VAR=...`` (se veria en ``/proc/<pid>/cmdline``) ni por un
``source`` de bash (un ``$(...)`` en un valor se ejecutaria como root).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

FORMATO_DEFINICION = "libraedge-producto/1"
FORMATO_CARGA = "libraedge-carga/1"

#: El PostgreSQL embebido del nodo: solo 127.0.0.1 y no el 5432, que la PC puede tener ocupado.
PUERTO_POSTGRES = 55432

#: Lo que entrega el central al registrar el nodo (ADR-008) mas la URL del central. Es todo lo que puede traer
#: la carga: una clave de mas (``PATH``, ``SECRET_KEY``) se rechaza, porque la carga termina en el entorno
#: de dos servicios que corren como SYSTEM.
VARIABLES_DE_LA_CARGA = (
    "LIBRAEDGE_CENTRAL_URL",
    "LIBRAEDGE_NODE_ID",
    "LIBRAEDGE_NODE_SECRET",
    "LIBRAEDGE_PREFIJO",
    "LIBRAEDGE_TABLAS_ESPEJO",
)

#: Las claves del ``config.json`` del central que el nodo necesita para facturar e imprimir el ticket como el
#: central, y que el espejo no trae (no es una tabla). Lista cerrada: el ``config.json`` tambien guarda credenciales
#: de MercadoPago y SMTP (o sus restos de antes del almacen cifrado) y el estado del servicio, y nada de eso viaja.
#: El logo tampoco: ``logo_path`` es una ruta del VPS.
CLAVES_DEL_EMISOR = (
    "empresa_nombre",
    "empresa_cuit",
    "empresa_iva_condition",
    "empresa_direccion",
    "empresa_telefono",
    "empresa_email",
    "empresa_iibb",
    "empresa_inicio_actividades",
    "ticket_ancho_mm",
    "ticket_fuente_size",
    "ticket_mostrar_logo",
    "ticket_linea_corte",
    "ticket_pie",
)

#: Sin estas dos el nodo no sabe que comprobante emitir (A, B o C) ni a nombre de quien.
CLAVES_DEL_EMISOR_NECESARIAS = ("empresa_cuit", "empresa_iva_condition")

_PRODUCTO = re.compile(r"^[a-z][a-z0-9]{1,30}$")
_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_BASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_GUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
_ASGI = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_COMANDO = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PREFIJO_NUMERACION = re.compile(r"^[A-Z0-9]{1,8}$")
_IDENTIFICADOR_SQL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class ErrorDeInstalacion(ValueError):
    """La definicion o la carga no sirven. El mensaje dice que corregir y nunca incluye un secreto."""


# ── la definicion del producto ──────────────────────────────────────────────


@dataclass(frozen=True)
class Base:
    """Una base del PostgreSQL embebido y las variables del producto que apuntan a ella."""

    nombre: str
    variables: tuple[str, ...]
    nodo: bool = False


@dataclass(frozen=True)
class Definicion:
    """Lo que el instalador necesita saber de un producto (``instalador/productos/<producto>.json``)."""

    producto: str
    nombre: str
    app_id: str
    prefijo_variables: str
    bases: tuple[Base, ...]
    asgi: str
    puerto: int
    salud: str
    migraciones: tuple[tuple[str, ...], ...]
    base_auth: str | None = None
    extras: tuple[str, ...] = ()
    variables: Mapping[str, str] = field(default_factory=dict)
    version_esquema: str = "alembic"

    @property
    def base_del_nodo(self) -> Base:
        """La base donde viven el outbox y las tablas de LibraEdge (``LIBRAEDGE_DATABASE_URL``)."""
        return next(base for base in self.bases if base.nodo)

    @property
    def variables_de_admin(self) -> tuple[str, ...]:
        """Las dos formas que leen los productos (libraauth ``ensure_admin_user`` y ``ensure_default_admin``)."""
        return ("ADMIN_PASSWORD", f"{self.prefijo_variables}_ADMIN_PASSWORD")

    def plan(self) -> dict:
        """Lo que los scripts de PowerShell leen de la definicion, ya validado. No tiene secretos."""
        return {
            "producto": self.producto,
            "nombre": self.nombre,
            "prefijo_variables": self.prefijo_variables,
            "bases": [{"nombre": b.nombre, "variables": list(b.variables), "nodo": b.nodo} for b in self.bases],
            "base_del_nodo": self.base_del_nodo.nombre,
            "asgi": self.asgi,
            "puerto": self.puerto,
            "salud": self.salud,
            "migraciones": [list(m) for m in self.migraciones],
            "version_esquema": self.version_esquema,
        }


def _texto(datos: Mapping, clave: str, donde: str) -> str:
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor.strip():
        raise ErrorDeInstalacion(f"{donde}: falta {clave!r} (un texto no vacio).")
    return valor.strip()


def _bases(crudas, donde: str) -> tuple[Base, ...]:
    if not isinstance(crudas, list) or not crudas:
        raise ErrorDeInstalacion(f"{donde}: 'bases' tiene que ser una lista con al menos una base.")
    bases, vistas_nombres, vistas_variables = [], set(), set()
    for cruda in crudas:
        if not isinstance(cruda, Mapping):
            raise ErrorDeInstalacion(f"{donde}: cada base es un objeto con 'nombre' y 'variables'.")
        nombre = _texto(cruda, "nombre", donde)
        if not _BASE.match(nombre) or nombre in vistas_nombres:
            raise ErrorDeInstalacion(f"{donde}: nombre de base no valido o repetido: {nombre!r}.")
        variables = cruda.get("variables")
        if not isinstance(variables, list) or not variables:
            raise ErrorDeInstalacion(f"{donde}: la base {nombre!r} no dice que variables apuntan a ella.")
        for variable in variables:
            if not isinstance(variable, str) or not _VARIABLE.match(variable):
                raise ErrorDeInstalacion(f"{donde}: variable no valida en la base {nombre!r}: {variable!r}.")
            if variable in vistas_variables or variable == "LIBRAEDGE_DATABASE_URL":
                raise ErrorDeInstalacion(
                    f"{donde}: {variable} repetida (LIBRAEDGE_DATABASE_URL la pone el instalador: es la base"
                    " con 'nodo': true).")
            vistas_variables.add(variable)
        vistas_nombres.add(nombre)
        bases.append(Base(nombre, tuple(variables), bool(cruda.get("nodo", False))))
    marcadas = [b for b in bases if b.nodo]
    if len(bases) == 1 and not marcadas:
        bases[0] = Base(bases[0].nombre, bases[0].variables, True)
    elif len(marcadas) != 1:
        raise ErrorDeInstalacion(
            f"{donde}: con varias bases, exactamente una lleva 'nodo': true (la del outbox de LibraEdge).")
    return tuple(bases)


def _migraciones(crudas, donde: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(crudas, list) or not crudas:
        raise ErrorDeInstalacion(f"{donde}: 'migraciones' es la lista de comandos, en orden (la del central).")
    comandos = []
    for cruda in crudas:
        if (not isinstance(cruda, list) or not cruda or not all(isinstance(p, str) and p for p in cruda)
                or not _COMANDO.match(cruda[0])):
            raise ErrorDeInstalacion(f"{donde}: comando de migracion no valido: {cruda!r}.")
        comandos.append(tuple(cruda))
    return tuple(comandos)


_CLAVES_DE_LA_DEFINICION = frozenset({
    "formato", "producto", "nombre", "app_id", "prefijo_variables", "bases", "asgi", "puerto", "salud",
    "migraciones", "base_auth", "extras", "variables", "version_esquema",
})


def definicion_desde_dict(datos: Mapping, donde: str = "definicion") -> Definicion:
    """Valida y arma una :class:`Definicion`. Todo error es :class:`ErrorDeInstalacion`."""
    if not isinstance(datos, Mapping):
        raise ErrorDeInstalacion(f"{donde}: tiene que ser un objeto JSON.")
    if datos.get("formato") != FORMATO_DEFINICION:
        raise ErrorDeInstalacion(f"{donde}: 'formato' tiene que ser {FORMATO_DEFINICION!r}.")
    # Una clave mal escrita ('migracion', 'puertos') se ignoraria en silencio: se rechaza. Las que empiezan con
    # '_' son comentarios (JSON no tiene).
    desconocidas = sorted(k for k in datos if k not in _CLAVES_DE_LA_DEFINICION and not str(k).startswith("_"))
    if desconocidas:
        raise ErrorDeInstalacion(f"{donde}: claves desconocidas: {', '.join(desconocidas)}.")
    producto = _texto(datos, "producto", donde)
    if not _PRODUCTO.match(producto):
        raise ErrorDeInstalacion(f"{donde}: 'producto' va en minusculas y sin espacios: {producto!r}.")
    app_id = _texto(datos, "app_id", donde).upper()
    if not _GUID.match(app_id):
        raise ErrorDeInstalacion(f"{donde}: 'app_id' tiene que ser un GUID sin llaves.")
    prefijo = _texto(datos, "prefijo_variables", donde)
    if prefijo != producto.upper():
        raise ErrorDeInstalacion(
            f"{donde}: 'prefijo_variables' es {producto.upper()!r} (el que leen libracore y libraedge).")
    asgi = _texto(datos, "asgi", donde)
    if not _ASGI.match(asgi):
        raise ErrorDeInstalacion(f"{donde}: 'asgi' es 'modulo:objeto' (p. ej. 'app.asgi:app').")
    puerto = datos.get("puerto")
    if not isinstance(puerto, int) or isinstance(puerto, bool) or not 1024 <= puerto <= 65535 \
            or puerto == PUERTO_POSTGRES:
        raise ErrorDeInstalacion(f"{donde}: 'puerto' entre 1024 y 65535, y no el {PUERTO_POSTGRES} del PostgreSQL.")
    salud = _texto(datos, "salud", donde)
    if not salud.startswith("/"):
        raise ErrorDeInstalacion(f"{donde}: 'salud' es una ruta que empieza con '/'.")
    migraciones = _migraciones(datos.get("migraciones"), donde)
    base_auth = datos.get("base_auth")
    if base_auth is not None:
        if base_auth not in ("core", "dominio"):
            raise ErrorDeInstalacion(f"{donde}: 'base_auth' es 'core' o 'dominio' (el --base de libraauth-migrar).")
        de_auth = [m for m in migraciones if m[0] == "libraauth-migrar"]
        if not de_auth or any(_valor_de(m, "--base") != base_auth for m in de_auth):
            raise ErrorDeInstalacion(
                f"{donde}: 'base_auth' dice {base_auth!r} y las migraciones no corren libraauth-migrar"
                f" --base {base_auth}.")
    extras = datos.get("extras", [])
    if not isinstance(extras, list) or not all(isinstance(e, str) and e.strip() for e in extras):
        raise ErrorDeInstalacion(f"{donde}: 'extras' es una lista de requisitos de pip (p. ej. 'tzdata').")
    variables = datos.get("variables", {})
    if not isinstance(variables, Mapping) or not all(
            isinstance(k, str) and _VARIABLE.match(k) and isinstance(v, str) for k, v in variables.items()):
        raise ErrorDeInstalacion(f"{donde}: 'variables' es un objeto VARIABLE -> texto.")
    version_esquema = datos.get("version_esquema", "alembic")
    if not isinstance(version_esquema, str) or not version_esquema.strip():
        raise ErrorDeInstalacion(f"{donde}: 'version_esquema' es 'alembic', 'ninguna' o 'modulo:funcion'.")
    definicion = Definicion(
        producto=producto, nombre=_texto(datos, "nombre", donde), app_id=app_id, prefijo_variables=prefijo,
        bases=_bases(datos.get("bases"), donde), asgi=asgi, puerto=puerto, salud=salud, migraciones=migraciones,
        base_auth=base_auth, extras=tuple(e.strip() for e in extras), variables=dict(variables),
        version_esquema=version_esquema.strip())
    pisadas = set(definicion.variables) & set(_variables_generadas(definicion))
    if pisadas:
        raise ErrorDeInstalacion(f"{donde}: 'variables' no puede fijar {', '.join(sorted(pisadas))}: las pone el"
                                 " instalador.")
    return definicion


def _valor_de(comando: tuple[str, ...], opcion: str) -> str | None:
    for i, parte in enumerate(comando[:-1]):
        if parte == opcion:
            return comando[i + 1]
    return None


def _leer_json(ruta: str, que: str):
    try:
        with open(ruta, encoding="utf-8-sig") as archivo:
            return json.load(archivo)
    except FileNotFoundError:
        raise ErrorDeInstalacion(f"No esta {que}: {ruta}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ErrorDeInstalacion(f"{que} ({ruta}) no es un JSON legible: {type(exc).__name__}.") from None


def leer_definicion(ruta: str) -> Definicion:
    return definicion_desde_dict(_leer_json(ruta, "la definicion del producto"), donde=os.path.basename(ruta))


# ── la carga del nodo ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Carga:
    """Lo que el central emitio para ESTE nodo. ``entorno`` tiene el secreto: no imprimirlo."""

    producto: str
    entorno: Mapping[str, str]
    emisor: Mapping[str, object]
    sucursal: Mapping[str, str]
    avisos: tuple[str, ...] = ()

    def resumen(self) -> dict:
        """Lo que se puede mostrar: todo menos el secreto."""
        return {
            "producto": self.producto,
            "node_id": self.entorno["LIBRAEDGE_NODE_ID"],
            "prefijo": self.entorno["LIBRAEDGE_PREFIJO"],
            "central": self.entorno["LIBRAEDGE_CENTRAL_URL"],
            "tablas_espejo": len(parsear_tablas_espejo(self.entorno["LIBRAEDGE_TABLAS_ESPEJO"])),
            "sucursal": dict(self.sucursal),
            "emisor": sorted(self.emisor),
            "avisos": list(self.avisos),
        }


def _sin_saltos(nombre: str, valor: str) -> None:
    # Un salto de linea en un valor es una variable de mas en el archivo de entorno.
    if any(c in valor for c in "\r\n\0"):
        raise ErrorDeInstalacion(f"{nombre} tiene un salto de linea o un caracter nulo.")


def normalizar_url_del_central(url: str) -> str:
    """``https://cliente.ventalibra.com.ar/`` -> sin la barra final. Solo HTTPS (HTTP solo contra la misma PC)."""
    url = (url or "").strip()
    _sin_saltos("LIBRAEDGE_CENTRAL_URL", url)
    partes = urlsplit(url)
    local = partes.hostname in ("localhost", "127.0.0.1")
    if partes.scheme != "https" and not (partes.scheme == "http" and local):
        raise ErrorDeInstalacion(
            "LIBRAEDGE_CENTRAL_URL tiene que ser https:// (el secreto del nodo viaja en cada pedido).")
    if not partes.hostname or partes.username or partes.password or partes.query or partes.fragment:
        raise ErrorDeInstalacion("LIBRAEDGE_CENTRAL_URL es la base del central: https://host[:puerto][/ruta].")
    return url.rstrip("/")


def parsear_tablas_espejo(crudo: str) -> dict[str, tuple[str, ...]]:
    """``"a:id,party_roles:party_id+role"`` -> ``{"a": ("id",), "party_roles": ("party_id", "role")}``.

    El mismo formato que lee ``libraedge-nodo`` (``cli.tablas_espejo``), con nombres SQL validos."""
    tablas: dict[str, tuple[str, ...]] = {}
    for parte in (crudo or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        nombre, _, pk = parte.partition(":")
        columnas = tuple(c.strip() for c in (pk.strip() or "id").split("+"))
        nombre = nombre.strip()
        if not _IDENTIFICADOR_SQL.match(nombre) or not all(_IDENTIFICADOR_SQL.match(c) for c in columnas):
            raise ErrorDeInstalacion(f"LIBRAEDGE_TABLAS_ESPEJO tiene una entrada no valida: {parte!r}.")
        tablas[nombre] = columnas
    if not tablas:
        raise ErrorDeInstalacion("LIBRAEDGE_TABLAS_ESPEJO esta vacia: es el reparto de autoridad, no es opcional.")
    return tablas


def _cuit_valido(cuit: str) -> bool:
    digitos = re.sub(r"[-\s]", "", cuit)
    if not re.fullmatch(r"\d{11}", digitos):
        return False
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    resto = sum(int(d) * p for d, p in zip(digitos[:10], pesos)) % 11
    verificador = 0 if resto == 0 else 11 - resto
    return verificador != 10 and verificador == int(digitos[10])


def _emisor(crudo, avisos: list[str]) -> dict[str, object]:
    if crudo is None:
        avisos.append("La carga no trae los datos del emisor: el nodo no va a poder facturar hasta que se"
                      " carguen (config.json).")
        return {}
    if not isinstance(crudo, Mapping):
        raise ErrorDeInstalacion("'emisor' tiene que ser un objeto con las claves del config.json.")
    emisor: dict[str, object] = {}
    for clave, valor in crudo.items():
        if clave not in CLAVES_DEL_EMISOR:
            # Un central mas nuevo puede mandar claves que este instalador no conoce: no se escriben.
            avisos.append(f"Se ignora la clave {clave!r} del emisor (no es una de las que se copian).")
            continue
        if not isinstance(valor, str | bool | int | float):
            raise ErrorDeInstalacion(f"El emisor tiene un valor no valido en {clave!r}.")
        emisor[clave] = valor
    cuit = emisor.get("empresa_cuit")
    if isinstance(cuit, str) and cuit.strip() and not _cuit_valido(cuit):
        raise ErrorDeInstalacion("empresa_cuit del emisor no es un CUIT valido (digito verificador).")
    faltan = [c for c in CLAVES_DEL_EMISOR_NECESARIAS if not str(emisor.get(c, "")).strip()]
    if faltan:
        avisos.append(f"Al emisor le falta {', '.join(faltan)}: el nodo no va a poder facturar.")
    return emisor


def validar_emisor(crudo) -> tuple[dict[str, object], list[str]]:
    """Los datos del emisor que se copian y los avisos (claves ignoradas, faltantes). Un CUIT invalido es error."""
    avisos: list[str] = []
    return _emisor(crudo, avisos), avisos


def carga_desde_dict(datos: Mapping, producto: str) -> Carga:
    """Valida la carga contra el producto que se esta instalando. Los errores nunca muestran el secreto."""
    if not isinstance(datos, Mapping):
        raise ErrorDeInstalacion("La carga tiene que ser un objeto JSON.")
    if datos.get("formato") != FORMATO_CARGA:
        raise ErrorDeInstalacion(f"La carga no es una carga de nodo ('formato' tiene que ser {FORMATO_CARGA!r}).")
    de = datos.get("producto")
    if de != producto:
        raise ErrorDeInstalacion(
            f"La carga es de {de!r} y este instalador es de {producto!r}: registrar el nodo en el central del"
            " producto que se instala.")
    crudo = datos.get("entorno")
    if not isinstance(crudo, Mapping):
        raise ErrorDeInstalacion("La carga no trae 'entorno'.")
    sobran = sorted(set(crudo) - set(VARIABLES_DE_LA_CARGA))
    if sobran:
        raise ErrorDeInstalacion(f"La carga trae variables que no le corresponden: {', '.join(sobran)}.")
    entorno: dict[str, str] = {}
    for nombre in VARIABLES_DE_LA_CARGA:
        valor = crudo.get(nombre)
        if not isinstance(valor, str) or not valor.strip():
            raise ErrorDeInstalacion(f"A la carga le falta {nombre}.")
        valor = valor.strip()
        _sin_saltos(nombre, valor)
        entorno[nombre] = valor
    entorno["LIBRAEDGE_CENTRAL_URL"] = normalizar_url_del_central(entorno["LIBRAEDGE_CENTRAL_URL"])
    node_id = entorno["LIBRAEDGE_NODE_ID"]
    if any(c.isspace() for c in node_id) or len(node_id) > 64:
        raise ErrorDeInstalacion("LIBRAEDGE_NODE_ID no es valido (sin espacios, hasta 64 caracteres).")
    if not _PREFIJO_NUMERACION.match(entorno["LIBRAEDGE_PREFIJO"]):
        raise ErrorDeInstalacion("LIBRAEDGE_PREFIJO son de 1 a 8 mayusculas o digitos (lo asigna el central).")
    secreto = entorno["LIBRAEDGE_NODE_SECRET"]
    if len(secreto) < 20 or any(c.isspace() for c in secreto):
        raise ErrorDeInstalacion("LIBRAEDGE_NODE_SECRET no parece el que emite el central (corto o con espacios).")
    parsear_tablas_espejo(entorno["LIBRAEDGE_TABLAS_ESPEJO"])
    avisos: list[str] = []
    emisor = _emisor(datos.get("emisor"), avisos)
    sucursal = datos.get("sucursal") or {}
    if not isinstance(sucursal, Mapping):
        raise ErrorDeInstalacion("'sucursal' es un objeto {id, nombre}.")
    return Carga(producto, entorno, emisor, {k: str(v) for k, v in sucursal.items()}, tuple(avisos))


def leer_carga(ruta: str, producto: str) -> Carga:
    return carga_desde_dict(_leer_json(ruta, "la carga del nodo"), producto)


# ── el entorno del nodo ─────────────────────────────────────────────────────


def _variables_generadas(definicion: Definicion) -> tuple[str, ...]:
    variables = [*VARIABLES_DE_LA_CARGA, "LIBRAEDGE_DATABASE_URL", "LIBRAEDGE_ESTADO", "LIBRAEDGE_VERSION_ESQUEMA"]
    for base in definicion.bases:
        variables.extend(base.variables)
    variables.extend(["DATA_DIR", "SECRET_KEY", *definicion.variables_de_admin, "TZ"])
    return tuple(dict.fromkeys(variables))


def url_de_base(clave_pg: str, base: str, puerto: int = PUERTO_POSTGRES) -> str:
    # La clave va codificada: con un '@' o un '/' adentro la URL se parte en otro lugar y nadie avisa.
    return f"postgresql://postgres:{quote(clave_pg, safe='')}@127.0.0.1:{puerto}/{base}"


def armar_entorno(definicion: Definicion, carga: Carga, *, clave_pg: str, estado: str, data_dir: str,
                  previo: Mapping[str, str] | None = None,
                  generar: Callable[[], str] = lambda: secrets.token_urlsafe(48)) -> dict[str, str]:
    """El ``nodo.env`` completo, en orden. Lo que se genera aca (``SECRET_KEY``, la clave del admin local) se
    conserva del ``previo`` en una reinstalacion: un ``SECRET_KEY`` nuevo deja ilegible lo que libraauth cifro con
    el anterior y cierra todas las sesiones."""
    if carga.producto != definicion.producto:
        raise ErrorDeInstalacion("La carga no es de este producto.")
    if not clave_pg or any(c in clave_pg for c in "\r\n\0"):
        raise ErrorDeInstalacion("La clave de PostgreSQL esta vacia o tiene saltos de linea.")
    for nombre, valor in (("LIBRAEDGE_ESTADO", estado), ("DATA_DIR", data_dir)):
        if not valor:
            raise ErrorDeInstalacion(f"Falta la ruta de {nombre}.")
        _sin_saltos(nombre, valor)
    previo = previo or {}

    def conservado(nombre: str) -> str:
        valor = (previo.get(nombre) or "").strip()
        return valor or generar()

    entorno: dict[str, str] = {nombre: carga.entorno[nombre] for nombre in VARIABLES_DE_LA_CARGA}
    entorno["LIBRAEDGE_DATABASE_URL"] = url_de_base(clave_pg, definicion.base_del_nodo.nombre)
    entorno["LIBRAEDGE_ESTADO"] = estado
    entorno["LIBRAEDGE_VERSION_ESQUEMA"] = definicion.version_esquema
    for base in definicion.bases:
        for variable in base.variables:
            entorno[variable] = url_de_base(clave_pg, base.nombre)
    entorno["DATA_DIR"] = data_dir
    entorno["SECRET_KEY"] = conservado("SECRET_KEY")
    # El admin que crea el producto al arrancar con la tabla de usuarios vacia. Sin clave, unos productos no
    # arrancan (ensure_default_admin) y otros la inventan y la escriben en el log (ensure_admin_user). Nadie la
    # necesita: los usuarios bajan del central por el espejo.
    clave_admin = next((previo[v].strip() for v in definicion.variables_de_admin if (previo.get(v) or "").strip()),
                       None) or generar()
    for variable in definicion.variables_de_admin:
        entorno[variable] = clave_admin
    entorno["TZ"] = "America/Argentina/Buenos_Aires"
    for nombre, valor in definicion.variables.items():
        _sin_saltos(nombre, valor)
        entorno[nombre] = valor
    return entorno


def formatear_entorno(entorno: Mapping[str, str]) -> str:
    lineas = []
    for nombre, valor in entorno.items():
        if not _VARIABLE.match(nombre):
            raise ErrorDeInstalacion(f"Nombre de variable no valido: {nombre!r}.")
        _sin_saltos(nombre, valor)
        lineas.append(f"{nombre}={valor}\n")
    return "".join(lineas)


def leer_entorno(texto: str) -> dict[str, str]:
    """El inverso de :func:`formatear_entorno`. Tolera BOM, CRLF, comentarios y lineas en blanco."""
    entorno: dict[str, str] = {}
    for linea in texto.lstrip("﻿").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        nombre, _, valor = linea.partition("=")
        entorno[nombre.strip()] = valor
    return entorno


def revisar_entorno(definicion: Definicion, entorno: Mapping[str, str]) -> list[str]:
    """Las variables que faltan (o estan vacias) en el ``nodo.env`` de un nodo instalado. Solo nombres."""
    return [nombre for nombre in _variables_generadas(definicion) if not (entorno.get(nombre) or "").strip()]


def escribir_en_archivo_protegido(ruta: str, contenido: str) -> None:
    """Escribe sobre un archivo que YA existe, sin BOM.

    🔴 Tiene que existir: lo crea antes el script de PowerShell con la ACL cortada (SYSTEM y Administradores).
    Crearlo aca lo dejaria con los permisos heredados de la carpeta, legible por cualquier usuario de la PC, y
    abrir un archivo existente para escribir conserva su ACL."""
    if not os.path.isfile(ruta):
        raise ErrorDeInstalacion(f"{ruta} no existe: lo crea el instalador con la ACL restringida antes de escribir.")
    with open(ruta, "r+", encoding="utf-8", newline="\n") as archivo:
        archivo.seek(0)
        archivo.write(contenido)
        archivo.truncate()


# ── el config.json del emisor ───────────────────────────────────────────────


def fusionar_config(ruta: str, emisor: Mapping[str, object]) -> list[str]:
    """Copia los datos del emisor al ``config.json`` del nodo sin tocar el resto. Devuelve las claves cambiadas.

    Un ``config.json`` que existe y no se puede leer se deja como esta: pisarlo borraria lo que el producto
    guardo ahi (formato del ticket, estado del servicio)."""
    actual: dict = {}
    if os.path.exists(ruta):
        datos = _leer_json(ruta, "el config.json del nodo")
        if not isinstance(datos, dict):
            raise ErrorDeInstalacion(f"{ruta} no es un objeto JSON: se deja como esta.")
        actual = datos
    cambiadas = [clave for clave, valor in emisor.items() if actual.get(clave) != valor]
    if not cambiadas and os.path.exists(ruta):
        return []
    actual.update(emisor)
    carpeta = os.path.dirname(os.path.abspath(ruta))
    os.makedirs(carpeta, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=carpeta)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
            json.dump(actual, archivo, ensure_ascii=False, indent=2)
        os.replace(temporal, ruta)
    except BaseException:
        if os.path.exists(temporal):
            os.unlink(temporal)
        raise
    return cambiadas


# ── del lado del central: escribir la carga ─────────────────────────────────


def emisor_desde_config(ruta: str) -> dict[str, object] | None:
    """Las :data:`CLAVES_DEL_EMISOR` del ``config.json`` del central, o ``None`` si no hay archivo."""
    if not os.path.exists(ruta):
        return None
    datos = _leer_json(ruta, "el config.json del central")
    if not isinstance(datos, dict):
        raise ErrorDeInstalacion(f"{ruta} no es un objeto JSON.")
    return {clave: datos[clave] for clave in CLAVES_DEL_EMISOR if clave in datos}


def ruta_config_del_central(entorno: Mapping[str, str] | None = None) -> str:
    """Donde lo lee libracore (``config_manager``): ``$DATA_DIR/config.json``, o el directorio actual."""
    entorno = os.environ if entorno is None else entorno
    return os.path.join(entorno.get("DATA_DIR") or os.getcwd(), "config.json")


def armar_carga(producto: str, entorno_del_alta: Mapping[str, str], central_url: str,
                emisor: Mapping[str, object] | None, sucursal: Mapping[str, str] | None = None) -> dict:
    """La carga que lee el instalador, validada con las mismas reglas con que la va a leer."""
    entorno = dict(entorno_del_alta)
    entorno["LIBRAEDGE_CENTRAL_URL"] = central_url
    carga = {
        "formato": FORMATO_CARGA,
        "producto": producto,
        "entorno": {nombre: entorno.get(nombre, "") for nombre in VARIABLES_DE_LA_CARGA},
        "sucursal": dict(sucursal or {}),
    }
    if emisor is not None:
        carga["emisor"] = dict(emisor)
    validada = carga_desde_dict(carga, producto)
    carga["entorno"] = dict(validada.entorno)
    return carga


def escribir_carga(ruta: str, carga: Mapping) -> None:
    """Crea el archivo con permisos 600 y **no pisa** uno que ya existe (podria ser la carga de otro nodo)."""
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
        json.dump(carga, archivo, ensure_ascii=False, indent=2)
        archivo.write("\n")


# ── correr un console script sin su .exe ────────────────────────────────────


def cargar_entorno(ruta: str, destino: dict | None = None) -> int:
    """Pone las variables del ``nodo.env`` en ``destino`` (``os.environ`` por omision). Devuelve cuantas.

    Lo mismo que ``Cargar-EntornoEnProceso`` de ``comun.ps1``. No imprime valores."""
    if not os.path.isfile(ruta):
        raise ErrorDeInstalacion(f"No esta el archivo de entorno: {ruta}")
    variables = leer_entorno(_leer_texto(ruta))
    malos = [n for n in variables if not _VARIABLE.match(n)]
    if malos:
        raise ErrorDeInstalacion(f"{ruta} tiene nombres de variable no validos: {', '.join(malos)}.")
    (os.environ if destino is None else destino).update(variables)
    return len(variables)


def bajar_a_usuario(usuario: str) -> None:
    """Pasa este proceso al usuario del servicio (Linux): grupos, grupo, usuario y ``HOME``, en ese orden.

    Las migraciones y la primera sincronizacion corren como el usuario de los servicios y no como root: lo que
    escriban (``estado.json``, archivos de ``DATA_DIR``) queda de quien lo va a seguir escribiendo."""
    if not hasattr(os, "setuid"):
        raise ErrorDeInstalacion("--como solo existe en Linux: en Windows los servicios corren como SYSTEM.")
    import pwd

    try:
        entrada = pwd.getpwnam(usuario)
    except KeyError:
        raise ErrorDeInstalacion(f"No existe el usuario {usuario!r}.") from None
    if os.geteuid() != entrada.pw_uid:
        os.initgroups(usuario, entrada.pw_gid)
        os.setgid(entrada.pw_gid)
        os.setuid(entrada.pw_uid)
    os.environ["HOME"] = entrada.pw_dir
    os.environ["USER"] = os.environ["LOGNAME"] = usuario


def ejecutar(nombre: str, argumentos: list[str]) -> int:
    """Corre el console script ``nombre`` (``libracore-migrar``, ``alembic``...) en este mismo Python.

    El paquete del nodo se arma desde Linux para Windows, y asi no se generan los ``.exe`` de ``Scripts``: el
    instalador no depende de ellos. Se busca el entry point del grupo ``console_scripts`` y se lo llama con
    ``sys.argv`` armado como si fuera el ejecutable."""
    from importlib.metadata import entry_points

    encontrados = list(entry_points(group="console_scripts", name=nombre))
    if not encontrados:
        raise ErrorDeInstalacion(f"No esta el comando {nombre!r} en el Python del nodo: el paquete se armo sin el.")
    funcion = encontrados[0].load()
    sys.argv = [nombre, *argumentos]
    try:
        resultado = funcion()
    except SystemExit as salida:
        resultado = salida.code
    if resultado is None:
        return 0
    if isinstance(resultado, int):
        return resultado
    print(resultado, file=sys.stderr)
    return 1


# ── la linea de comandos ────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libraedge_instalador", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    plan = sub.add_parser("plan", help="la definicion validada, en JSON (sin secretos)")
    plan.add_argument("--definicion", required=True)
    validar = sub.add_parser("validar", help="valida la carga contra el producto; imprime un resumen sin secretos")
    validar.add_argument("--definicion", required=True)
    validar.add_argument("--carga", required=True)
    entorno = sub.add_parser("entorno", help="escribe el nodo.env (que ya tiene que existir, con su ACL)")
    entorno.add_argument("--definicion", required=True)
    entorno.add_argument("--carga", required=True)
    entorno.add_argument("--clave-pg", required=True, help="ARCHIVO con la clave del PostgreSQL embebido")
    entorno.add_argument("--estado", required=True, help="ruta del estado.json del proceso del nodo")
    entorno.add_argument("--data-dir", required=True, help="DATA_DIR del producto (config.json, logos...)")
    entorno.add_argument("--salida", required=True, help="el nodo.env")
    config = sub.add_parser("config-emisor", help="copia los datos del emisor de la carga al config.json")
    config.add_argument("--definicion", required=True)
    config.add_argument("--carga", required=True)
    config.add_argument("--config", required=True)
    revisar = sub.add_parser("revisar-entorno", help="lista las variables que faltan en un nodo.env (sin valores)")
    revisar.add_argument("--definicion", required=True)
    revisar.add_argument("--entorno", required=True)
    correr = sub.add_parser("ejecutar", help="corre un console script del Python del nodo")
    correr.add_argument("--entorno", help="nodo.env que se carga en el proceso antes de correr (Linux)")
    correr.add_argument("--como", help="usuario al que se baja despues de leer --entorno (Linux, root)")
    correr.add_argument("nombre")
    correr.add_argument("argumentos", nargs=argparse.REMAINDER)
    return parser


def _leer_texto(ruta: str) -> str:
    try:
        with open(ruta, encoding="utf-8-sig") as archivo:
            return archivo.read()
    except FileNotFoundError:
        return ""


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.comando == "ejecutar":
            # El entorno se lee ANTES de bajar de usuario: el nodo.env es de root con permisos 600.
            if args.entorno:
                cargar_entorno(args.entorno)
            if args.como:
                bajar_a_usuario(args.como)
            argumentos = args.argumentos[1:] if args.argumentos[:1] == ["--"] else args.argumentos
            return ejecutar(args.nombre, argumentos)
        definicion = leer_definicion(args.definicion)
        if args.comando == "plan":
            print(json.dumps(definicion.plan(), ensure_ascii=False))
            return 0
        if args.comando == "revisar-entorno":
            faltan = revisar_entorno(definicion, leer_entorno(_leer_texto(args.entorno)))
            for nombre in faltan:
                print(f"FALTA {nombre}")
            return 1 if faltan else 0
        carga = leer_carga(args.carga, definicion.producto)
        if args.comando == "validar":
            print(json.dumps(carga.resumen(), ensure_ascii=False))
            return 0
        if args.comando == "config-emisor":
            if not carga.emisor:
                print("La carga no trae datos del emisor: el config.json no se toca.")
                return 0
            cambiadas = fusionar_config(args.config, carga.emisor)
            print(f"config.json: {len(cambiadas)} claves del emisor actualizadas"
                  + (f" ({', '.join(cambiadas)})." if cambiadas else "."))
            return 0
        # entorno
        clave_pg = _leer_texto(args.clave_pg).strip()
        if not clave_pg:
            raise ErrorDeInstalacion(f"El archivo de la clave de PostgreSQL esta vacio o no existe: {args.clave_pg}")
        previo = leer_entorno(_leer_texto(args.salida))
        entorno = armar_entorno(definicion, carga, clave_pg=clave_pg, estado=args.estado, data_dir=args.data_dir,
                                previo=previo)
        escribir_en_archivo_protegido(args.salida, formatear_entorno(entorno))
        print(f"nodo.env: {len(entorno)} variables ({', '.join(entorno)}).")
        for aviso in carga.avisos:
            print(f"AVISO: {aviso}")
        return 0
    except ErrorDeInstalacion as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
