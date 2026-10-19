"""La identidad del nodo: ``LIBRAEDGE_NODE_ID`` y ``nodo_actual()``.

Es **la identidad, no el opt-in comercial**: dice "esta instancia es el nodo X"
y la escribe el instalador en la PC del local. Lo que se contrata es el
adicional ``offline`` por sucursal (libracore ADR-048), y eso se hace cumplir
del lado del central al registrar el nodo (``libraedge.aprovisionamiento``).

Hasta v0.8 cada producto leia su propia variable (``RESTOLIBRA_EDGE_NODE_ID``)
y el proceso del nodo otra (``LIBRAEDGE_NODE_ID``), con el mismo valor escrito
dos veces por el instalador. Ahora la general es ``LIBRAEDGE_NODE_ID`` y la del
producto (``<PREFIJO>_EDGE_NODE_ID``) se sigue leyendo por compatibilidad: una
instalacion vieja que solo tiene la del producto no deja de ser nodo.

🔴 Si estan las dos y dicen cosas distintas es un error, no una preferencia. El
producto encolaria operaciones con un ``node_id`` y el proceso del nodo se
autenticaria con otro: el central rechazaria la subida y nadie veria por que.
"""

import os
from collections.abc import Mapping

VARIABLE = "LIBRAEDGE_NODE_ID"
#: El prefijo de numeracion del nodo (``S02``), que asigna el central al
#: registrarlo. Va con ``nodo_desde_entorno(VARIABLE,
#: VARIABLE_PREFIJO)`` de LibraCommerce (`erp.offline`).
VARIABLE_PREFIJO = "LIBRAEDGE_PREFIJO"

_VALIDO = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class IdentidadEnConflicto(ValueError):
    """``LIBRAEDGE_NODE_ID`` y ``<PREFIJO>_EDGE_NODE_ID`` estan las dos y difieren."""


def variable_del_producto(prefijo: str) -> str:
    """``"restolibra"`` -> ``"RESTOLIBRA_EDGE_NODE_ID"``."""
    prefijo = (prefijo or "").strip().upper()
    if not prefijo or not set(prefijo) <= _VALIDO or prefijo[0].isdigit():
        raise ValueError(f"prefijo de variables no valido: {prefijo!r}")
    return f"{prefijo}_EDGE_NODE_ID"


def nodo_actual(prefijo: str | None = None, entorno: Mapping[str, str] | None = None) -> str | None:
    """El id de este nodo, o ``None`` si esta instancia no es un nodo offline.

    Lee ``LIBRAEDGE_NODE_ID`` y, si se pasa ``prefijo``, tambien
    ``<PREFIJO>_EDGE_NODE_ID``. Un valor vacio o de puros espacios cuenta como
    ausente. Levanta ``IdentidadEnConflicto`` si las dos estan y difieren.
    """
    entorno = os.environ if entorno is None else entorno
    general = (entorno.get(VARIABLE) or "").strip() or None
    if prefijo is None:
        return general
    nombre = variable_del_producto(prefijo)
    propia = (entorno.get(nombre) or "").strip() or None
    if general and propia and general != propia:
        raise IdentidadEnConflicto(
            f"{VARIABLE}={general!r} y {nombre}={propia!r} no coinciden. El producto "
            f"encolaria con un id y el nodo se autenticaria con otro: dejar una sola, "
            f"o las dos con el mismo valor.")
    return general or propia


def prefijo_actual(entorno: Mapping[str, str] | None = None) -> str | None:
    """El prefijo de numeracion de este nodo (``LIBRAEDGE_PREFIJO``), o ``None``."""
    entorno = os.environ if entorno is None else entorno
    return (entorno.get(VARIABLE_PREFIJO) or "").strip() or None
