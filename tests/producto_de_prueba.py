"""La funcion de carga inicial de un producto de juguete (ADR-023).

La importa el nodo por ``LIBRAEDGE_CARGA_INICIAL=tests.producto_de_prueba:aplicar``, como importaria
``libracommerce.erp.offline_stock:aplicar_carga_inicial``. Lo que hace lo decide el test, con ``respuesta``.
"""

#: Cada llamada: ``(conexion, traer)``.
llamadas: list = []

#: Lo que devuelve la proxima llamada, o la excepcion que levanta. Si es un invocable ``(conexion, traer)``, se corre.
respuesta = {"hecha": True}


def reiniciar(nueva=None) -> None:
    llamadas.clear()
    globals()["respuesta"] = {"hecha": True} if nueva is None else nueva


def aplicar(conexion, traer):
    llamadas.append((conexion, traer))
    if isinstance(respuesta, BaseException):
        raise respuesta
    if callable(respuesta):
        return respuesta(conexion, traer)
    return respuesta
