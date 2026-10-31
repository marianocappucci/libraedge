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
    python libraedge_instalador.py canjear --definicion producto.json --direccion sistema.contalibra.com.ar \\
        --salida carga-nodo.json        (el codigo, de LIBRAEDGE_CODIGO o de --archivo-codigo; ADR-014)
    python libraedge_instalador.py entorno --definicion ... --carga ... --clave-pg ARCHIVO \\
        --estado RUTA --data-dir RUTA --salida nodo.env
    python libraedge_instalador.py entorno ... --previo /var/lib/libraedge-nodo/conservado.env   (Linux)
    python libraedge_instalador.py config-emisor --carga ... --config RUTA\\config.json
    python libraedge_instalador.py revisar-entorno --definicion ... --entorno nodo.env
    python libraedge_instalador.py completar-entorno --entorno nodo.env [--definicion producto.json]
        (sin carga: variables fijas, ADR-016, y las `variables` del producto, ADR-023)
    python libraedge_instalador.py ejecutar libracore-migrar upgrade --prefijo ventalibra
    python libraedge_instalador.py ejecutar --entorno /etc/libraedge-nodo/nodo.env --como libraedge \\
        libraedge-nodo sincronizar          (Linux, ADR-012)
    python libraedge_instalador.py acceso --definicion producto.json     (ADR-013: como se entra, con la IP)
    python libraedge_instalador.py acceso --definicion producto.json --json
    python libraedge_instalador.py favoritos --actual "['a.desktop']" --agregar libraedge-nodo-x.desktop  (dock.sh)
    python libraedge_instalador.py descubrimiento --definicion producto.json --salida descubrimiento.json \\
        [--carga carga-nodo.json] [--entorno nodo.env] [--config config.json]    (ADR-017: lo que contesta el nodo)

En Linux (``instalador/linux/``, ADR-012) lo usa ``instalar.sh`` igual que ``instalar.ps1`` en Windows; lo unico
que agrega es que ``ejecutar`` cargue el ``nodo.env`` y baje al usuario del servicio **dentro** del proceso: asi
el entorno, que tiene los secretos, no pasa por ``env VAR=...`` (se veria en ``/proc/<pid>/cmdline``) ni por un
``source`` de bash (un ``$(...)`` en un valor se ejecutaria como root).
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

FORMATO_DEFINICION = "libraedge-producto/1"
#: La ruta publica del central donde se canjea el codigo de vinculacion (``pantalla_del_central.RUTA_DE_CANJE``).
RUTA_DE_CANJE = "/sync/v1/vinculacion"
#: De donde sale el codigo si no viene en un archivo: asi no pasa por ninguna linea de comandos.
VARIABLE_CODIGO = "LIBRAEDGE_CODIGO"
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

#: Lo opcional que puede traer la carga: las tablas del espejo que el nodo guarda contra escrituras del producto (v0.17,
#: ADR-019) y las tablas donde da de alta filas con un id provisorio, con lo que las referencia sin FK (v0.18, ADR-021).
#: Solo viajan si el producto las declara; sin ellas el nodo no instala ninguna guarda ni reapunta nada.
VARIABLES_OPCIONALES_DE_LA_CARGA = (
    "LIBRAEDGE_TABLAS_SOLO_LECTURA",
    "LIBRAEDGE_ALTAS_EN_NODO",
)

#: Lo que el nodo fija en el entorno del producto, igual en todos los nodos y en Windows y Linux (ADR-016). No son
#: secretos ni vienen de la carga:
#:
#: - ``LIBRAAUTH_COOKIE_SECURE=0``: el nodo sirve el producto por **http** en la LAN (``http://localhost:8000`` en la
#:   PC y ``http://<IP>:8000`` desde los otros puestos). Con la cookie de sesion ``Secure`` (el default de libraauth,
#:   ADR-026 de libraauth) el login da 200 y el pedido siguiente 401: WebKitGTK (la app de escritorio en Linux) no
#:   guarda una cookie ``Secure`` que llega por http ni desde ``localhost``, y ningun navegador la guarda desde una IP.
#:   Un producto con un libraauth anterior a v0.53.0 la ignora y queda como antes.
VARIABLES_FIJAS_DEL_NODO: Mapping[str, str] = {
    "LIBRAAUTH_COOKIE_SECURE": "0",
}

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

#: La MARCA BLANCA que viaja en la carga (ADR-030): las claves del ``config.json`` donde ``libracore`` guarda la
#: marca y los Terminos propios (``marca_router.CLAVE_CONFIG``, ``terminos_propios.CLAVE_CONFIG`` y
#: ``CLAVE_HISTORIAL``), mas los dos archivos que acompanian (``marca/logo.png`` y ``marca/terminos.md``, en
#: ``DATA_DIR``). Sin esto el nodo se instala con la marca del producto aunque el central tenga la del revendedor.
#: Lista cerrada, como la del emisor.
CLAVES_DE_LA_MARCA = ("marca", "terminos_propios", "terminos_propios_historial")
CARPETA_DE_LA_MARCA = "marca"
ARCHIVO_DEL_LOGO = "logo.png"
ARCHIVO_DE_LOS_TERMINOS = "terminos.md"
#: Los mismos topes que ``libracore`` (``marca_router`` re-codifica el logo a 1024 px; ``terminos_propios.MAX_TEXTO``).
MAX_LOGO_BYTES = 2_000_000
MAX_TERMINOS = 300_000
_FIRMA_PNG = b"\x89PNG\r\n\x1a\n"

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
    if "LIBRAEDGE_CARGA_INICIAL" in variables and not _ASGI.match(variables["LIBRAEDGE_CARGA_INICIAL"]):
        raise ErrorDeInstalacion(f"{donde}: 'LIBRAEDGE_CARGA_INICIAL' es 'modulo:funcion' (la del producto que trae las"
                                 " existencias de la sucursal).")
    version_esquema = datos.get("version_esquema", "alembic")
    if not isinstance(version_esquema, str) or not version_esquema.strip():
        raise ErrorDeInstalacion(f"{donde}: 'version_esquema' es 'alembic', 'ninguna' o 'modulo:funcion'.")
    definicion = Definicion(
        producto=producto, nombre=_texto(datos, "nombre", donde), app_id=app_id, prefijo_variables=prefijo,
        bases=_bases(datos.get("bases"), donde), asgi=asgi, puerto=puerto, salud=salud, migraciones=migraciones,
        base_auth=base_auth, extras=tuple(e.strip() for e in extras), variables=dict(variables),
        version_esquema=version_esquema.strip())
    pisadas = set(definicion.variables) & (
        set(_variables_generadas(definicion)) | set(VARIABLES_OPCIONALES_DE_LA_CARGA))
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
    #: La marca blanca validada (``None`` si la carga no la trae):
    #: ``{"config": {...}, "logo": bytes | None, "terminos": str | None}``.
    marca: Mapping[str, object] | None = None

    def resumen(self) -> dict:
        """Lo que se puede mostrar: todo menos el secreto."""
        resumen = {
            "producto": self.producto,
            "node_id": self.entorno["LIBRAEDGE_NODE_ID"],
            "prefijo": self.entorno["LIBRAEDGE_PREFIJO"],
            "central": self.entorno["LIBRAEDGE_CENTRAL_URL"],
            "tablas_espejo": len(parsear_tablas_espejo(self.entorno["LIBRAEDGE_TABLAS_ESPEJO"])),
            "sucursal": dict(self.sucursal),
            "emisor": sorted(self.emisor),
            "avisos": list(self.avisos),
        }
        if self.marca:
            resumen["marca"] = {"nombre": self.marca["config"]["marca"].get("nombre", ""),
                                "logo": self.marca["logo"] is not None, "terminos": self.marca["terminos"] is not None}
        solo_lectura = self.entorno.get("LIBRAEDGE_TABLAS_SOLO_LECTURA")
        if solo_lectura:
            resumen["tablas_solo_lectura"] = len(solo_lectura.split(","))
        altas = self.entorno.get("LIBRAEDGE_ALTAS_EN_NODO")
        if altas:
            resumen["altas_en_nodo"] = [parte.partition(":")[0] for parte in altas.split(",")]
        return resumen


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


def _marca(crudo, avisos: list[str]) -> dict | None:
    """Valida la marca blanca de la carga. ``None`` si no trae. Un nodo con marca propia y sin Terminos propios queda
    **congelado** (``libraauth`` ADR-030: sin contrato nadie puede aceptar), y eso se avisa aca y no despues de
    instalar."""
    if crudo is None:
        return None
    if not isinstance(crudo, Mapping):
        raise ErrorDeInstalacion("'marca' tiene que ser un objeto {config, logo_png, terminos}.")
    sobran = sorted(set(crudo) - {"config", "logo_png", "terminos"})
    if sobran:
        raise ErrorDeInstalacion(f"La marca trae claves que no le corresponden: {', '.join(sobran)}.")
    config = crudo.get("config")
    if not isinstance(config, Mapping):
        raise ErrorDeInstalacion("A la marca le falta 'config' (la clave 'marca' del config.json de la instancia).")
    ajenas = sorted(set(config) - set(CLAVES_DE_LA_MARCA))
    if ajenas:
        raise ErrorDeInstalacion(f"La marca trae claves del config.json que no se copian: {', '.join(ajenas)}.")
    propia = config.get("marca")
    if not isinstance(propia, Mapping) or not propia:
        raise ErrorDeInstalacion("La marca no tiene la clave 'marca' con el nombre y los colores.")
    if not any(isinstance(propia.get(c), str) and propia[c].strip() for c in ("nombre", "nombre_corto")):
        raise ErrorDeInstalacion("La marca no tiene nombre.")
    for clave in ("terminos_propios", "terminos_propios_historial"):
        if clave in config and not isinstance(config[clave], Mapping):
            raise ErrorDeInstalacion(f"{clave!r} de la marca tiene que ser un objeto.")
    logo = None
    if crudo.get("logo_png") is not None:
        try:
            logo = base64.b64decode(str(crudo["logo_png"]), validate=True)
        except (binascii.Error, ValueError):
            raise ErrorDeInstalacion("El logo de la marca no es base64 valido.") from None
        if not logo.startswith(_FIRMA_PNG):
            raise ErrorDeInstalacion("El logo de la marca tiene que ser un PNG.")
        if len(logo) > MAX_LOGO_BYTES:
            raise ErrorDeInstalacion(f"El logo de la marca pesa mas de {MAX_LOGO_BYTES // 1_000_000} MB.")
    elif propia.get("logo"):
        avisos.append("La marca declara un logo pero la carga no lo trae: el nodo va a mostrar la marca sin logo.")
    terminos = crudo.get("terminos")
    if terminos is not None:
        if not isinstance(terminos, str) or not terminos.strip():
            raise ErrorDeInstalacion("Los Terminos de la marca tienen que ser un texto.")
        if len(terminos) > MAX_TERMINOS:
            raise ErrorDeInstalacion(f"Los Terminos de la marca pasan de {MAX_TERMINOS} caracteres.")
    if terminos is None or "terminos_propios" not in config:
        avisos.append("La marca no trae Terminos propios completos: con marca propia y sin Terminos el nodo queda"
                      " congelado (nadie puede aceptar) hasta cargarlos.")
    return {"config": {c: config[c] for c in CLAVES_DE_LA_MARCA if c in config}, "logo": logo, "terminos": terminos}


def validar_marca(crudo) -> tuple[dict | None, list[str]]:
    """La marca validada y los avisos. Un logo que no es PNG o una marca sin nombre son error."""
    avisos: list[str] = []
    return _marca(crudo, avisos), avisos


def _escribir_atomico(ruta: str, contenido: bytes) -> bool:
    """Escribe ``contenido`` en ``ruta`` si cambio. ``True`` si escribio."""
    if os.path.isfile(ruta):
        with open(ruta, "rb") as archivo:
            if archivo.read() == contenido:
                return False
    carpeta = os.path.dirname(os.path.abspath(ruta))
    os.makedirs(carpeta, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=".marca-", dir=carpeta)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)
        os.replace(temporal, ruta)
    except BaseException:
        if os.path.exists(temporal):
            os.unlink(temporal)
        raise
    return True


def aplicar_marca(ruta_config: str, marca: Mapping[str, object]) -> list[str]:
    """Escribe la marca del nodo donde ``libracore`` la lee: las claves en el ``config.json`` (fusionado, sin tocar el
    resto) y el logo y los Terminos en ``marca/`` junto a el (``config_manager.MARCA_DIR``). Devuelve lo que cambio."""
    cambios = [f"config.json:{clave}" for clave in fusionar_config(ruta_config, marca["config"])]
    carpeta = os.path.join(os.path.dirname(os.path.abspath(ruta_config)), CARPETA_DE_LA_MARCA)
    if marca["logo"] is not None and _escribir_atomico(os.path.join(carpeta, ARCHIVO_DEL_LOGO), marca["logo"]):
        cambios.append(f"{CARPETA_DE_LA_MARCA}/{ARCHIVO_DEL_LOGO}")
    if marca["terminos"] is not None and _escribir_atomico(
            os.path.join(carpeta, ARCHIVO_DE_LOS_TERMINOS), marca["terminos"].encode("utf-8")):
        cambios.append(f"{CARPETA_DE_LA_MARCA}/{ARCHIVO_DE_LOS_TERMINOS}")
    return cambios


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


def parsear_tablas_solo_lectura(crudo: str, espejo: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """``"productos,categorias"`` -> ``("categorias", "productos")``: nombres SQL validos y **de tablas del espejo**.

    El mismo formato que lee ``libraedge-nodo`` (``cli.tablas_solo_lectura``). Una que el nodo no espeja no se acepta:
    nadie la escribiria nunca (el producto no puede y el espejo no la trae)."""
    nombres: set[str] = set()
    for parte in (crudo or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        if not _IDENTIFICADOR_SQL.match(parte):
            raise ErrorDeInstalacion(f"LIBRAEDGE_TABLAS_SOLO_LECTURA tiene una entrada no valida: {parte!r}.")
        nombres.add(parte)
    sobran = sorted(nombres - set(espejo))
    if sobran:
        raise ErrorDeInstalacion(
            f"LIBRAEDGE_TABLAS_SOLO_LECTURA trae tablas que el nodo no espeja: {', '.join(sobran)}.")
    return tuple(sorted(nombres))


_VALOR_DE_CONDICION = re.compile(r"^[A-Za-z0-9_-]+$")
#: El mapeo de los ids provisorios a los del central (``libraedge.mapeo.TABLA``): se repite aca porque este modulo solo
#: usa la biblioteca estandar (se copia al lado de los scripts del instalador y corre con el Python del producto).
_TABLA_DEL_MAPEO = "libraedge_mapeo"


def parsear_altas_en_nodo(crudo: str, espejo: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """``"clients:cc_asientos.tercero_id@rol=cliente,parties"`` -> ``("clients", "parties")``: las tablas con alta en el
    nodo. El mismo formato que lee ``libraedge-nodo`` (``libraedge.altas.parsear_altas``; un test fija que coinciden).

    Cada tabla tiene que ser **del espejo** y de una sola columna de clave, y el espejo tiene que traer
    ``libraedge_mapeo``: sin el, el nodo nunca se entera del id que le asigna el central."""
    tablas: list[str] = []
    for parte in (crudo or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        tabla, _, referencias = parte.partition(":")
        tabla = tabla.strip()
        if not _IDENTIFICADOR_SQL.match(tabla):
            raise ErrorDeInstalacion(f"LIBRAEDGE_ALTAS_EN_NODO no se entiende: la tabla {tabla!r} no es valida.")
        for referencia in (r.strip() for r in referencias.split("+") if r.strip()):
            cabeza, *condiciones = referencia.split("@")
            hija, punto, columna = cabeza.partition(".")
            if not punto or not _IDENTIFICADOR_SQL.match(hija) or not _IDENTIFICADOR_SQL.match(columna):
                raise ErrorDeInstalacion(
                    f"LIBRAEDGE_ALTAS_EN_NODO no se entiende: la referencia {referencia!r} es tabla.columna.")
            for condicion in condiciones:
                nombre, igual, valor = condicion.partition("=")
                if not igual or not _IDENTIFICADOR_SQL.match(nombre) or not _VALOR_DE_CONDICION.match(valor):
                    raise ErrorDeInstalacion(
                        f"LIBRAEDGE_ALTAS_EN_NODO no se entiende: la condicion {condicion!r} es columna=valor.")
        tablas.append(tabla)
    sobran = sorted(set(tablas) - set(espejo))
    if sobran:
        raise ErrorDeInstalacion(f"LIBRAEDGE_ALTAS_EN_NODO trae tablas que el nodo no espeja: {', '.join(sobran)}.")
    compuestas = sorted(t for t in tablas if len(espejo[t]) != 1)
    if compuestas:
        raise ErrorDeInstalacion(
            f"LIBRAEDGE_ALTAS_EN_NODO trae tablas con clave compuesta: {', '.join(compuestas)}.")
    if tablas and _TABLA_DEL_MAPEO not in espejo:
        raise ErrorDeInstalacion(
            f"LIBRAEDGE_ALTAS_EN_NODO pide {_TABLA_DEL_MAPEO} en LIBRAEDGE_TABLAS_ESPEJO y no esta: sin el mapeo el "
            "nodo no reapunta lo que dio de alta.")
    return tuple(sorted(set(tablas)))


#: Como se valida cada variable opcional de la carga (recibe el texto y el espejo).
_VALIDADORES_OPCIONALES = {
    "LIBRAEDGE_TABLAS_SOLO_LECTURA": lambda valor, espejo: parsear_tablas_solo_lectura(valor, espejo),
    "LIBRAEDGE_ALTAS_EN_NODO": lambda valor, espejo: parsear_altas_en_nodo(valor, espejo),
}


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
    sobran = sorted(set(crudo) - set(VARIABLES_DE_LA_CARGA) - set(VARIABLES_OPCIONALES_DE_LA_CARGA))
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
    espejo = parsear_tablas_espejo(entorno["LIBRAEDGE_TABLAS_ESPEJO"])
    for nombre in VARIABLES_OPCIONALES_DE_LA_CARGA:
        valor = crudo.get(nombre)
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue  # no la declaro el producto: el nodo se instala igual que en v0.16
        if not isinstance(valor, str):
            raise ErrorDeInstalacion(f"{nombre} tiene que ser un texto.")
        valor = valor.strip()
        _sin_saltos(nombre, valor)
        _VALIDADORES_OPCIONALES[nombre](valor, espejo)
        entorno[nombre] = valor
    avisos: list[str] = []
    emisor = _emisor(datos.get("emisor"), avisos)
    sucursal = datos.get("sucursal") or {}
    if not isinstance(sucursal, Mapping):
        raise ErrorDeInstalacion("'sucursal' es un objeto {id, nombre}.")
    marca = _marca(datos.get("marca"), avisos)
    return Carga(producto, entorno, emisor, {k: str(v) for k, v in sucursal.items()}, tuple(avisos), marca)


def leer_carga(ruta: str, producto: str) -> Carga:
    return carga_desde_dict(_leer_json(ruta, "la carga del nodo"), producto)


# ── el entorno del nodo ─────────────────────────────────────────────────────


def _variables_generadas(definicion: Definicion) -> tuple[str, ...]:
    variables = [*VARIABLES_DE_LA_CARGA, "LIBRAEDGE_DATABASE_URL", "LIBRAEDGE_ESTADO", "LIBRAEDGE_VERSION_ESQUEMA"]
    for base in definicion.bases:
        variables.extend(base.variables)
    variables.extend(["DATA_DIR", "SECRET_KEY", *definicion.variables_de_admin, "TZ", *VARIABLES_FIJAS_DEL_NODO])
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
    for nombre in VARIABLES_OPCIONALES_DE_LA_CARGA:
        if carga.entorno.get(nombre):
            entorno[nombre] = carga.entorno[nombre]
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
    entorno.update(VARIABLES_FIJAS_DEL_NODO)
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


def completar_entorno(texto: str, definicion: Definicion | None = None) -> tuple[str, list[str]]:
    """Agrega al ``nodo.env`` las :data:`VARIABLES_FIJAS_DEL_NODO` que no tenga, sin tocar lo demas.

    Es para el nodo que se actualiza **sin** la carga (``apt`` en Linux, o el instalador de Windows sin vincular sobre
    un nodo ya vinculado): ahi el ``nodo.env`` no se vuelve a armar y una variable fija nueva no llegaria nunca. Una
    variable que ya esta, con cualquier valor, se respeta. Devuelve el texto nuevo y los nombres agregados.

    Con la ``definicion`` del producto (v0.20, ADR-023) tambien agrega sus ``variables`` que falten: asi una variable
    nueva del producto (``LIBRAEDGE_CARGA_INICIAL``) llega a los nodos ya instalados con solo actualizar, sin volver a
    vincular. Con la carga el valor de la definicion **pisa** (``armar_entorno``); aca se respeta el que ya estaba,
    para no deshacer lo que soporte haya tocado a mano en un nodo."""
    presentes = leer_entorno(texto)
    por_agregar = dict(VARIABLES_FIJAS_DEL_NODO)
    if definicion is not None:
        por_agregar.update(definicion.variables)
    faltan = {nombre: valor for nombre, valor in por_agregar.items() if nombre not in presentes}
    if not faltan:
        return texto, []
    nuevo = texto.lstrip("\ufeff")
    if nuevo and not nuevo.endswith("\n"):
        nuevo += "\n"
    nuevo += formatear_entorno(faltan)
    return nuevo, list(faltan)


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


def marca_desde_config(ruta: str) -> dict | None:
    """La marca blanca de la instancia (las :data:`CLAVES_DE_LA_MARCA` del ``config.json``, el logo y los Terminos de
    ``marca/`` junto a el) lista para ir en la carga, o ``None`` si la instancia no tiene marca propia."""
    if not os.path.exists(ruta):
        return None
    datos = _leer_json(ruta, "el config.json del central")
    if not isinstance(datos, dict) or not isinstance(datos.get("marca"), dict) or not datos["marca"]:
        return None
    marca: dict = {"config": {c: datos[c] for c in CLAVES_DE_LA_MARCA if c in datos}}
    carpeta = os.path.join(os.path.dirname(os.path.abspath(ruta)), CARPETA_DE_LA_MARCA)
    logo = os.path.join(carpeta, ARCHIVO_DEL_LOGO)
    if os.path.isfile(logo):
        with open(logo, "rb") as archivo:
            marca["logo_png"] = base64.b64encode(archivo.read()).decode("ascii")
    terminos = os.path.join(carpeta, ARCHIVO_DE_LOS_TERMINOS)
    if os.path.isfile(terminos):
        with open(terminos, encoding="utf-8") as archivo:
            marca["terminos"] = archivo.read()
    return marca


def ruta_config_del_central(entorno: Mapping[str, str] | None = None) -> str:
    """Donde lo lee libracore (``config_manager``): ``$DATA_DIR/config.json``, o el directorio actual."""
    entorno = os.environ if entorno is None else entorno
    return os.path.join(entorno.get("DATA_DIR") or os.getcwd(), "config.json")


def armar_carga(producto: str, entorno_del_alta: Mapping[str, str], central_url: str,
                emisor: Mapping[str, object] | None, sucursal: Mapping[str, str] | None = None,
                marca: Mapping[str, object] | None = None) -> dict:
    """La carga que lee el instalador, validada con las mismas reglas con que la va a leer."""
    entorno = dict(entorno_del_alta)
    entorno["LIBRAEDGE_CENTRAL_URL"] = central_url
    carga = {
        "formato": FORMATO_CARGA,
        "producto": producto,
        "entorno": {nombre: entorno.get(nombre, "") for nombre in VARIABLES_DE_LA_CARGA},
        "sucursal": dict(sucursal or {}),
    }
    for nombre in VARIABLES_OPCIONALES_DE_LA_CARGA:
        if (entorno.get(nombre) or "").strip():
            carga["entorno"][nombre] = entorno[nombre]
    if emisor is not None:
        carga["emisor"] = dict(emisor)
    if marca:
        carga["marca"] = dict(marca)
    validada = carga_desde_dict(carga, producto)
    carga["entorno"] = dict(validada.entorno)
    return carga


def escribir_carga(ruta: str, carga: Mapping) -> None:
    """Crea el archivo con permisos 600 y **no pisa** uno que ya existe (podria ser la carga de otro nodo)."""
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
        json.dump(carga, archivo, ensure_ascii=False, indent=2)
        archivo.write("\n")


# ── el codigo de vinculacion (ADR-014) ──────────────────────────────────────

#: Las mismas reglas que ``libraedge.vinculacion`` (este modulo no puede importarlo): 8 simbolos sin ``0 O 1 I L``.
_ALFABETO_DEL_CODIGO = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
#: Donde suele estar el almacen de certificados del sistema en Linux, por si el OpenSSL del Python del nodo no lo
#: encuentra solo (el de python-build-standalone puede traer otra ruta por defecto).
_ALMACENES_LINUX = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem")


def normalizar_direccion(direccion: str) -> str:
    """``sistema.contalibra.com.ar`` -> ``https://sistema.contalibra.com.ar``. Solo https (http solo a esta PC).

    Es lo que muestra la pantalla «Sucursal sin internet» al lado del codigo: sin esquema, se le pone ``https://``."""
    direccion = (direccion or "").strip()
    if not direccion:
        raise ErrorDeInstalacion("Falta la direccion del sistema (la que muestra «Sucursal sin internet»).")
    if "://" not in direccion:
        direccion = "https://" + direccion
    try:
        return normalizar_url_del_central(direccion)
    except ErrorDeInstalacion:
        raise ErrorDeInstalacion(
            "La direccion del sistema no es valida: es la que muestra «Sucursal sin internet» al lado del codigo"
            " (por ejemplo sistema.contalibra.com.ar), siempre por https.") from None


def normalizar_codigo(codigo: str) -> str:
    """``k7q4 m2xp`` -> ``K7Q4-M2XP``. ``ErrorDeInstalacion`` si no puede ser un codigo (sin decir cual es)."""
    limpio = "".join(c for c in (codigo or "") if c not in " -\t\r\n").upper()
    if len(limpio) != 8 or any(c not in _ALFABETO_DEL_CODIGO for c in limpio):
        raise ErrorDeInstalacion(
            "El codigo de vinculacion no tiene el formato de la pantalla (8 letras y numeros, como K7Q4-M2XP).")
    return f"{limpio[:4]}-{limpio[4:]}"


def leer_codigo(archivo: str | None = None, entorno: Mapping[str, str] | None = None) -> str:
    """El codigo, de ``archivo`` o de :data:`VARIABLE_CODIGO`. Nunca de un argumento: la linea de comandos se loguea."""
    if archivo:
        texto = _leer_texto(archivo)
        if not texto.strip():
            raise ErrorDeInstalacion(f"El archivo del codigo esta vacio o no existe: {archivo}")
        return normalizar_codigo(texto)
    entorno = os.environ if entorno is None else entorno
    if not (entorno.get(VARIABLE_CODIGO) or "").strip():
        raise ErrorDeInstalacion(f"Falta el codigo de vinculacion ({VARIABLE_CODIGO} o --archivo-codigo).")
    return normalizar_codigo(entorno[VARIABLE_CODIGO])


def contexto_tls():
    """El contexto de https con verificacion de certificado y de nombre. Nunca se apaga la verificacion.

    En Windows, ``create_default_context`` lee el almacen del sistema; en Linux se suma el archivo del sistema por si
    el OpenSSL del Python del nodo busca en otro lado (``SSL_CERT_FILE`` sigue mandando si esta)."""
    import ssl

    contexto = ssl.create_default_context()
    if sys.platform.startswith("linux") and not os.environ.get("SSL_CERT_FILE"):
        for ruta in _ALMACENES_LINUX:
            if os.path.isfile(ruta):
                contexto.load_verify_locations(cafile=ruta)
                break
    return contexto


def _mensaje_del_central(cuerpo: bytes) -> str | None:
    try:
        detalle = json.loads(cuerpo.decode("utf-8")).get("detail")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    if isinstance(detalle, dict) and isinstance(detalle.get("mensaje"), str):
        return detalle["mensaje"][:400]
    return detalle[:400] if isinstance(detalle, str) else None


def canjear_codigo(direccion: str, codigo: str, producto: str, *, abrir=None, timeout: float = 30) -> dict:
    """Canjea el codigo contra el central y devuelve la carga **validada**, como si se la hubiera elegido de un archivo.

    🔴 La respuesta tiene el secreto del nodo: no se imprime, no va en el texto de ningun error. Los errores son los
    del central (que tampoco lo traen) o el motivo de red, sin el codigo.
    """
    import urllib.error
    import urllib.request

    base = normalizar_direccion(direccion)
    codigo = normalizar_codigo(codigo)
    pedido = urllib.request.Request(
        base + RUTA_DE_CANJE, method="POST",
        data=json.dumps({"codigo": codigo, "producto": producto}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "libraedge-instalador"})
    abrir = abrir or (lambda p: urllib.request.urlopen(p, timeout=timeout, context=contexto_tls()))  # noqa: S310
    try:
        with abrir(pedido) as respuesta:
            cuerpo = respuesta.read()
    except urllib.error.HTTPError as exc:
        mensaje = _mensaje_del_central(exc.read() or b"")
        if exc.code == 404:
            raise ErrorDeInstalacion(mensaje or "El codigo no es valido o ya vencio: pedir otro.") from None
        if exc.code == 429:
            espera = exc.headers.get("Retry-After") if exc.headers else None
            raise ErrorDeInstalacion((mensaje or "Demasiados intentos.")
                                     + (f" (reintentar en {espera} segundos)" if espera else "")) from None
        raise ErrorDeInstalacion(f"El sistema contesto {exc.code} al canjear el codigo"
                                 + (f": {mensaje}" if mensaje else ".")) from None
    except urllib.error.URLError as exc:
        motivo = getattr(exc, "reason", exc)
        raise ErrorDeInstalacion(
            f"No se pudo conectar con {base} para canjear el codigo ({type(motivo).__name__}: {motivo}). Revisar la"
            " direccion y la conexion a internet de esta PC.") from None
    except (OSError, TimeoutError) as exc:
        raise ErrorDeInstalacion(f"No se pudo conectar con {base} para canjear el codigo ({type(exc).__name__}).") \
            from None
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ErrorDeInstalacion(f"{base} no contesto una carga de nodo: revisar la direccion.") from None
    carga_desde_dict(datos, producto)  # las mismas reglas que una carga de archivo
    return datos


def escribir_carga_canjeada(ruta: str, carga: Mapping) -> None:
    """Escribe la carga canjeada con permisos de secreto, como la del archivo.

    Si el archivo ya existe se escribe encima conservando su ACL (Windows: lo crea antes ``instalar.ps1`` solo para
    SYSTEM y Administradores, igual que el ``nodo.env``). Si no existe se crea con permisos 600 (Linux)."""
    contenido = json.dumps(carga, ensure_ascii=False, indent=2) + "\n"
    if os.path.isfile(ruta):
        escribir_en_archivo_protegido(ruta, contenido)
        return
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
        archivo.write(contenido)


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


# ── como se entra al producto en el local (ADR-013) ────────────────────────
#
# En la PC con el nodo se entra SIEMPRE por la copia local, aunque haya internet: el nodo sincroniza por detras. Quien
# entra por la nube desde el local se queda sin sistema cuando se corta internet (VM, 2026-10-20). El instalador lo
# dice al terminar y la bandeja lo repite, con la IP de esta PC ya completa para los otros puestos del local.

#: Una direccion publica cualquiera: conectar un socket UDP no manda nada, pero hace que el sistema elija la interfaz
#: de la ruta por defecto, que es la de la red del local.
_DESTINO_DE_PRUEBA = ("8.8.8.8", 80)


def es_ip_de_la_lan(ip: str) -> bool:
    """IPv4 privada (10/8, 172.16/12, 192.168/16), sin loopback ni link-local (169.254, una PC sin DHCP)."""
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (direccion.version == 4 and direccion.is_private and not direccion.is_loopback
            and not direccion.is_link_local and not direccion.is_unspecified)


def _ip_de_la_ruta_por_defecto() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(_DESTINO_DE_PRUEBA)
        return sock.getsockname()[0]


def _ips_del_nombre() -> list[str]:
    return socket.gethostbyname_ex(socket.gethostname())[2]


def ip_de_la_lan(ruta_por_defecto: Callable[[], str | None] = _ip_de_la_ruta_por_defecto,
                 del_nombre: Callable[[], list[str]] = _ips_del_nombre) -> str | None:
    """La IP privada de esta PC en la red del local, o ``None``.

    Si hay varias, la de la ruta por defecto. Sin ruta por defecto (el local sin router hacia afuera), la primera
    privada que resuelva el nombre de la PC. Nunca levanta: sin IP, el texto lo dice y el instalador sigue."""
    for buscar in (lambda: [ruta_por_defecto()], del_nombre):
        try:
            candidatas = buscar()
        except (OSError, UnicodeError):
            continue
        for ip in candidatas:
            if ip and es_ip_de_la_lan(ip):
                return ip
    return None


def url_local(puerto: int) -> str:
    """Lo que abre el acceso directo del producto en esta PC."""
    return f"http://localhost:{puerto}/"


def url_de_la_lan(ip: str | None, puerto: int) -> str:
    """La direccion para los otros puestos del local."""
    return f"http://{ip}:{puerto}" if ip else f"http://<IP de esta PC>:{puerto}"


def acceso(definicion: Definicion, ip: str | None) -> dict:
    """Como se entra, en datos (``--json``: la bandeja) y en texto (``lineas``: el final de la instalacion).

    El texto va sin acentos, como el resto de los mensajes del instalador: Inno Setup lo lee con la pagina de codigos
    del sistema."""
    otras = url_de_la_lan(ip, definicion.puerto)
    lineas = [
        "En este local se entra siempre por la copia local, aunque haya internet:",
        f'  en esta PC, con el acceso directo "{definicion.nombre}" (o {url_local(definicion.puerto)});',
        f"  desde los otros puestos, con {otras}"
        + ("" if ip else " (no se encontro la IP de esta PC en la red del local)"),
    ]
    lineas.append("La direccion de la nube es para entrar desde afuera del local.")
    return {"nombre": definicion.nombre, "puerto": definicion.puerto, "ip": ip,
            "url_local": url_local(definicion.puerto), "url_lan": otras, "lineas": lineas}


#: Donde contesta el nodo a los puestos que lo buscan (``libraedge/descubrimiento.py``, ADR-017). UDP.
PUERTO_DESCUBRIMIENTO = 47823
FORMATO_DESCUBRIMIENTO = "libraedge-descubrimiento/1"
#: Largo maximo de los textos libres que contesta el nodo (la respuesta entera no pasa de 512 bytes).
_LARGO_DESCUBRIMIENTO = 80


def datos_de_descubrimiento(definicion: Definicion, *, carga: Carga | None = None,
                            entorno: Mapping[str, str] | None = None, config: Mapping | None = None,
                            previo: Mapping | None = None) -> dict:
    """Lo que el nodo contesta a los puestos del local (ADR-017): **nada secreto**. Producto, nombre y puerto, de la
    definicion; el nodo y la sucursal, de la carga (o del ``nodo.env`` y del archivo anterior, al actualizar sin
    carga); el negocio, del emisor (``empresa_nombre`` de la carga o del ``config.json``)."""
    entorno = entorno or {}
    previo = previo if isinstance(previo, Mapping) else {}

    def corto(valor) -> str:
        texto = " ".join(str(valor or "").split())
        return texto[:_LARGO_DESCUBRIMIENTO]

    nodo = (carga.entorno.get("LIBRAEDGE_NODE_ID") if carga else None) or entorno.get("LIBRAEDGE_NODE_ID") \
        or previo.get("nodo") or ""
    sucursal = (carga.sucursal.get("nombre") if carga else None) or previo.get("sucursal") or ""
    negocio = ((carga.emisor or {}).get("empresa_nombre") if carga else None) \
        or (config or {}).get("empresa_nombre") or previo.get("negocio") or ""
    return {
        "formato": FORMATO_DESCUBRIMIENTO,
        "producto": definicion.producto,
        "nombre": definicion.nombre,
        "puerto": definicion.puerto,
        "nodo": corto(nodo),
        "sucursal": corto(sucursal),
        "negocio": corto(negocio),
    }


def escribir_descubrimiento(ruta: str, datos: Mapping) -> None:
    """Escribe ``descubrimiento.json`` legible por todos (0644): lo lee el servicio que contesta, que corre sin
    privilegios, y no tiene secretos."""
    carpeta = os.path.dirname(os.path.abspath(ruta))
    os.makedirs(carpeta, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=".descubrimiento-", suffix=".json", dir=carpeta)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
            json.dump(dict(datos), archivo, ensure_ascii=False, indent=2)
        os.chmod(temporal, 0o644)
        os.replace(temporal, ruta)
    except BaseException:
        if os.path.exists(temporal):
            os.unlink(temporal)
        raise


def _leer_json_si_hay(ruta: str | None):
    if not ruta or not os.path.exists(ruta):
        return None
    try:
        return _leer_json(ruta, ruta)
    except ErrorDeInstalacion:
        return None


def favoritos_con(actual: str, agregar: str) -> str:
    """Los favoritos del dock de GNOME (``org.gnome.shell favorite-apps``, un ``as`` de GVariant como lo imprime
    ``gsettings get``) con ``agregar`` al final si no estaba (ADR-013). Lo usa ``linux/dock.sh``.

    ``"['firefox_firefox.desktop', 'yelp.desktop']"`` o ``"@as []"`` -> el mismo formato, que es el de un
    ``.gschema.override``."""
    texto = actual.strip()
    if texto.startswith("@as"):
        texto = texto[3:].strip()
    try:
        lista = ast.literal_eval(texto) if texto else []
    except (ValueError, SyntaxError) as exc:
        raise ErrorDeInstalacion(f"favorite-apps no es una lista: {actual!r}") from exc
    if not isinstance(lista, list) or not all(isinstance(x, str) for x in lista):
        raise ErrorDeInstalacion(f"favorite-apps no es una lista de textos: {actual!r}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.desktop", agregar):
        raise ErrorDeInstalacion(f"No es el nombre de un .desktop: {agregar!r}")
    if agregar not in lista:
        lista.append(agregar)
    for item in lista:
        if "'" in item or "\\" in item:
            raise ErrorDeInstalacion(f"Un favorito con comillas o barras no se reescribe: {item!r}")
    return "[" + ", ".join(f"'{item}'" for item in lista) + "]"


# ── la linea de comandos ────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libraedge_instalador", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    plan = sub.add_parser("plan", help="la definicion validada, en JSON (sin secretos)")
    plan.add_argument("--definicion", required=True)
    validar = sub.add_parser("validar", help="valida la carga contra el producto; imprime un resumen sin secretos")
    validar.add_argument("--definicion", required=True)
    validar.add_argument("--carga", required=True)
    canje = sub.add_parser("canjear", help="canjea el codigo de vinculacion por la carga y la escribe (ADR-014)")
    canje.add_argument("--definicion", required=True)
    canje.add_argument("--direccion", required=True, help="la direccion del sistema (sistema.contalibra.com.ar)")
    canje.add_argument("--archivo-codigo", help=f"archivo con el codigo; sin esto, de la variable {VARIABLE_CODIGO}")
    canje.add_argument("--salida", required=True, help="donde se escribe la carga (permisos de secreto)")
    entorno = sub.add_parser("entorno", help="escribe el nodo.env (que ya tiene que existir, con su ACL)")
    entorno.add_argument("--definicion", required=True)
    entorno.add_argument("--carga", required=True)
    entorno.add_argument("--clave-pg", required=True, help="ARCHIVO con la clave del PostgreSQL embebido")
    entorno.add_argument("--estado", required=True, help="ruta del estado.json del proceso del nodo")
    entorno.add_argument("--data-dir", required=True, help="DATA_DIR del producto (config.json, logos...)")
    entorno.add_argument("--salida", required=True, help="el nodo.env")
    entorno.add_argument("--previo", help="lo conservado de un nodo.env borrado al desinstalar (Linux): SECRET_KEY y"
                         " la clave del admin local, si el nodo.env no los tiene")
    config = sub.add_parser("config-emisor", help="copia los datos del emisor de la carga al config.json")
    config.add_argument("--definicion", required=True)
    config.add_argument("--carga", required=True)
    config.add_argument("--config", required=True)
    completar = sub.add_parser("completar-entorno",
                               help="agrega a un nodo.env existente las variables fijas que le falten (ADR-016) y,"
                                    " con --definicion, las del producto (ADR-023)")
    completar.add_argument("--entorno", required=True)
    completar.add_argument("--definicion", help="el producto.json: sus `variables` tambien se agregan si faltan")
    revisar = sub.add_parser("revisar-entorno", help="lista las variables que faltan en un nodo.env (sin valores)")
    revisar.add_argument("--definicion", required=True)
    revisar.add_argument("--entorno", required=True)
    entrar = sub.add_parser("acceso", help="como se entra al producto en el local, con la IP de esta PC (ADR-013)")
    entrar.add_argument("--definicion", required=True)
    entrar.add_argument("--json", action="store_true", help="en JSON (la bandeja), en vez del texto")
    fav = sub.add_parser("favoritos", help="favorite-apps del dock con un .desktop mas (ADR-013, Linux)")
    fav.add_argument("--actual", required=True, help="lo que imprime gsettings get org.gnome.shell favorite-apps")
    fav.add_argument("--agregar", required=True, help="el .desktop que se agrega al final si no esta")
    desc = sub.add_parser("descubrimiento", help="lo que el nodo contesta a los puestos (ADR-017, sin secretos)")
    desc.add_argument("--definicion", required=True)
    desc.add_argument("--salida", required=True, help="descubrimiento.json (0644)")
    desc.add_argument("--carga", help="la carga del nodo (de ahi salen el nodo, la sucursal y el negocio)")
    desc.add_argument("--entorno", help="el nodo.env, si no hay carga (solo se lee LIBRAEDGE_NODE_ID)")
    desc.add_argument("--config", help="el config.json del nodo (empresa_nombre), si no hay carga")
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
        if args.comando == "completar-entorno":
            if not os.path.isfile(args.entorno):
                raise ErrorDeInstalacion(f"No esta el archivo de entorno: {args.entorno}")
            texto, agregadas = completar_entorno(
                _leer_texto(args.entorno), leer_definicion(args.definicion) if args.definicion else None)
            if agregadas:
                escribir_en_archivo_protegido(args.entorno, texto)
            detalle = f" ({', '.join(agregadas)})." if agregadas else "."
            print(f"nodo.env: {len(agregadas)} variables agregadas{detalle}")
            return 0
        if args.comando == "favoritos":
            print(favoritos_con(args.actual, args.agregar))
            return 0
        definicion = leer_definicion(args.definicion)
        if args.comando == "plan":
            print(json.dumps(definicion.plan(), ensure_ascii=False))
            return 0
        if args.comando == "acceso":
            datos = acceso(definicion, ip_de_la_lan())
            print(json.dumps(datos, ensure_ascii=False) if args.json else "\n".join(datos["lineas"]))
            return 0
        if args.comando == "descubrimiento":
            carga = leer_carga(args.carga, definicion.producto) if args.carga else None
            entorno = leer_entorno(_leer_texto(args.entorno)) if args.entorno else {}
            datos = datos_de_descubrimiento(
                definicion, carga=carga, entorno={"LIBRAEDGE_NODE_ID": entorno.get("LIBRAEDGE_NODE_ID", "")},
                config=_leer_json_si_hay(args.config), previo=_leer_json_si_hay(args.salida))
            escribir_descubrimiento(args.salida, datos)
            print(f"descubrimiento.json: {definicion.nombre} en el puerto {definicion.puerto}, nodo "
                  f"{datos['nodo'] or '(sin nodo)'}; contesta en UDP {PUERTO_DESCUBRIMIENTO}.")
            return 0
        if args.comando == "revisar-entorno":
            faltan = revisar_entorno(definicion, leer_entorno(_leer_texto(args.entorno)))
            for nombre in faltan:
                print(f"FALTA {nombre}")
            return 1 if faltan else 0
        if args.comando == "canjear":
            carga_cruda = canjear_codigo(args.direccion, leer_codigo(args.archivo_codigo), definicion.producto)
            escribir_carga_canjeada(args.salida, carga_cruda)
            print(json.dumps(carga_desde_dict(carga_cruda, definicion.producto).resumen(), ensure_ascii=False))
            return 0
        carga = leer_carga(args.carga, definicion.producto)
        if args.comando == "validar":
            print(json.dumps(carga.resumen(), ensure_ascii=False))
            return 0
        if args.comando == "config-emisor":
            if carga.marca:
                cambios = aplicar_marca(args.config, carga.marca)
                print(f"Marca blanca: {len(cambios)} cambios" + (f" ({', '.join(cambios)})." if cambios else "."))
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
        # Lo del nodo.env actual manda; lo conservado al desinstalar (Linux, ADR-012) solo completa lo que falta.
        previo = {**(leer_entorno(_leer_texto(args.previo)) if args.previo else {}),
                  **{k: v for k, v in leer_entorno(_leer_texto(args.salida)).items() if v.strip()}}
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
