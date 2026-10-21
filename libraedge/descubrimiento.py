"""Los otros puestos del local encuentran la PC principal (ADR-017).

La PC principal (la del nodo) contesta en la red del local un pedido de descubrimiento por **UDP en el puerto
47823**: la app de un puesto secundario (``escritorio/src/descubrimiento.rs``) manda el pedido en broadcast y cada
nodo **de su producto** le contesta con datos que **no son secretos**: el producto, su nombre, el puerto donde atiende,
el identificador del nodo, la sucursal y el nombre del negocio. La IP no viaja en la respuesta: el puesto usa la
direccion de quien le contesto.

🔴 **Solo biblioteca estandar, y sin importar nada de ``libraedge``**: como ``instalador.py``, los instaladores copian
este archivo tal cual (``libraedge_descubrimiento.py``) y lo corren con el Python del nodo, que trae la version de
LibraEdge pineada por el producto. Asi el que contesta llega con el instalador, sin esperar a que los productos suban
el pin.

El servicio no ve ningun secreto: lee **solo** ``descubrimiento.json`` (lo escribe el ayudante del instalador al
configurar, sin secretos y legible por todos) y corre como un usuario sin privilegios (``libraedge`` en Ubuntu,
``LocalService`` en Windows). Para que no sirva para amplificar un ataque (un pedido chico con la IP de otro que
provoca una respuesta grande), **un pedido mas corto que :data:`TAMANO_DEL_PEDIDO` no se contesta** y la respuesta
nunca pasa de ese tamano; ademas solo se contesta a IPv4 privadas o de enlace local y hay un tope de respuestas por
origen y en total.

Uso::

    python libraedge_descubrimiento.py responder --datos /var/lib/libraedge-nodo/descubrimiento.json
    python libraedge_descubrimiento.py buscar --producto ventalibra [--a 192.168.0.10] [--espera 2]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: El puerto fijo del descubrimiento (UDP). La app lo tiene en ``descubrimiento.rs`` (un test compara los dos).
PUERTO = 47823
#: Lo que dice un pedido y una respuesta (version del protocolo).
PEDIDO = "descubrir/1"
RESPUESTA = "nodo/1"
#: Un pedido trae relleno hasta este tamano; uno mas corto no se contesta, y ninguna respuesta lo supera.
TAMANO_DEL_PEDIDO = 512
#: Un datagrama mas grande que esto no es un pedido.
TAMANO_MAXIMO = 2048
FORMATO_DATOS = "libraedge-descubrimiento/1"
#: Respuestas por origen en la ventana, y en total por segundo.
POR_ORIGEN = 5
VENTANA_POR_ORIGEN = 10.0
POR_SEGUNDO = 50

_CAMPOS_DE_TEXTO = ("producto", "nombre", "nodo", "sucursal", "negocio", "version")


def origen_permitido(ip: str) -> bool:
    """A quien se le contesta: IPv4 privada (10/8, 172.16/12, 192.168/16), de enlace local o loopback."""
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(direccion, ipaddress.IPv6Address):
        if direccion.ipv4_mapped is None:
            return False
        direccion = direccion.ipv4_mapped
    if direccion.is_unspecified or direccion.is_multicast or str(direccion) == "255.255.255.255":
        return False
    return direccion.is_loopback or direccion.is_link_local or (direccion.is_private and not direccion.is_reserved)


def armar_pedido(producto: str) -> bytes:
    """El pedido de un puesto, con relleno hasta :data:`TAMANO_DEL_PEDIDO` (JSON admite espacios al final)."""
    cuerpo = json.dumps({"libraedge": PEDIDO, "producto": producto}).encode()
    return cuerpo + b" " * max(0, TAMANO_DEL_PEDIDO - len(cuerpo))


def leer_pedido(datagrama: bytes) -> str | None:
    """El producto que busca un pedido valido, o ``None`` (corto, largo, roto o de otro protocolo)."""
    if not TAMANO_DEL_PEDIDO <= len(datagrama) <= TAMANO_MAXIMO:
        return None
    try:
        datos = json.loads(datagrama.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(datos, dict) or datos.get("libraedge") != PEDIDO:
        return None
    producto = datos.get("producto")
    return producto if isinstance(producto, str) and producto else None


def leer_datos(ruta: str) -> dict | None:
    """Lo que contesta este nodo (``descubrimiento.json``), validado, o ``None`` si no esta o no sirve."""
    try:
        with open(ruta, encoding="utf-8-sig") as archivo:
            datos = json.load(archivo)
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict) or datos.get("formato") != FORMATO_DATOS:
        return None
    puerto = datos.get("puerto")
    if not isinstance(puerto, int) or isinstance(puerto, bool) or not 1 <= puerto <= 65535:
        return None
    if not isinstance(datos.get("producto"), str) or not datos["producto"]:
        return None
    return {"puerto": puerto, **{c: str(datos[c]) for c in _CAMPOS_DE_TEXTO if isinstance(datos.get(c), str)}}


def armar_respuesta(datos: dict) -> bytes:
    """La respuesta, que nunca pasa de :data:`TAMANO_DEL_PEDIDO`: si no entra, se acortan negocio y sucursal."""
    cuerpo = {"libraedge": RESPUESTA, **datos}
    for largo in (None, 120, 60, 20, 0):
        if largo is not None:
            for campo in ("negocio", "sucursal"):
                if campo in cuerpo:
                    cuerpo[campo] = cuerpo[campo][:largo]
        salida = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        if len(salida) <= TAMANO_DEL_PEDIDO:
            return salida
    # Lo fijo (producto, nombre, nodo) ya viene acotado por el instalador; por las dudas, sin los de texto libre.
    return json.dumps({"libraedge": RESPUESTA, "producto": datos["producto"], "puerto": datos["puerto"]}).encode()


def leer_respuesta(datagrama: bytes) -> dict | None:
    """Una respuesta valida, o ``None``. Lo usa :func:`buscar` (la app tiene la suya en Rust)."""
    try:
        datos = json.loads(datagrama.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(datos, dict) or datos.get("libraedge") != RESPUESTA:
        return None
    puerto = datos.get("puerto")
    if not isinstance(puerto, int) or isinstance(puerto, bool) or not 1 <= puerto <= 65535:
        return None
    return datos


@dataclass
class Limitador:
    """Cuantas respuestas se dan: :data:`POR_ORIGEN` por origen en :data:`VENTANA_POR_ORIGEN` segundos y
    :data:`POR_SEGUNDO` en total. Un pedido que no entra se ignora (no se contesta ni se anota)."""

    reloj: Callable[[], float] = time.monotonic
    _por_origen: dict[str, list[float]] = field(default_factory=dict)
    _ultimo_segundo: list[float] = field(default_factory=list)

    def permitir(self, origen: str) -> bool:
        ahora = self.reloj()
        self._ultimo_segundo = [t for t in self._ultimo_segundo if ahora - t < 1.0]
        if len(self._ultimo_segundo) >= POR_SEGUNDO:
            return False
        recientes = [t for t in self._por_origen.get(origen, []) if ahora - t < VENTANA_POR_ORIGEN]
        if len(recientes) >= POR_ORIGEN:
            self._por_origen[origen] = recientes
            return False
        recientes.append(ahora)
        self._por_origen[origen] = recientes
        self._ultimo_segundo.append(ahora)
        if len(self._por_origen) > 1024:  # que la memoria no crezca con origenes viejos
            self._por_origen = {o: ts for o, ts in self._por_origen.items() if ahora - ts[-1] < VENTANA_POR_ORIGEN}
        return True


def contestar(datagrama: bytes, origen: str, datos: dict | None, limitador: Limitador) -> bytes | None:
    """Que contestarle a un datagrama (o ``None``): un pedido valido de este producto, desde la red del local."""
    if datos is None or not origen_permitido(origen):
        return None
    if leer_pedido(datagrama) != datos["producto"]:
        return None
    if not limitador.permitir(origen):
        return None
    return armar_respuesta(datos)


class Datos:
    """``descubrimiento.json``, releido cuando cambia (vincular la PC otra vez cambia el nodo y la sucursal)."""

    def __init__(self, ruta: str):
        self.ruta = ruta
        self._marca: tuple[int, int] | None = None
        self._datos: dict | None = None

    def actuales(self) -> dict | None:
        try:
            estado = os.stat(self.ruta)
            marca = (estado.st_mtime_ns, estado.st_size)
        except OSError:
            self._marca, self._datos = None, None
            return None
        if marca != self._marca:
            self._marca, self._datos = marca, leer_datos(self.ruta)
        return self._datos


def abrir_socket(puerto: int = PUERTO, direccion: str = "0.0.0.0") -> socket.socket:
    """El socket del que contesta. En Windows con uso exclusivo: sin eso otro proceso podria tomar el mismo puerto
    (``SO_REUSEADDR`` alla deja robarlo)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    exclusivo = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusivo is not None:  # pragma: no cover - solo Windows
        sock.setsockopt(socket.SOL_SOCKET, exclusivo, 1)
    sock.bind((direccion, puerto))
    return sock


def responder(sock: socket.socket, datos: Datos, *, limitador: Limitador | None = None,
              seguir: Callable[[], bool] = lambda: True) -> int:
    """Contesta hasta que ``seguir()`` diga que no. Devuelve cuantas respuestas dio. No anota cada pedido (seria
    ruido y una forma de llenar el disco desde la red)."""
    limitador = limitador or Limitador()
    dadas = 0
    sock.settimeout(1.0)
    while seguir():
        try:
            datagrama, (origen, puerto) = sock.recvfrom(TAMANO_MAXIMO + 1)[:2]
        except TimeoutError:
            continue
        except OSError:
            # En Windows, un ICMP «puerto inalcanzable» de una respuesta anterior llega como error del recvfrom
            # (WSAECONNRESET): no es un problema de este socket.
            continue
        respuesta = contestar(datagrama, origen, datos.actuales(), limitador)
        if respuesta is None:
            continue
        try:
            sock.sendto(respuesta, (origen, puerto))
            dadas += 1
        except OSError:
            continue
    return dadas


def _broadcasts(ip_local: str | None) -> list[str]:
    destinos = ["255.255.255.255"]
    if ip_local:
        # El broadcast de la red /24 de esta PC: en Windows el 255.255.255.255 sale por una sola interfaz.
        destinos.append(str(ipaddress.ip_network(f"{ip_local}/24", strict=False).broadcast_address))
    return destinos


def _ip_local() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # no manda nada: elige la interfaz de la ruta por defecto
            return sock.getsockname()[0]
    except OSError:
        return None


def buscar(producto: str, *, destinos: Iterable[str] | None = None, puerto: int = PUERTO,
           espera: float = 2.0) -> list[dict]:
    """Los nodos de ``producto`` que contestan, con la IP de quien contesto (``ip``), sin repetir el nodo.

    Sin ``destinos``, por broadcast. Es la misma busqueda que hace la app; esta la usan el CI y soporte."""
    destinos = list(destinos) if destinos is not None else _broadcasts(_ip_local())
    encontrados: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        pedido = armar_pedido(producto)
        for destino in destinos:
            try:
                sock.sendto(pedido, (destino, puerto))
            except OSError:
                continue
        limite = time.monotonic() + espera
        while (resto := limite - time.monotonic()) > 0:
            sock.settimeout(resto)
            try:
                datagrama, (ip, _p) = sock.recvfrom(TAMANO_MAXIMO)[:2]
            except (TimeoutError, OSError):
                break
            respuesta = leer_respuesta(datagrama)
            if respuesta is None or respuesta.get("producto") != producto or not origen_permitido(ip):
                continue
            clave = respuesta.get("nodo") or f"{ip}:{respuesta['puerto']}"
            encontrados.setdefault(clave, {**respuesta, "ip": ip})
    return list(encontrados.values())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libraedge_descubrimiento", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="comando", required=True)
    resp = sub.add_parser("responder", help="contesta los pedidos de los puestos (el servicio del nodo)")
    resp.add_argument("--datos", required=True, help="descubrimiento.json (sin secretos)")
    resp.add_argument("--puerto", type=int, default=PUERTO)
    busca = sub.add_parser("buscar", help="busca los nodos de un producto en la red del local")
    busca.add_argument("--producto", required=True)
    busca.add_argument("--a", action="append", dest="destinos", help="una IP en vez del broadcast (se repite)")
    busca.add_argument("--puerto", type=int, default=PUERTO)
    busca.add_argument("--espera", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.comando == "buscar":
        encontrados = buscar(args.producto, destinos=args.destinos, puerto=args.puerto, espera=args.espera)
        for nodo in encontrados:
            print(json.dumps(nodo, ensure_ascii=False))
        return 0 if encontrados else 1
    sock = abrir_socket(args.puerto)  # pragma: no cover - el servicio; se prueba con responder() y en el CI
    print(f"Contestando en UDP {args.puerto} con {args.datos}.", flush=True)  # pragma: no cover
    responder(sock, Datos(args.datos))  # pragma: no cover
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
