"""Lo que la bandeja muestra, segun el estado que dejo el nodo.

Esto es la parte con reglas; el caparazon grafico --que es de Windows y no se
puede probar en este entorno-- solo dibuja lo que estas funciones devuelven.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from libraedge.bandeja import Severidad, resumen_para_la_bandeja
from libraedge.nodo import EstadoNodo

AHORA = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


def estado(**cambios):
    base = {
        "node_id": "node-1", "en_linea": True, "pendientes": 0, "cursor": 10,
        "ultimo_intento": (AHORA - timedelta(seconds=20)).isoformat(),
    }
    base.update(cambios)
    return EstadoNodo(**base)


# ── El caso feliz ────────────────────────────────────────────────────────

def test_al_dia():
    r = resumen_para_la_bandeja(estado(), AHORA)
    assert r.severidad == Severidad.OK
    assert r.titulo == "Al día"


def test_sincronizando_con_cola():
    """Anda, pero todavia queda algo: atencion, no problema."""
    r = resumen_para_la_bandeja(estado(pendientes=4), AHORA)
    assert r.severidad == Severidad.ATENCION
    assert "4" in r.detalle


# ── Sin enlace ───────────────────────────────────────────────────────────

def test_sin_conexion_es_atencion_y_no_problema():
    """🔴 Un corte de internet **no es una falla del nodo**: es el escenario
    para el que se instalo. Pintarlo de rojo enseña a ignorar el rojo."""
    r = resumen_para_la_bandeja(estado(en_linea=False, ultimo_error="sin red"), AHORA)
    assert r.severidad == Severidad.ATENCION
    assert "Sin conexión" in r.titulo
    assert "se sigue operando" in r.detalle.lower()


def test_sin_conexion_dice_cuantas_esperan():
    r = resumen_para_la_bandeja(
        estado(en_linea=False, pendientes=12, ultimo_error="sin red"), AHORA)
    assert "12" in r.detalle


# ── 🔴 El estado viejo ───────────────────────────────────────────────────

def test_un_estado_viejo_que_dice_en_linea_no_se_muestra_verde():
    """La trampa que esta pantalla tiene que resolver.

    Si el servicio se muere, el archivo **queda como estaba**, y lo ultimo que
    dijo puede ser "en linea, todo al dia". Una bandeja que renderice el archivo
    tal cual muestra el icono verde con el nodo muerto -- exactamente la
    pantalla que no puede mentir.
    """
    viejo = estado(
        en_linea=True, pendientes=0,
        ultimo_intento=(AHORA - timedelta(hours=5)).isoformat(),
    )
    r = resumen_para_la_bandeja(viejo, AHORA, intervalo_segundos=60)

    assert r.severidad == Severidad.PROBLEMA
    assert "no está sincronizando" in r.titulo
    assert "hace 5 h" in r.detalle


def test_una_demora_de_un_ciclo_no_alarma():
    """Un ciclo puede tardar de mas porque el central esta lento.

    Una bandeja que grita a la primera demora enseña a ignorarla, que es peor
    que no tenerla.
    """
    demorado = estado(ultimo_intento=(AHORA - timedelta(seconds=90)).isoformat())
    assert resumen_para_la_bandeja(demorado, AHORA, intervalo_segundos=60).severidad \
        == Severidad.OK


def test_tres_ciclos_sin_escribir_ya_es_problema():
    """El limite, medido por los dos lados: 90 s pasa (test de arriba), 200 s no."""
    muerto = estado(ultimo_intento=(AHORA - timedelta(seconds=200)).isoformat())
    assert resumen_para_la_bandeja(muerto, AHORA, intervalo_segundos=60).severidad \
        == Severidad.PROBLEMA


def test_el_limite_se_escala_con_el_intervalo():
    """Un nodo que cicla cada 10 minutos no esta muerto a los 3 minutos.

    Sin esto, el umbral fijo daria falsa alarma permanente en cualquier
    instalacion con un intervalo largo.
    """
    hace_cinco = estado(ultimo_intento=(AHORA - timedelta(minutes=5)).isoformat())
    assert resumen_para_la_bandeja(hace_cinco, AHORA, intervalo_segundos=60).severidad \
        == Severidad.PROBLEMA
    assert resumen_para_la_bandeja(hace_cinco, AHORA, intervalo_segundos=600).severidad \
        == Severidad.OK


def test_un_estado_viejo_igual_dice_cuantas_quedaron():
    """Es el dato que importa cuando el servicio esta caido."""
    muerto = estado(
        pendientes=7, ultimo_intento=(AHORA - timedelta(hours=2)).isoformat())
    assert "7 operaciones sin enviar" in resumen_para_la_bandeja(muerto, AHORA).detalle


# ── Sin estado todavía ───────────────────────────────────────────────────

def test_sin_archivo_de_estado():
    """`leer_estado` devuelve None cuando no existe o esta corrupto."""
    r = resumen_para_la_bandeja(None, AHORA)
    assert r.severidad == Severidad.PROBLEMA
    assert "todavía no sincronizó" in r.titulo


def test_una_marca_ilegible_se_trata_como_sin_datos():
    """Un `ultimo_intento` que no parsea no puede pasar por reciente."""
    roto = estado(ultimo_intento="ayer a la tarde")
    assert resumen_para_la_bandeja(roto, AHORA).severidad == Severidad.PROBLEMA


def test_una_marca_sin_zona_se_asume_utc():
    """Defensa por si alguien edito el archivo a mano: sin zona, no reventar."""
    sin_zona = estado(ultimo_intento="2026-08-30T11:59:40")
    assert resumen_para_la_bandeja(sin_zona, AHORA).severidad == Severidad.OK


# ── El texto ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("segundos,esperado", [
    (30, "hace menos de un minuto"),
    (300, "hace 5 min"),
    (7200, "hace 2 h"),
    (172800, "hace 2 d"),
])
def test_la_antiguedad_se_dice_en_castellano(segundos, esperado):
    viejo = estado(ultimo_intento=(AHORA - timedelta(seconds=segundos)).isoformat())
    r = resumen_para_la_bandeja(viejo, AHORA, intervalo_segundos=segundos * 10)
    assert esperado in r.detalle


def test_el_tooltip_junta_titulo_y_detalle():
    r = resumen_para_la_bandeja(estado(), AHORA)
    assert r.texto.startswith(r.titulo)
    assert r.detalle in r.texto


# ── El comando, que es lo unico ejecutable sin Windows ───────────────────

def test_bandeja_una_vez_imprime_lo_mismo_que_el_tooltip(tmp_path, monkeypatch, capsys):
    """🔴 La unica forma de ver esto sin Windows, y por eso existe.

    El caparazon grafico no se puede correr en este entorno; `--una-vez` imprime
    exactamente lo que iria en el tooltip, asi que la decision queda verificable
    por el camino real del comando y no solo llamando a la funcion.
    """
    from libraedge.cli import main
    from libraedge.nodo import escribir_estado

    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo(
        "node-1", en_linea=False, pendientes=3, cursor=1,
        ultimo_intento=datetime.now(UTC).isoformat(),
    ))
    monkeypatch.setenv("LIBRAEDGE_ESTADO", ruta)

    codigo = main(["bandeja", "--una-vez"])
    salida = capsys.readouterr().out

    assert codigo == 1, "fuera de linea tiene que salir distinto de cero"
    assert "Sin conexión" in salida
    assert "3 operaciones" in salida


def test_bandeja_una_vez_sale_cero_cuando_esta_al_dia(tmp_path, monkeypatch, capsys):
    """El positivo al lado del negativo: sin esto, el codigo 1 de arriba podria
    ser el unico que el comando sabe devolver."""
    from libraedge.cli import main
    from libraedge.nodo import escribir_estado

    ruta = str(tmp_path / "estado.json")
    escribir_estado(ruta, EstadoNodo(
        "node-1", en_linea=True, pendientes=0, cursor=1,
        ultimo_intento=datetime.now(UTC).isoformat(),
    ))
    monkeypatch.setenv("LIBRAEDGE_ESTADO", ruta)

    assert main(["bandeja", "--una-vez"]) == 0
    assert "Al día" in capsys.readouterr().out


def test_bandeja_no_toca_la_base_ni_el_central(tmp_path, monkeypatch, capsys):
    """Tiene que contestar con el servicio caido, que es cuando se la mira."""
    from libraedge.cli import main

    monkeypatch.setenv("LIBRAEDGE_ESTADO", str(tmp_path / "no-existe.json"))
    for sobrante in ("LIBRAEDGE_DATABASE_URL", "LIBRAEDGE_CENTRAL_URL",
                     "LIBRAEDGE_NODE_ID", "LIBRAEDGE_NODE_SECRET",
                     "LIBRAEDGE_TABLAS_ESPEJO"):
        monkeypatch.delenv(sobrante, raising=False)

    assert main(["bandeja", "--una-vez"]) == 1
    assert "todavía no sincronizó" in capsys.readouterr().out
