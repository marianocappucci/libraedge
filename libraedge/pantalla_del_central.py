"""«Sucursal sin internet»: el aprovisionamiento del nodo desde la pantalla del central (ADR-011).

Hasta v0.10 dar de alta un nodo era un comando en el VPS (``libraedge-central nodo registrar ... --carga``, ADR-008 y
ADR-010). Esto es **lo mismo**, montado como router dentro del producto para que lo haga el administrador de la
instancia desde su navegador y se lleve las dos cosas que necesita para instalar el nodo en la PC del local:

1. el **instalador** de su producto: un link fijo a los Releases de LibraEdge (:func:`url_del_instalador`), y
2. la **carga del nodo** (``carga-nodo.json``), con el secreto, que se descarga **una sola vez**.

No hay lógica nueva de aprovisionamiento: el router llama a ``libraedge.aprovisionamiento`` (``publicar``,
``registrar``, ``dar_de_baja``) y arma la carga con ``libraedge.instalador.armar_carga``, las mismas funciones que el
CLI. Lo del producto entra por las costuras de siempre: su ``Aprovisionamiento`` (tablas, sucursales, guarda del
adicional ``offline``) y la fábrica de conexiones (la firma de ``libracore.db.core.get_connection``).

Rutas (con ``prefijo="/api/nodos-offline"``)::

    GET    /api/nodos-offline                    el estado: contratado, sucursales, nodos, instalador, central
    GET    /api/nodos-offline/menu               {contratado, nodos_activos, visible}: para el menú, una vez al cargar
    POST   /api/nodos-offline                    {"sucursal_id": "2"} -> alta de un nodo nuevo; devuelve la CARGA
    POST   /api/nodos-offline/{node_id}/carga    la misma PC, carga nueva (invalida la anterior); devuelve la CARGA
    DELETE /api/nodos-offline/{node_id}          baja (PC robada o reemplazada): su secreto deja de verificar

## 🔴 El secreto

- **Sólo viaja en el cuerpo de la respuesta que ES la carga**, como archivo adjunto (``Content-Disposition:
  attachment``) y con ``Cache-Control: no-store``. Nunca en un ``GET``, nunca en un JSON que la pantalla muestre,
  nunca en la URL, en una cabecera ni en el texto de un error.
- **No se guarda en ningún lado del central**: sólo su hash (``NodeRepository.register_node``). No hay «volver a
  bajar la carga»: si se pierde, se emite otra (``/carga``), que invalida la anterior.
- **Nada de esto se loguea.** Este módulo no escribe logs; el access log del servidor registra método y ruta, y el
  secreto no está en ninguno de los dos.

## Permisos

**El router no trae guarda propia**: la pone el producto al montarlo, con la de administrador que ya usa para su
configuración (``include_router(..., dependencies=[Depends(<admin>)])``). Es el mismo criterio que el router de SMTP de
libracore: cada producto tiene su propia forma de decir «admin» y LibraEdge no depende de libracore en runtime.

El adicional ``offline`` (libracore ADR-048) **no** se chequea acá a mano: el alta pasa por
``aprov.verificar_alta``, la misma guarda del CLI, y su rechazo es un 403 con el mensaje del motor. El ``GET`` dice si
el adicional está contratado para que la pantalla lo explique en vez de ofrecer un botón que va a fallar; listar y dar
de baja siguen andando sin él (un nodo instalado tiene ventas sin subir y hay que poder revocarlo igual). Por eso la
entrada del menú se ve con el adicional **o** con nodos activos (``GET .../menu``, decisión del humano 2026-10-19).
"""


import json
import os
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from libraedge import aprovisionamiento as ap
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


def adicional_offline_contratado(conn) -> bool:
    """¿Está prendido el módulo ``offline`` (libracore ADR-048)? Sin la tabla ``modulos``, no."""
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
    """Los nodos registrados, con su sucursal y su estado. **Sin el hash del secreto.**"""
    from libraedge.db.repository import NodeRepository

    nombres = {s.id: s.nombre for s in sucursales}
    altas = {f[0]: f[1] for f in conn.execute("SELECT node_id, installed_at FROM node_identity").fetchall()}
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


def nuevo_node_id(sucursal_id: str) -> str:
    """``nodo-2-a1b2c3``: uno nuevo por alta. 🔴 Una PC nueva es un ``node_id`` nuevo (y un prefijo nuevo, ADR-008)."""
    cuerpo = "".join(c for c in str(sucursal_id).lower() if c.isalnum() and c.isascii())[:20] or "x"
    return f"nodo-{cuerpo}-{secrets.token_hex(3)}"


# ── el router ───────────────────────────────────────────────────────────────


def create_nodos_router(aprov: ap.Aprovisionamiento, abrir_conexion: Callable[[], Any], *,
                        producto: str | None = None, prefijo: str = PREFIJO_DE_RUTA,
                        ruta_config: Callable[[], str] | None = None,
                        contratado: Callable[[Any], bool] = adicional_offline_contratado,
                        umbral_minutos: int = UMBRAL_MINUTOS, entorno: Any = None):
    """El router de «Sucursal sin internet». El producto lo monta **con su guarda de administrador**.

    - ``aprov``: el ``Aprovisionamiento`` del producto (el mismo de ``LIBRAEDGE_APROVISIONAMIENTO``).
    - ``abrir_conexion``: devuelve un context manager de conexión (``libracore.db.core.get_connection``); se abre una
      por pedido, como ``create_sync_router``.
    - ``producto``: el de la carga y el del instalador; por defecto ``aprov.prefijo`` en minúsculas.
    - ``ruta_config``: el ``config.json`` de la instancia (el emisor que viaja en la carga); por defecto
      ``$DATA_DIR/config.json``, como el CLI.
    - ``contratado(conn)``: si el adicional está prendido (sólo informa: el que corta es ``aprov.verificar_alta``).
    - ``entorno``: para los tests; por defecto ``os.environ`` (lo lee la URL del central).
    """
    try:
        from fastapi import APIRouter, HTTPException, Request, Response
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - el central siempre tiene FastAPI
        raise RuntimeError("FastAPI es opcional; instalar libraedge[server]") from exc

    producto = (producto or aprov.prefijo).strip().lower()
    instalador = url_del_instalador(producto)
    leer_ruta_config = ruta_config or (lambda: ruta_config_del_central(entorno))

    class Alta(BaseModel):
        sucursal_id: str

    router = APIRouter(prefix=prefijo, tags=["libraedge"])

    def _error(status: int, codigo: str, mensaje: str) -> HTTPException:
        return HTTPException(status, detail={"codigo": codigo, "mensaje": mensaje})

    def _como_http(exc: Exception) -> HTTPException:
        if isinstance(exc, ap.AltaRechazada):
            return _error(403, "adicional_offline", str(exc))
        if isinstance(exc, ap.SegundoNodoEnLaSucursal):
            return _error(409, "sucursal_con_nodo", str(exc))
        if isinstance(exc, ap.SucursalInvalida):
            return _error(422, "sucursal_invalida", str(exc))
        return _error(422, "no_se_puede", str(exc))

    def _url_del_central(request) -> str:
        return resolver_url_del_central(aprov.prefijo, request.headers, entorno)

    @router.get("/menu")
    def menu():
        """¿Se muestra la entrada del menú? Si el adicional está contratado **o** quedan nodos activos.

        Sin el adicional el alta y la carga nueva siguen bloqueadas (403), pero un nodo que sigue activo se tiene que
        poder dar de baja: la entrada no puede desaparecer con él adentro. Liviana a propósito: el layout la pide una
        vez al cargar (``libra-ui``, ``nodosOffline`` en la entrada del menú), no la pantalla entera.
        """
        with abrir_conexion() as conn:
            esta_contratado = contratado(conn)
            activos = ap.nodos_activos(conn)
        return {"contratado": esta_contratado, "nodos_activos": activos,
                "visible": esta_contratado or activos > 0}

    @router.get("")
    def estado(request: Request):
        with abrir_conexion() as conn:
            sucursales = ap.listar_sucursales(conn, aprov)
            nodos = nodos_de_la_instancia(conn, sucursales, umbral_minutos=umbral_minutos)
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
        entorno_del_nodo = dict(linea.split("=", 1) for linea in alta.entorno_del_nodo())
        try:
            carga = armar_carga(producto, entorno_del_nodo, url, emisor,
                                {"id": str(alta.sucursal.id), "nombre": alta.sucursal.nombre or ""})
        except ErrorDeInstalacion as exc:
            raise _error(500, "carga_no_armada",
                         f"El nodo {alta.node_id!r} quedó registrado pero no se pudo armar la carga ({exc}). "
                         "Emitir una carga nueva para ese nodo.") from None
        cuerpo = json.dumps(carga, ensure_ascii=False, indent=2) + "\n"
        return Response(
            content=cuerpo.encode("utf-8"), media_type="application/json",
            headers={**CABECERAS_SIN_CACHE,
                     "Content-Disposition": f'attachment; filename="carga-nodo-{alta.node_id}.json"',
                     # Lo que la pantalla puede mostrar sin abrir el archivo: nada secreto.
                     "X-Libraedge-Node-Id": alta.node_id, "X-Libraedge-Prefijo": alta.prefijo},
        )

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
            fila = conn.execute("SELECT branch_id, active FROM node_identity WHERE node_id = ?",
                                (node_id,)).fetchone()
            if fila is None:
                raise _error(404, "nodo_inexistente", f"No hay ningún nodo {node_id!r}.")
            if not fila[1]:
                raise _error(409, "nodo_dado_de_baja",
                             f"El nodo {node_id!r} está dado de baja: para otra PC se da de alta un nodo nuevo.")
            try:
                _publicar(conn)
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
            ap.dar_de_baja(conn, node_id, salida=lambda _: None)
        return {"node_id": node_id, "ya_estaba": ya_estaba}

    return router
