"""«Sucursal sin internet»: el aprovisionamiento del nodo desde la pantalla del central (ADR-011 y ADR-014).

Hasta v0.10 dar de alta un nodo era un comando en el VPS (``libraedge-central nodo registrar ... --carga``, ADR-008 y
ADR-010). Esto es **lo mismo**, montado como router dentro del producto para que lo haga el administrador de la
instancia desde su navegador. Para instalar el nodo en la PC del local hacen falta dos cosas:

1. el **instalador** de su producto: un link fijo a los Releases de LibraEdge (:func:`url_del_instalador`), y
2. la **carga del nodo**, con el secreto. Desde v0.14 (ADR-014) se lleva como **dirección + código de vinculación**: la
   pantalla muestra «Dirección: sistema.contalibra.com.ar · Código: K7Q4-M2XP», el instalador los pide y canjea el
   código por la carga contra la ruta pública de :func:`create_canje_router`. El archivo ``carga-nodo.json`` (que se
   descarga una sola vez) queda como alternativa para soporte.

No hay lógica nueva de aprovisionamiento: el router llama a ``libraedge.aprovisionamiento`` (``publicar``,
``registrar``, ``dar_de_baja``) y arma la carga con ``libraedge.instalador.armar_carga``, las mismas funciones que el
CLI; el código vive en ``libraedge.vinculacion``. Lo del producto entra por las costuras de siempre: su
``Aprovisionamiento`` (tablas, sucursales) y la fábrica de conexiones (la de ``libracore.db.core.get_connection``).

Rutas de administración (``create_nodos_router``, con ``prefijo="/api/nodos-offline"``)::

    GET    /api/nodos-offline                         el estado: sucursales, nodos (y su código pendiente), central
    GET    /api/nodos-offline/menu                    {contratado, nodos_activos, visible}: compatibilidad
    POST   /api/nodos-offline/vinculacion             {"sucursal_id": "2"} -> alta de un nodo nuevo; devuelve el CÓDIGO
    POST   /api/nodos-offline/{node_id}/vinculacion   un código nuevo para el nodo (anula el pendiente)
    DELETE /api/nodos-offline/{node_id}/vinculacion   anula el código pendiente
    POST   /api/nodos-offline                         {"sucursal_id": "2"} -> alta con el ARCHIVO de carga (soporte)
    POST   /api/nodos-offline/{node_id}/carga         misma PC, archivo de carga nuevo (invalida el anterior; soporte)
    DELETE /api/nodos-offline/{node_id}               baja (PC robada o reemplazada): su secreto deja de verificar

Ruta pública (``create_canje_router``, **sin sesión**, sólo https)::

    POST   /sync/v1/vinculacion                       {"codigo": "K7Q4-M2XP", "producto": "contalibra"} -> la CARGA

## 🔴 El secreto

- **Sólo viaja en el cuerpo de la respuesta que ES la carga**: el canje del código o el archivo adjunto, siempre con
  ``Cache-Control: no-store``. Nunca en un ``GET``, nunca en un JSON que la pantalla muestre, nunca en la URL, en una
  cabecera ni en el texto de un error. La respuesta del código **no** lo trae: el secreto se genera recién en el canje.
- **No se guarda en ningún lado del central**: sólo su hash (``NodeRepository.register_node``), y del código también
  sólo el hash. No hay «volver a bajar»: si se pierde, se pide otro código (o se emite otro archivo).
- **Nada de esto se loguea**: los logs del canje dicen IP, resultado y ``node_id``.

## Permisos

**El router de administración no trae guarda propia**: la pone el producto al montarlo, con la de administrador que ya
usa para su configuración (``include_router(..., dependencies=[Depends(<admin>)])``). Es el mismo criterio que el
router de SMTP de libracore: cada producto tiene su propia forma de decir «admin» y LibraEdge no depende de libracore
en runtime. **El de canje se monta sin guarda**: lo llama el instalador, que no tiene sesión; lo protegen el código
(un solo uso, 15 minutos) y el límite de intentos (``libraedge.vinculacion``).

## LibraEdge es parte del sistema (decisión del humano, 2026-10-20)

Desde v0.14 LibraEdge está incluido en todos los planes: no hay adicional ``offline`` que contratar. El alta, la carga
nueva y la baja están disponibles para el administrador de cualquier instancia. Por compatibilidad con las pantallas
de antes, el ``GET`` y el ``GET .../menu`` siguen mandando ``contratado``, siempre ``true``, y el menú ``visible``
siempre ``true``. La guarda del alta sigue siendo una costura del producto (``aprov.verificar_alta``, ``sin_guarda``
por defecto); si una la rechaza, es un 403 con su mensaje.
"""


import json
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from libraedge import aprovisionamiento as ap
from libraedge import vinculacion as vinc
from libraedge.instalador import (
    CLAVES_DEL_EMISOR_NECESARIAS,
    ErrorDeInstalacion,
    armar_carga,
    emisor_desde_config,
    normalizar_url_del_central,
    ruta_config_del_central,
    validar_emisor,
)

#: Donde el CI de LibraEdge publica los instaladores (``.github/workflows/publicar-instalador.yml``, ADR-010). El
#: repo es público y el ``.exe`` no lleva secretos: el del nodo viaja en la carga.
URL_DE_LOS_INSTALADORES = "https://github.com/marianocappucci/libraedge/releases/latest/download"

#: Minutos sin noticias para decir «sin noticias» en la pantalla. El mismo default que ``nodo vigilar``.
UMBRAL_MINUTOS = 15

PREFIJO_DE_RUTA = "/api/nodos-offline"

#: La ruta pública del canje: al lado de las del nodo (``/sync/v1/push``, ``/pull``), que tampoco llevan sesión.
RUTA_DE_CANJE = "/sync/v1/vinculacion"

logger = logging.getLogger("libraedge.vinculacion")

#: Las cabeceras de la respuesta que trae el secreto: que no quede en ninguna caché.
CABECERAS_SIN_CACHE = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


def url_del_instalador(producto: str) -> str:
    """``ventalibra`` -> ``.../releases/latest/download/libraedge-nodo-ventalibra.exe``."""
    producto = (producto or "").strip().lower()
    if not producto.isalnum():
        raise ValueError(f"producto no valido: {producto!r}")
    return f"{URL_DE_LOS_INSTALADORES}/libraedge-nodo-{producto}.exe"


# ── la URL del central ──────────────────────────────────────────────────────


def _origen(url: str) -> str | None:
    partes = urlsplit((url or "").strip())
    if not partes.scheme or not partes.hostname:
        return None
    return f"{partes.scheme}://{partes.netloc}"


def candidatas_a_url_del_central(prefijo: str, cabeceras: Any = None,
                                 entorno: Any = None) -> list[tuple[str, str]]:
    """De dónde puede salir la URL pública de la instancia, en orden: ``[(origen, url)]``.

    1. ``LIBRAEDGE_CENTRAL_URL``, si el VPS la fija (la salida explícita);
    2. el origen de ``<PREFIJO>_RESET_URL_BASE``, que los tres productos ya tienen para los mails de recuperación;
    3. el ``Origin`` del pedido (el navegador del administrador está en esa URL);
    4. ``X-Forwarded-Proto`` + ``Host`` (detrás del proxy).

    Se usa la primera que :func:`normalizar_url_del_central` acepte: **sólo https** (el secreto viaja en cada pedido
    del nodo). Por eso un ``Origin`` ``http://localhost`` de un túnel no sirve de nada salvo que no haya otra.
    """
    entorno = os.environ if entorno is None else entorno
    cabeceras = cabeceras or {}
    candidatas: list[tuple[str, str]] = []
    if entorno.get("LIBRAEDGE_CENTRAL_URL"):
        candidatas.append(("LIBRAEDGE_CENTRAL_URL", entorno["LIBRAEDGE_CENTRAL_URL"]))
    reset = entorno.get(f"{prefijo.upper()}_RESET_URL_BASE")
    if reset and _origen(reset):
        candidatas.append((f"{prefijo.upper()}_RESET_URL_BASE", _origen(reset)))
    if cabeceras.get("origin") and _origen(cabeceras["origin"]):
        candidatas.append(("origin", _origen(cabeceras["origin"])))
    host = cabeceras.get("x-forwarded-host") or cabeceras.get("host")
    if host:
        esquema = (cabeceras.get("x-forwarded-proto") or "http").split(",")[0].strip()
        candidatas.append(("host", f"{esquema}://{host.split(',')[0].strip()}"))
    return candidatas


def resolver_url_del_central(prefijo: str, cabeceras: Any = None, entorno: Any = None) -> str:
    """La primera candidata válida (https), normalizada. ``ErrorDeInstalacion`` si ninguna sirve."""
    candidatas = candidatas_a_url_del_central(prefijo, cabeceras, entorno)
    for _, url in candidatas:
        try:
            normalizada = normalizar_url_del_central(url)
        except ErrorDeInstalacion:
            continue
        if urlsplit(normalizada).scheme == "https":
            return normalizada
    raise ErrorDeInstalacion(
        "No se pudo saber la URL pública (https) de esta instancia para la carga del nodo. Definir "
        "LIBRAEDGE_CENTRAL_URL en el entorno del central.")


# ── lo que se lee ───────────────────────────────────────────────────────────


def siempre_incluido(conn) -> bool:
    """LibraEdge está incluido en todos los planes (ADR-014): ``contratado`` (compatibilidad) es siempre ``True``."""
    return True


def adicional_offline_contratado(conn) -> bool:
    """¿Está prendido el módulo ``offline`` (libracore ADR-048)? Sin la tabla ``modulos``, no.

    🔴 Ya no es el default de ``create_nodos_router`` (v0.14, ADR-014): queda por compatibilidad."""
    try:
        fila = conn.execute("SELECT habilitado FROM modulos WHERE modulo = ?", ("offline",)).fetchone()
    except Exception:  # noqa: BLE001 - una base sin `modulos` no tiene el adicional
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001, S110
            pass
        return False
    return bool(fila and fila[0])


def _estado_del_nodo(activo: bool, minutos: float | None, umbral: int) -> str:
    if not activo:
        return "baja"
    if minutos is None:
        return "nunca"
    return "al_dia" if minutos <= umbral else "sin_noticias"


def nodos_de_la_instancia(conn, sucursales: list[ap.Sucursal], *, ahora: datetime | None = None,
                          umbral_minutos: int = UMBRAL_MINUTOS) -> list[dict]:
    """Los nodos registrados, con su sucursal, su estado y su código pendiente. **Sin el hash del secreto.**

    ``vinculacion`` es ``{"vence": ...}`` si el nodo tiene un código vivo (sin usar, sin anular, sin vencer) y ``None``
    si no: el código en sí no se puede volver a mostrar (se guarda el hash), sí pedir otro o anularlo."""
    from libraedge.db.repository import NodeRepository

    nombres = {s.id: s.nombre for s in sucursales}
    altas = {f[0]: f[1] for f in conn.execute("SELECT node_id, installed_at FROM node_identity").fetchall()}
    codigos = vinc.pendientes(conn, ahora=ahora)
    nodos = []
    for nodo in NodeRepository(conn).nodos_para_vigilar(ahora=ahora or datetime.now(UTC)):
        minutos = nodo["minutos_sin_verse"]
        nodos.append({
            "node_id": nodo["node_id"],
            "sucursal_id": str(nodo["branch_id"]),
            "sucursal": nombres.get(str(nodo["branch_id"])),
            "prefijo": nodo["prefijo"],
            "activo": nodo["activo"],
            "registrado": altas.get(nodo["node_id"]),
            "ultima_sincronizacion": nodo["ultima_vez"],
            "minutos_sin_verse": None if minutos is None else round(minutos, 1),
            "estado": _estado_del_nodo(nodo["activo"], minutos, umbral_minutos),
            "vinculacion": ({"vence": codigos[nodo["node_id"]]}
                            if nodo["activo"] and nodo["node_id"] in codigos else None),
        })
    # Los activos primero; dentro de cada grupo, por sucursal y node_id.
    nodos.sort(key=lambda n: (not n["activo"], n["sucursal_id"], n["node_id"]))
    return nodos


def _emisor_y_faltantes(ruta: str) -> tuple[dict | None, list[str], str | None]:
    """El emisor del ``config.json``, lo que le falta para facturar y el error si el CUIT es inválido."""
    try:
        emisor = emisor_desde_config(ruta)
        if emisor is None:
            return None, list(CLAVES_DEL_EMISOR_NECESARIAS), None
        validar_emisor(emisor)
    except ErrorDeInstalacion as exc:
        return None, [], str(exc)
    faltan = [c for c in CLAVES_DEL_EMISOR_NECESARIAS if not str(emisor.get(c, "")).strip()]
    return emisor, faltan, None


def direccion_del_central(url: str) -> str:
    """``https://sistema.contalibra.com.ar`` -> ``sistema.contalibra.com.ar``: lo que se escribe en el instalador.

    El instalador le vuelve a poner ``https://`` (``instalador.normalizar_direccion``). Una URL ``http://`` (sólo
    ``localhost``, en pruebas) se deja entera: sin el esquema el instalador la tomaría como https."""
    return url.split("://", 1)[1] if url.startswith("https://") else url


def nuevo_node_id(sucursal_id: str) -> str:
    """``nodo-2-a1b2c3``: uno nuevo por alta. 🔴 Una PC nueva es un ``node_id`` nuevo (y un prefijo nuevo, ADR-008)."""
    cuerpo = "".join(c for c in str(sucursal_id).lower() if c.isalnum() and c.isascii())[:20] or "x"
    return f"nodo-{cuerpo}-{secrets.token_hex(3)}"


# ── el router ───────────────────────────────────────────────────────────────


def create_nodos_router(aprov: ap.Aprovisionamiento, abrir_conexion: Callable[[], Any], *,
                        producto: str | None = None, prefijo: str = PREFIJO_DE_RUTA,
                        ruta_config: Callable[[], str] | None = None,
                        contratado: Callable[[Any], bool] = siempre_incluido,
                        umbral_minutos: int = UMBRAL_MINUTOS, entorno: Any = None,
                        ahora: Callable[[], datetime] | None = None):
    """El router de «Sucursal sin internet». El producto lo monta **con su guarda de administrador**.

    - ``aprov``: el ``Aprovisionamiento`` del producto (el mismo de ``LIBRAEDGE_APROVISIONAMIENTO``).
    - ``abrir_conexion``: devuelve un context manager de conexión (``libracore.db.core.get_connection``); se abre una
      por pedido, como ``create_sync_router``.
    - ``producto``: el de la carga y el del instalador; por defecto ``aprov.prefijo`` en minúsculas.
    - ``ruta_config``: el ``config.json`` de la instancia (el emisor que viaja en la carga); por defecto
      ``$DATA_DIR/config.json``, como el CLI.
    - ``contratado(conn)``: compatibilidad con las pantallas de antes de v0.14; por defecto siempre ``True``
      (LibraEdge incluido en todos los planes, ADR-014). Sólo informa: el que corta es ``aprov.verificar_alta``.
    - ``entorno``: para los tests; por defecto ``os.environ`` (lo lee la URL del central).
    - ``ahora``: el reloj de los códigos, para los tests; por defecto ``datetime.now(UTC)``.
    """
    try:
        from fastapi import APIRouter, HTTPException, Request, Response
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - el central siempre tiene FastAPI
        raise RuntimeError("FastAPI es opcional; instalar libraedge[server]") from exc

    producto = (producto or aprov.prefijo).strip().lower()
    instalador = url_del_instalador(producto)
    leer_ruta_config = ruta_config or (lambda: ruta_config_del_central(entorno))
    reloj = ahora or (lambda: datetime.now(UTC))

    class Alta(BaseModel):
        sucursal_id: str

    router = APIRouter(prefix=prefijo, tags=["libraedge"])
    _error = _error_http(HTTPException)

    def _como_http(exc: Exception) -> HTTPException:
        if isinstance(exc, ap.AltaRechazada):
            return _error(403, "alta_rechazada", str(exc))
        if isinstance(exc, ap.SegundoNodoEnLaSucursal):
            return _error(409, "sucursal_con_nodo", str(exc))
        if isinstance(exc, ap.RegistroConProblemas):
            return _error(409, "registro_con_problemas", str(exc))
        if isinstance(exc, ap.SucursalInvalida):
            return _error(422, "sucursal_invalida", str(exc))
        return _error(422, "no_se_puede", str(exc))

    def _url_del_central(request) -> str:
        return resolver_url_del_central(aprov.prefijo, request.headers, entorno)

    @router.get("/menu")
    def menu():
        """Compatibilidad con los layouts de antes de v0.14 (``nodosOffline`` del kit): la entrada se ve siempre.

        Hasta v0.13 se veía con el adicional ``offline`` o con nodos activos. Desde ADR-014 LibraEdge está incluido en
        todos los planes y el kit ya no la pregunta; la ruta queda para un layout viejo, con ``visible`` en ``true``.
        """
        with abrir_conexion() as conn:
            esta_contratado = contratado(conn)
            activos = ap.nodos_activos(conn)
        return {"contratado": esta_contratado, "nodos_activos": activos, "visible": True}

    @router.get("")
    def estado(request: Request):
        with abrir_conexion() as conn:
            sucursales = ap.listar_sucursales(conn, aprov)
            nodos = nodos_de_la_instancia(conn, sucursales, ahora=reloj(), umbral_minutos=umbral_minutos)
            esta_contratado = contratado(conn)
        try:
            central, error_central = _url_del_central(request), None
        except ErrorDeInstalacion as exc:
            central, error_central = None, str(exc)
        _, faltan, error_emisor = _emisor_y_faltantes(leer_ruta_config())
        return {
            "producto": producto,
            "contratado": esta_contratado,
            "nodos_activos": sum(1 for n in nodos if n["activo"]),
            "instalador_url": instalador,
            "central_url": central,
            "central_url_error": error_central,
            "direccion": direccion_del_central(central) if central else None,
            "vinculacion_minutos": int(vinc.VIGENCIA.total_seconds() // 60),
            "emisor_faltan": faltan,
            "emisor_error": error_emisor,
            "umbral_minutos": umbral_minutos,
            "sucursales": [{"id": s.id, "nombre": s.nombre, "activa": s.activa, "nodo": s.nodo}
                           for s in sucursales],
            "nodos": nodos,
        }

    def _preparar(request) -> tuple[str, dict | None]:
        """Lo que se valida ANTES de emitir un secreto (como ``_preparar_carga`` del CLI)."""
        try:
            url = _url_del_central(request)
        except ErrorDeInstalacion as exc:
            raise _error(422, "sin_url_del_central", str(exc)) from None
        emisor, _, error_emisor = _emisor_y_faltantes(leer_ruta_config())
        if error_emisor:
            raise _error(422, "emisor_invalido", f"El config.json de la instancia: {error_emisor}")
        return url, emisor

    def _publicar(conn) -> None:
        """Los triggers del changelog y la siembra: sin esto el nodo arranca con el espejo vacío. Idempotente."""
        if isinstance(conn, sqlite3.Connection):  # el central es PostgreSQL; los tests de SQLite no publican
            return
        lineas: list[str] = []
        if ap.publicar(conn, aprov, salida=lineas.append) != 0:
            raise _error(409, "publicacion_incompleta",
                         "Hay tablas de referencia que no se pueden publicar y el nodo quedaría con una copia "
                         "incompleta: " + " ".join(linea.strip() for linea in lineas if "SALTEADA" in linea))

    def _responder_carga(alta: ap.Alta, url: str, emisor: dict | None):
        carga = _carga_de(alta, url, emisor, producto, _error)
        cuerpo = json.dumps(carga, ensure_ascii=False, indent=2) + "\n"
        return Response(
            content=cuerpo.encode("utf-8"), media_type="application/json",
            headers={**CABECERAS_SIN_CACHE,
                     "Content-Disposition": f'attachment; filename="carga-nodo-{alta.node_id}.json"',
                     # Lo que la pantalla puede mostrar sin abrir el archivo: nada secreto.
                     "X-Libraedge-Node-Id": alta.node_id, "X-Libraedge-Prefijo": alta.prefijo},
        )

    def _responder_codigo(conn, node_id: str, sucursal: ap.Sucursal | None, prefijo_del_nodo: str | None, url: str):
        emitido = vinc.emitir(conn, node_id, ahora=reloj())
        conn.commit()
        return JSONResponse(
            {"node_id": node_id, "prefijo": prefijo_del_nodo,
             "sucursal": None if sucursal is None else {"id": str(sucursal.id), "nombre": sucursal.nombre or ""},
             "direccion": direccion_del_central(url), "central_url": url,
             "codigo": emitido.codigo, "vence": emitido.vence,
             "vinculacion_minutos": int(vinc.VIGENCIA.total_seconds() // 60)},
            headers=CABECERAS_SIN_CACHE)

    def _nodo_activo(conn, node_id: str):
        fila = conn.execute("SELECT branch_id, active, prefijo FROM node_identity WHERE node_id = ?",
                            (node_id,)).fetchone()
        if fila is None:
            raise _error(404, "nodo_inexistente", f"No hay ningún nodo {node_id!r}.")
        if not fila[1]:
            raise _error(409, "nodo_dado_de_baja",
                         f"El nodo {node_id!r} está dado de baja: para otra PC se da de alta un nodo nuevo.")
        return fila

    @router.post("/vinculacion")
    def dar_de_alta_con_codigo(cuerpo: Alta, request: Request):
        """El alta de ADR-014: registra el nodo y devuelve dirección + código. **El secreto no viaja acá.**

        El nodo queda registrado con un secreto que nadie conoce (sólo su hash): el canje del código lo vuelve a
        registrar (``ap.registrar``, como la carga nueva) y recién ahí sale el secreto que se usa."""
        url, _ = _preparar(request)
        with abrir_conexion() as conn:
            try:
                sucursal = ap.elegir_sucursal(conn, aprov, cuerpo.sucursal_id.strip() or None)
                _publicar(conn)
                alta = ap.registrar(conn, aprov, nuevo_node_id(sucursal.id), sucursal.id, salida=lambda _: None)
            except ap.ErrorDeAprovisionamiento as exc:
                raise _como_http(exc) from None
            return _responder_codigo(conn, alta.node_id, alta.sucursal, alta.prefijo, url)

    @router.post("/{node_id}/vinculacion")
    def pedir_otro_codigo(node_id: str, request: Request):
        """Un código nuevo para un nodo activo (reinstalar la misma PC, o el anterior venció). Anula el pendiente.

        El secreto en uso **no cambia hasta el canje**: si el nodo está instalado, sigue sincronizando hasta que se
        reinstale con el código."""
        url, _ = _preparar(request)
        with abrir_conexion() as conn:
            fila = _nodo_activo(conn, node_id)
            try:
                # Mismo chequeo que el alta: un codigo que el canje va a rechazar no se emite (ADR-023).
                ap.verificar_registro(conn, aprov)
            except ap.ErrorDeAprovisionamiento as exc:
                raise _como_http(exc) from None
            _publicar(conn)
            sucursal = next((s for s in ap.listar_sucursales(conn, aprov) if s.id == str(fila[0])), None)
            return _responder_codigo(conn, node_id, sucursal, fila[2], url)

    @router.delete("/{node_id}/vinculacion")
    def anular_codigo(node_id: str):
        with abrir_conexion() as conn:
            if conn.execute("SELECT 1 FROM node_identity WHERE node_id = ?", (node_id,)).fetchone() is None:
                raise _error(404, "nodo_inexistente", f"No hay ningún nodo {node_id!r}.")
            anulados = vinc.anular_pendientes(conn, node_id, ahora=reloj())
            conn.commit()
        logger.info("Códigos de vinculación anulados para el nodo %s: %s.", node_id, anulados)
        return {"node_id": node_id, "anulados": anulados}

    @router.post("")
    def dar_de_alta(cuerpo: Alta, request: Request):
        url, emisor = _preparar(request)
        with abrir_conexion() as conn:
            try:
                sucursal = ap.elegir_sucursal(conn, aprov, cuerpo.sucursal_id.strip() or None)
                _publicar(conn)
                alta = ap.registrar(conn, aprov, nuevo_node_id(sucursal.id), sucursal.id, salida=lambda _: None)
            except ap.ErrorDeAprovisionamiento as exc:
                raise _como_http(exc) from None
        return _responder_carga(alta, url, emisor)

    @router.post("/{node_id}/carga")
    def emitir_carga_nueva(node_id: str, request: Request):
        url, emisor = _preparar(request)
        with abrir_conexion() as conn:
            fila = _nodo_activo(conn, node_id)
            try:
                _publicar(conn)
                # El archivo reemplaza a un código pendiente: si quedaran los dos, el código canjeado después
                # invalidaría el secreto de este archivo.
                vinc.anular_pendientes(conn, node_id, ahora=reloj())
                alta = ap.registrar(conn, aprov, node_id, str(fila[0]), salida=lambda _: None)
            except ap.ErrorDeAprovisionamiento as exc:
                raise _como_http(exc) from None
        return _responder_carga(alta, url, emisor)

    @router.delete("/{node_id}")
    def dar_de_baja(node_id: str):
        with abrir_conexion() as conn:
            fila = conn.execute("SELECT active FROM node_identity WHERE node_id = ?", (node_id,)).fetchone()
            if fila is None:
                raise _error(404, "nodo_inexistente", f"No hay ningún nodo {node_id!r}.")
            ya_estaba = not fila[0]
            # Un código pendiente de un nodo dado de baja no puede volver a darle un secreto.
            vinc.anular_pendientes(conn, node_id, ahora=reloj())
            ap.dar_de_baja(conn, node_id, salida=lambda _: None)
        return {"node_id": node_id, "ya_estaba": ya_estaba}

    return router


def _status_de_registro(exc: Exception) -> tuple[int, str]:
    """El status y el ``codigo`` del canje cuando ``registrar`` se niega."""
    if isinstance(exc, ap.AltaRechazada):
        return 403, "alta_rechazada"
    if isinstance(exc, ap.RegistroConProblemas):
        return 409, "registro_con_problemas"
    return 422, "no_se_puede"


def _error_http(HTTPException):
    def _error(status: int, codigo: str, mensaje: str, headers: dict | None = None):
        return HTTPException(status, detail={"codigo": codigo, "mensaje": mensaje}, headers=headers)
    return _error


def _carga_de(alta: ap.Alta, url: str, emisor: dict | None, producto: str, _error) -> dict:
    """La carga del nodo recién registrado: lo mismo para el archivo y para el canje."""
    entorno_del_nodo = dict(linea.split("=", 1) for linea in alta.entorno_del_nodo())
    try:
        return armar_carga(producto, entorno_del_nodo, url, emisor,
                           {"id": str(alta.sucursal.id), "nombre": alta.sucursal.nombre or ""})
    except ErrorDeInstalacion as exc:
        raise _error(500, "carga_no_armada",
                     f"El nodo {alta.node_id!r} quedó registrado pero no se pudo armar la carga ({exc}). "
                     "Pedir otro código (o emitir una carga nueva) para ese nodo.") from None


# ── el canje: la ruta pública ───────────────────────────────────────────────


def ip_del_par(request) -> str:
    """La IP del par TCP. El producto pasa la suya (``libraauth.auth_events.ip_del_request``, que lee
    ``X-Forwarded-For`` detrás del proxy de confianza); ésta sólo sirve sin proxy."""
    return (request.client.host if request.client else "")[:64] or "desconocida"


def es_https(request) -> bool:
    """¿Llegó por https? Directo (``request.url.scheme``) o detrás del proxy que termina TLS (``X-Forwarded-Proto``)."""
    if request.url.scheme == "https":
        return True
    reenviado = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return reenviado == "https"


def create_canje_router(aprov: ap.Aprovisionamiento, abrir_conexion: Callable[[], Any], *,
                        producto: str | None = None, ruta: str = RUTA_DE_CANJE,
                        ruta_config: Callable[[], str] | None = None, entorno: Any = None,
                        ip_del_request: Callable[[Any], str] = ip_del_par, exigir_https: bool = True,
                        maximo_por_ip: int = vinc.MAXIMO_POR_IP, maximo_global: int = vinc.MAXIMO_GLOBAL,
                        ahora: Callable[[], datetime] | None = None):
    """El canje del código de vinculación (ADR-014). **El producto lo monta SIN guarda**: lo llama el instalador.

    ``POST <ruta>`` con ``{"codigo": "K7Q4-M2XP", "producto": "contalibra"}``:

    1. sólo https (403 ``solo_https``); el producto del instalador tiene que ser el de esta instancia (422
       ``otro_producto``, sin mirar el código);
    2. el límite de intentos (429 ``demasiados_intentos`` con ``Retry-After``), **antes** de mirar el código;
    3. el código se marca usado en una sola sentencia (dos canjes simultáneos: uno solo gana) y en la misma
       transacción el nodo se vuelve a registrar: sale un secreto nuevo, el anterior deja de verificar;
    4. devuelve la carga (la misma que el archivo) con ``Cache-Control: no-store``.

    Un código que no sirve —no existe, venció, ya se usó o se anuló— es **un solo** 404 ``codigo_invalido`` y suma un
    intento fallido. Lo demás, como ``create_nodos_router``. ``ip_del_request(request)`` es la costura para leer la IP
    real detrás del proxy (``libraauth.auth_events.ip_del_request``).
    """
    try:
        from fastapi import APIRouter, HTTPException, Request
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - el central siempre tiene FastAPI
        raise RuntimeError("FastAPI es opcional; instalar libraedge[server]") from exc

    producto = (producto or aprov.prefijo).strip().lower()
    leer_ruta_config = ruta_config or (lambda: ruta_config_del_central(entorno))
    reloj = ahora or (lambda: datetime.now(UTC))
    _error = _error_http(HTTPException)

    class Canje(BaseModel):
        codigo: str
        producto: str | None = None

    router = APIRouter(tags=["libraedge"])

    @router.post(ruta)
    def canjear(cuerpo: Canje, request: Request):
        ip = ip_del_request(request) or "desconocida"
        if exigir_https and not es_https(request):
            logger.warning("Canje de código rechazado: llegó sin https (ip %s).", ip)
            raise _error(403, "solo_https", "El canje del código sólo se acepta por https.")
        pedido = (cuerpo.producto or "").strip().lower()
        if pedido and pedido != producto:
            raise _error(422, "otro_producto",
                         f"Esta dirección es de un sistema de {producto} y el instalador es de {pedido}: usar la "
                         "dirección del sistema del producto que se instala.")
        # Lo que se valida ANTES de gastar el código: sin URL pública o con el CUIT roto no se puede armar la carga.
        try:
            url = resolver_url_del_central(aprov.prefijo, request.headers, entorno)
        except ErrorDeInstalacion as exc:
            raise _error(503, "sin_url_del_central", str(exc)) from None
        emisor, _, error_emisor = _emisor_y_faltantes(leer_ruta_config())
        if error_emisor:
            raise _error(503, "emisor_invalido", f"El config.json del sistema: {error_emisor}")

        momento = reloj()
        with abrir_conexion() as conn:
            try:
                vinc.verificar_limite(conn, ip, ahora=momento, maximo_por_ip=maximo_por_ip,
                                      maximo_global=maximo_global)
            except vinc.DemasiadosIntentos as exc:
                raise _error(429, "demasiados_intentos",
                             "Demasiados intentos con códigos que no sirven. Esperá unos minutos y probá de nuevo; "
                             "si sigue, usá el archivo de carga.",
                             headers={"Retry-After": str(exc.reintentar_en)}) from None
            node_id = vinc.canjear(conn, cuerpo.codigo, ahora=momento)
            if node_id is None:
                vinc.anotar_fallo(conn, ip, ahora=momento)
                conn.commit()
                logger.warning("Canje de código fallido (ip %s).", ip)
                raise _error(404, "codigo_invalido", vinc.MENSAJE_CODIGO_INVALIDO)
            fila = conn.execute("SELECT branch_id, active FROM node_identity WHERE node_id = ?",
                                (node_id,)).fetchone()
            if fila is None or not fila[1]:
                # La baja anula los códigos: esto es una carrera con ella. El código no se gasta.
                conn.rollback()
                logger.warning("Canje de código de un nodo dado de baja (%s, ip %s).", node_id, ip)
                raise _error(404, "codigo_invalido", vinc.MENSAJE_CODIGO_INVALIDO)
            try:
                # Registra de nuevo (secreto nuevo) y hace el commit: el código queda usado junto con el secreto.
                alta = ap.registrar(conn, aprov, node_id, str(fila[0]), salida=lambda _: None)
            except ap.ErrorDeAprovisionamiento as exc:
                conn.rollback()
                status, codigo = _status_de_registro(exc)
                raise _error(status, codigo, str(exc)) from None
        carga = _carga_de(alta, url, emisor, producto, _error)
        logger.info("Código de vinculación canjeado: nodo %s (ip %s).", node_id, ip)
        return JSONResponse(carga, headers=CABECERAS_SIN_CACHE)

    return router
