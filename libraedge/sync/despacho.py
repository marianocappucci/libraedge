"""El despacho del central: qué manejador aplica cada operación que sube.

Hasta v0.6 el router recibía **un** manejador y **una** versión de contrato, y
cada manejador del producto filtraba con ``if operation_type != ...: return``.
Ese ``return`` era un agujero: una operación de un tipo que el central no
conocía volvía del manejador sin error, el receptor la anotaba ``applied`` y el
nodo la confirmaba. **Se perdía en silencio**, que es la peor forma de perder
una venta.

Con el despacho, el producto registra un manejador por cada par
``(operation_type, schema_version)``, y lo que no tiene manejador **no se
aplica**: va a revisión manual con el motivo, y el nodo recibe ``rejected``.
Varias versiones de un mismo tipo conviven --un nodo sin actualizar sigue
mandando ``v1`` mientras los nuevos mandan ``v2``--, cada una con su manejador.

La firma de un manejador es ``(conexion, operacion)``, la misma que ya usaban
los de la familia (ver ``create_sync_router``).
"""

from collections.abc import Callable

from libraedge.domain.sync import OutboxOperation

Manejador = Callable[[object, OutboxOperation], None]


class SinManejador(LookupError):
    """No hay manejador para ``(operation_type, schema_version)``.

    No hereda de ``ValueError`` a propósito: el receptor la trata aparte para
    que el motivo que ve el operador diga *qué* falta, no un error genérico.
    """


class Despachador:
    """El registro de manejadores del central, por tipo y versión de contrato.

    Uso::

        despachador = Despachador()
        despachador.registrar("venta.cobrada", 1, aplicar_venta_v1)

        @despachador.manejador("venta.cobrada", 2)
        def aplicar_venta_v2(conexion, operacion): ...

        router = create_sync_router(get_connection, despachador=despachador)
    """

    def __init__(self):
        self._manejadores: dict[tuple[str, int], Manejador] = {}

    def registrar(self, operation_type: str, schema_version: int,
                  manejador: Manejador) -> Manejador:
        """Registra ``manejador``. Registrar dos veces el mismo par es un error.

        Pisar en silencio el manejador de un par dejaría al primero muerto sin
        que nadie se entere; mejor que el producto falle al arrancar.
        """
        if not operation_type:
            raise ValueError("operation_type vacío")
        clave = (operation_type, int(schema_version))
        if clave in self._manejadores:
            raise ValueError(
                f"ya hay un manejador para {operation_type} v{schema_version}")
        self._manejadores[clave] = manejador
        return manejador

    def manejador(self, operation_type: str, schema_version: int):
        """La misma registración, como decorador."""
        def decorar(funcion: Manejador) -> Manejador:
            return self.registrar(operation_type, schema_version, funcion)
        return decorar

    def resolver(self, operacion: OutboxOperation) -> Manejador | None:
        return self._manejadores.get((operacion.operation_type, operacion.schema_version))

    def versiones(self, operation_type: str) -> tuple[int, ...]:
        """Las versiones de ``operation_type`` que este central sabe aplicar."""
        return tuple(sorted(v for t, v in self._manejadores if t == operation_type))

    def tipos(self) -> tuple[str, ...]:
        return tuple(sorted({t for t, _ in self._manejadores}))

    def motivo_sin_manejador(self, operacion: OutboxOperation) -> str:
        """El texto que ve quien revisa: qué llegó y qué conoce el central."""
        conocidas = self.versiones(operacion.operation_type)
        if conocidas:
            lista = ", ".join(f"v{v}" for v in conocidas)
            return (f"sin manejador para {operacion.operation_type} "
                    f"v{operacion.schema_version}: el central conoce {lista}")
        return (f"sin manejador para {operacion.operation_type} "
                f"v{operacion.schema_version}: el central no conoce ese tipo")

    def __call__(self, conexion, operacion: OutboxOperation) -> None:
        manejador = self.resolver(operacion)
        if manejador is None:
            raise SinManejador(self.motivo_sin_manejador(operacion))
        manejador(conexion, operacion)

    def __len__(self) -> int:
        return len(self._manejadores)
