"""La declaracion de las altas en el nodo y su variable de la carga (`LIBRAEDGE_ALTAS_EN_NODO`, v0.18, ADR-021)."""

import pytest

from libraedge.altas import (
    AltaEnNodo,
    Referencia,
    formatear_altas,
    normalizar_altas,
    parsear_altas,
)

LIBRO = Referencia("cc_asientos", "tercero_id", (("rol", "cliente"),))


def test_se_acepta_un_nombre_una_alta_o_un_par_con_referencias():
    altas = normalizar_altas([
        "parties",
        AltaEnNodo("clients", (LIBRO,)),
    ])
    assert altas == (AltaEnNodo("clients", (LIBRO,)), AltaEnNodo("parties"))
    assert normalizar_altas({"clients": [("cc_asientos", "tercero_id", {"rol": "cliente"})]}) == (
        AltaEnNodo("clients", (LIBRO,)),)
    assert normalizar_altas([("clients", [("remitos", "client_id")])]) == (
        AltaEnNodo("clients", (Referencia("remitos", "client_id"),)),)
    assert normalizar_altas("parties") == (AltaEnNodo("parties"),)
    assert normalizar_altas(None) == () and normalizar_altas(()) == ()


def test_una_tabla_repetida_o_un_identificador_malo_no_se_aceptan():
    with pytest.raises(ValueError, match="dos veces"):
        normalizar_altas(["clients", AltaEnNodo("clients")])
    for malo in ("clients; x", "1clients", ""):
        with pytest.raises(ValueError, match="identificador"):
            normalizar_altas([malo])
    with pytest.raises(ValueError, match="identificador"):
        Referencia("cc_asientos", "tercero id")
    with pytest.raises(ValueError, match="valor no valido"):
        Referencia("cc_asientos", "tercero_id", (("rol", "x' OR '1'='1"),))


def test_formato_de_la_variable():
    altas = [AltaEnNodo("clients", (LIBRO, Referencia("otra", "cliente_id"))), AltaEnNodo("parties")]
    assert formatear_altas(altas) == "clients:cc_asientos.tercero_id@rol=cliente+otra.cliente_id,parties"
    assert formatear_altas(()) == ""


def test_la_variable_se_lee_igual_que_se_escribe():
    altas = normalizar_altas([AltaEnNodo("clients", (LIBRO, Referencia("otra", "cliente_id"))), "parties"])
    assert parsear_altas(formatear_altas(altas)) == altas


def test_varias_condiciones_en_una_referencia():
    referencia = Referencia("asientos", "tercero_id", (("rol", "cliente"), ("origen", "caja")))
    texto = formatear_altas([AltaEnNodo("clients", (referencia,))])
    assert texto == "clients:asientos.tercero_id@rol=cliente@origen=caja"
    assert parsear_altas(texto) == (AltaEnNodo("clients", (referencia,)),)


def test_vacia_o_ausente_es_ninguna():
    assert parsear_altas(None) == () and parsear_altas("") == () and parsear_altas(" , ") == ()


@pytest.mark.parametrize("crudo", [
    "clients:cc_asientos",                  # falta la columna
    "clients:cc_asientos.tercero_id@rol",   # falta el valor de la condicion
    "clients:cc_asientos.tercero_id@rol=a b",
    "cli ents",
    "clients:x.y;z",
])
def test_una_entrada_que_no_se_entiende_es_un_error(crudo):
    with pytest.raises(ValueError):
        parsear_altas(crudo)
