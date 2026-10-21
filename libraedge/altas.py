"""Las tablas donde el nodo puede dar de alta una fila, y a quien apunta esa fila sin que la base lo sepa (ADR-021).

Una tabla **con alta en el nodo** es una tabla de referencia que el nodo espeja y en la que ademas crea filas con un id
provisorio negativo (`libraedge.mapeo`). Cuando el central le asigna su id, el nodo reapunta lo que referenciaba al
provisorio (`libraedge.sync.reconciliacion`). Lo que lo referencia **con una clave foranea** se encuentra solo, en el
catalogo de la base. Lo que lo referencia **sin clave foranea** (el `tercero_id` de un libro de cuentas, por ejemplo,
que sirve a varias tablas y por eso no tiene FK) hay que declararlo: es una `Referencia`.

```python
Aprovisionamiento(
    ..., tablas_de_referencia=[..., "clients", "parties"],
    altas_en_nodo=[
        AltaEnNodo("clients", referencias=[Referencia("cc_asientos", "tercero_id", {"rol": "cliente"})]),
        "parties",
    ],
)
```

## Como viaja al nodo

Por la carga, como `LIBRAEDGE_TABLAS_SOLO_LECTURA` (ADR-019): la variable opcional **`LIBRAEDGE_ALTAS_EN_NODO`**.
Tablas separadas por coma; una tabla puede llevar sus referencias despues de `:`, separadas por `+`; cada una es
`tabla.columna`, con condiciones opcionales `@columna=valor` (igualdades):

    clients:cc_asientos.tercero_id@rol=cliente,parties

Los valores de las condiciones son identificadores simples (`[A-Za-z0-9_-]`): la variable termina en un archivo de
entorno y en SQL, y nada de lo que trae se interpola sin validar.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from libraedge.db.changelog import validar_identificador

_VALOR = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Referencia:
    """Una columna que guarda el id de otra tabla **sin** clave foranea: `tabla.columna`, y las filas que cuentan
    (`donde`: igualdades `columna = valor`; vacio = todas)."""

    tabla: str
    columna: str
    donde: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        validar_identificador(self.tabla)
        validar_identificador(self.columna)
        for columna, valor in self.donde:
            validar_identificador(columna)
            if not _VALOR.match(str(valor)):
                raise ValueError(f"valor no valido en la condicion de {self.tabla}.{self.columna}: {valor!r}")


@dataclass(frozen=True)
class AltaEnNodo:
    """Una tabla con alta en el nodo y las referencias sin FK que apuntan a sus filas."""

    tabla: str
    referencias: tuple[Referencia, ...] = ()

    def __post_init__(self):
        validar_identificador(self.tabla)


def _referencia(item) -> Referencia:
    if isinstance(item, Referencia):
        return item
    tabla, columna, *resto = item
    donde = resto[0] if resto else None
    pares = tuple(sorted((str(k), str(v)) for k, v in (donde or {}).items())) if isinstance(donde, Mapping) \
        else tuple(donde or ())
    return Referencia(tabla, columna, pares)


def normalizar_altas(declaradas) -> tuple[AltaEnNodo, ...]:
    """La declaracion del producto como `AltaEnNodo`, validada, ordenada y sin repetidas.

    Acepta nombres (`"parties"`), `AltaEnNodo`, pares `(tabla, referencias)` y un `{tabla: referencias}`. Cada
    referencia es una `Referencia`, `(tabla, columna)` o `(tabla, columna, {"rol": "cliente"})`."""
    if declaradas is None:
        return ()
    if isinstance(declaradas, str):
        declaradas = [declaradas]
    if isinstance(declaradas, Mapping):
        declaradas = list(declaradas.items())
    altas: dict[str, AltaEnNodo] = {}
    for item in declaradas:
        if isinstance(item, AltaEnNodo):
            alta = item
        elif isinstance(item, str):
            alta = AltaEnNodo(item)
        else:
            tabla, referencias = item
            alta = AltaEnNodo(tabla, tuple(_referencia(r) for r in (referencias or ())))
        if alta.tabla in altas:
            raise ValueError(f"la tabla {alta.tabla!r} esta dos veces en las altas en el nodo")
        altas[alta.tabla] = alta
    return tuple(altas[t] for t in sorted(altas))


def formatear_altas(altas) -> str:
    """El valor de `LIBRAEDGE_ALTAS_EN_NODO` (vacio si no hay altas)."""
    partes = []
    for alta in normalizar_altas(altas):
        referencias = "+".join(
            f"{r.tabla}.{r.columna}" + "".join(f"@{c}={v}" for c, v in r.donde) for r in alta.referencias)
        partes.append(f"{alta.tabla}:{referencias}" if referencias else alta.tabla)
    return ",".join(partes)


def parsear_altas(crudo: str | None) -> tuple[AltaEnNodo, ...]:
    """`"clients:cc_asientos.tercero_id@rol=cliente,parties"` -> las `AltaEnNodo`. Vacio o ausente: ninguna.
    Una entrada que no se entiende es `ValueError` (el nodo no arranca a medias)."""
    altas = []
    for parte in (crudo or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        tabla, _, resto = parte.partition(":")
        referencias = []
        for texto in (r for r in resto.split("+") if r.strip()):
            cabeza, *condiciones = texto.strip().split("@")
            tabla_ref, punto, columna = cabeza.partition(".")
            if not punto:
                raise ValueError(f"referencia no valida en LIBRAEDGE_ALTAS_EN_NODO: {texto!r} (tabla.columna)")
            donde = []
            for condicion in condiciones:
                columna_cond, igual, valor = condicion.partition("=")
                if not igual:
                    raise ValueError(f"condicion no valida en LIBRAEDGE_ALTAS_EN_NODO: {condicion!r} (columna=valor)")
                donde.append((columna_cond, valor))
            referencias.append(Referencia(tabla_ref, columna, tuple(donde)))
        altas.append(AltaEnNodo(tabla.strip(), tuple(referencias)))
    return normalizar_altas(altas)
