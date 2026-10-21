"""El ayudante de la prueba de login por http del CI del instalador (ADR-016): lo que se puede probar sin un nodo."""
import json
import stat

import pytest

from tests import login_por_http as lp

CLAVE = "clave-del-admin-que-no-se-imprime-0123456789"


@pytest.mark.parametrize("version, nueva", [
    ("0.52.0", False),
    ("0.9.0", False),
    ("0.53.0", True),
    ("0.53.1", True),
    ("0.60.0", True),
    ("1.0.0", True),
    ("0.53.0.dev3+g1234abc", True),
    ("0.52.1.dev7+gabcdef0", False),
])
def test_que_version_de_libraauth_ya_lee_la_variable(version, nueva):
    assert lp.es_nueva(version) is nueva


def test_la_clave_del_admin_sale_del_nodo_env():
    texto = f"﻿SECRET_KEY=otra\r\nVENTALIBRA_ADMIN_PASSWORD={CLAVE}\r\nADMIN_PASSWORD={CLAVE}\r\n"
    assert lp.clave_del_admin(texto) == CLAVE
    with pytest.raises(SystemExit, match="ADMIN_PASSWORD"):
        lp.clave_del_admin("SECRET_KEY=x\nADMIN_PASSWORD=\n")


def test_el_cuerpo_va_a_un_archivo_privado_sin_mostrar_la_clave(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lp, "resolver_captcha", lambda desafio: f"resuelto-{desafio['n']}")
    entorno = tmp_path / "nodo.env"
    entorno.write_text(f"ADMIN_PASSWORD={CLAVE}\n", encoding="utf-8")
    desafio = tmp_path / "desafio.json"
    desafio.write_text(json.dumps({"n": 7}), encoding="utf-8")
    salida = tmp_path / "cuerpo.json"
    assert lp.main(["cuerpo", str(entorno), str(desafio), str(salida)]) == 0
    assert CLAVE not in capsys.readouterr().out
    assert json.loads(salida.read_text(encoding="utf-8")) == {"username": "admin", "password": CLAVE,
                                                               "captcha": "resuelto-7"}
    assert stat.S_IMODE(salida.stat().st_mode) == 0o600


def test_sin_argumentos_validos_sale_con_2(capsys):
    assert lp.main(["otra-cosa"]) == 2



def test_la_base_del_producto_sale_del_nodo_env_y_no_la_de_libraedge():
    env = ("LIBRAEDGE_DATABASE_URL=postgresql://postgres:x@127.0.0.1:55432/libraedge\n"
           "VENTALIBRA_DATABASE_URL=postgresql://postgres:x@127.0.0.1:55432/ventalibra\n")
    assert lp.url_de_la_base(env).endswith("/ventalibra")


def test_sin_base_del_producto_corta_claro():
    with pytest.raises(SystemExit):
        lp.url_de_la_base("LIBRAEDGE_DATABASE_URL=postgresql://x/y\n")
