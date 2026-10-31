"""La marca blanca viaja en la carga del nodo (ADR-030).

Sin esto, el instalador de un revendedor compila con su nombre pero el nodo, ya instalado, abre la web del producto:
`/api/marca` contesta `propia: false`. La carga lleva lo que `libracore` lee (las claves del `config.json`, el logo y
los Terminos propios) y `config-emisor` lo escribe donde corresponde. Probado en una VM de Windows el 2026-10-31.
"""

import base64
import json
from pathlib import Path

import pytest

from libraedge import instalador as ins

RAIZ = Path(__file__).resolve().parent.parent
SECRETO = "secreto-que-no-tiene-que-aparecer-0123456789"
#: Un PNG de 1x1 (basta la firma: el logo real lo re-codifica libracore al subirlo al central).
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
TERMINOS = "# Terminos de Comercial Modelo\n\nTexto de prueba con acentos: facturacion, cancelacion.\n"
CONFIG_DEL_CENTRAL = {
    "empresa_nombre": "Comercio de Prueba S.A.",
    "empresa_cuit": "30-99999904-9",
    "empresa_iva_condition": "Responsable Inscripto",
    "mp_access_token": "no-tiene-que-viajar",
    "marca": {"nombre": "Comercial Modelo", "nombre_corto": "Modelo", "color": "#1f5e8c", "logo": "a" * 16},
    "terminos_propios": {"version": "1.0", "vigente_desde": "31-10-2026"},
    "terminos_propios_historial": {"1.0": "b" * 64},
}


def _definicion(tmp_path: Path) -> str:
    ruta = tmp_path / "producto.json"
    ruta.write_text((RAIZ / "instalador" / "productos" / "ventalibra.json").read_text(encoding="utf-8"),
                    encoding="utf-8")
    return str(ruta)


def _entorno() -> dict[str, str]:
    return {
        "LIBRAEDGE_CENTRAL_URL": "https://cliente.ventalibra.com.ar/",
        "LIBRAEDGE_NODE_ID": "nodo-centro",
        "LIBRAEDGE_NODE_SECRET": SECRETO,
        "LIBRAEDGE_PREFIJO": "S02",
        "LIBRAEDGE_TABLAS_ESPEJO": "branches:id,party_roles:party_id+role",
    }


def _central(tmp_path: Path, config=None, logo=True, terminos=True) -> str:
    """Una instancia central con marca: devuelve la ruta de su `config.json`."""
    carpeta = tmp_path / "central"
    (carpeta / "marca").mkdir(parents=True, exist_ok=True)
    ruta = carpeta / "config.json"
    ruta.write_text(json.dumps(CONFIG_DEL_CENTRAL if config is None else config), encoding="utf-8")
    if logo:
        (carpeta / "marca" / "logo.png").write_bytes(PNG)
    if terminos:
        (carpeta / "marca" / "terminos.md").write_text(TERMINOS, encoding="utf-8")
    return str(ruta)


def _armar(marca=None) -> dict:
    return ins.armar_carga("ventalibra", _entorno(), "https://cliente.ventalibra.com.ar", None,
                           {"id": "2", "nombre": "Centro"}, marca)


# ── el lado del central ─────────────────────────────────────────────────────


def test_la_marca_sale_del_config_del_central_sin_lo_que_no_viaja(tmp_path):
    marca = ins.marca_desde_config(_central(tmp_path))
    assert set(marca["config"]) == set(ins.CLAVES_DE_LA_MARCA)
    assert "mp_access_token" not in json.dumps(marca) and "empresa_cuit" not in json.dumps(marca)
    assert base64.b64decode(marca["logo_png"]) == PNG
    assert marca["terminos"] == TERMINOS


def test_sin_marca_propia_no_hay_nada_que_llevar(tmp_path):
    assert ins.marca_desde_config(str(tmp_path / "no-esta.json")) is None
    assert ins.marca_desde_config(_central(tmp_path, config={"empresa_nombre": "X"})) is None
    assert ins.marca_desde_config(_central(tmp_path, config={"marca": {}})) is None


def test_la_carga_sin_marca_queda_como_antes():
    carga = ins.carga_desde_dict(_armar(), "ventalibra")
    assert carga.marca is None and "marca" not in carga.resumen()


def test_armar_la_carga_con_marca_y_leerla_de_vuelta(tmp_path):
    carga = ins.carga_desde_dict(_armar(ins.marca_desde_config(_central(tmp_path))), "ventalibra")
    assert carga.marca["logo"] == PNG and carga.marca["terminos"] == TERMINOS
    # El unico aviso es el del emisor, que este test no arma.
    assert not [a for a in carga.avisos if "marca" in a.lower()]
    resumen = json.dumps(carga.resumen(), ensure_ascii=False)
    assert '"nombre": "Comercial Modelo"' in resumen
    assert TERMINOS[:20] not in resumen and base64.b64encode(PNG).decode() not in resumen and SECRETO not in resumen


# ── lo que se rechaza ───────────────────────────────────────────────────────


def _con(tmp_path, **cambios) -> dict:
    marca = ins.marca_desde_config(_central(tmp_path))
    marca.update(cambios)
    return _armar_sin_validar(marca)


def _armar_sin_validar(marca) -> dict:
    carga = _armar()
    carga["marca"] = marca
    return carga


@pytest.mark.parametrize("cambios, texto", [
    ({"logo_png": base64.b64encode(b"GIF89a-no-es-un-png").decode()}, "tiene que ser un PNG"),
    ({"logo_png": "esto no es base64 !!"}, "base64"),
    ({"logo_png": base64.b64encode(PNG + b"0" * ins.MAX_LOGO_BYTES).decode()}, "pesa mas"),
    ({"terminos": "   "}, "Terminos de la marca"),
    ({"terminos": "x" * (ins.MAX_TERMINOS + 1)}, "pasan de"),
    ({"extra": 1}, "claves que no le corresponden"),
])
def test_una_marca_mal_formada_se_rechaza(tmp_path, cambios, texto):
    with pytest.raises(ins.ErrorDeInstalacion, match=texto):
        ins.carga_desde_dict(_con(tmp_path, **cambios), "ventalibra")


def test_la_marca_no_deja_pasar_otras_claves_del_config(tmp_path):
    marca = ins.marca_desde_config(_central(tmp_path))
    marca["config"]["mp_access_token"] = "se-colo"
    with pytest.raises(ins.ErrorDeInstalacion, match="no se copian"):
        ins.carga_desde_dict(_armar_sin_validar(marca), "ventalibra")


def test_una_marca_sin_nombre_se_rechaza(tmp_path):
    marca = ins.marca_desde_config(_central(tmp_path))
    marca["config"]["marca"] = {"color": "#1f5e8c"}
    with pytest.raises(ins.ErrorDeInstalacion, match="no tiene nombre"):
        ins.carga_desde_dict(_armar_sin_validar(marca), "ventalibra")


def test_marca_sin_terminos_avisa_que_el_nodo_queda_congelado(tmp_path):
    marca = ins.marca_desde_config(_central(tmp_path, terminos=False))
    carga = ins.carga_desde_dict(_armar_sin_validar(marca), "ventalibra")
    assert any("congelado" in aviso for aviso in carga.avisos)


def test_marca_con_logo_declarado_y_sin_archivo_avisa(tmp_path):
    marca = ins.marca_desde_config(_central(tmp_path, logo=False))
    carga = ins.carga_desde_dict(_armar_sin_validar(marca), "ventalibra")
    assert any("sin logo" in aviso for aviso in carga.avisos)


# ── el lado del nodo ────────────────────────────────────────────────────────


def _carga_en_archivo(tmp_path, marca) -> str:
    ruta = tmp_path / "carga.json"
    carga = _armar(marca)
    carga.pop("emisor", None)
    ruta.write_text(json.dumps(carga), encoding="utf-8")
    return str(ruta)


def test_config_emisor_escribe_la_marca_donde_la_lee_libracore(tmp_path, capsys):
    definicion = _definicion(tmp_path)
    carga = _carga_en_archivo(tmp_path, ins.marca_desde_config(_central(tmp_path)))
    nodo = tmp_path / "nodo" / "config.json"
    nodo.parent.mkdir()
    nodo.write_text(json.dumps({"ticket_pie": "gracias", "marca": {"nombre": "VentaLibra viejo"}}), encoding="utf-8")

    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", carga, "--config", str(nodo)]) == 0
    salida = capsys.readouterr().out
    assert "Marca blanca" in salida and SECRETO not in salida

    config = json.loads(nodo.read_text(encoding="utf-8"))
    assert config["marca"]["nombre"] == "Comercial Modelo" and config["ticket_pie"] == "gracias"
    assert config["terminos_propios"]["version"] == "1.0"
    assert (nodo.parent / "marca" / "logo.png").read_bytes() == PNG
    assert (nodo.parent / "marca" / "terminos.md").read_text(encoding="utf-8") == TERMINOS

    # Reinstalar encima con la misma carga no cambia nada.
    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", carga, "--config", str(nodo)]) == 0
    assert "0 cambios" in capsys.readouterr().out


def test_reinstalar_con_una_carga_sin_marca_no_borra_la_que_tiene(tmp_path):
    definicion = _definicion(tmp_path)
    nodo = tmp_path / "nodo" / "config.json"
    nodo.parent.mkdir()
    con = _carga_en_archivo(tmp_path, ins.marca_desde_config(_central(tmp_path)))
    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", con, "--config", str(nodo)]) == 0
    sin = tmp_path / "sin.json"
    carga = _armar()
    carga.pop("emisor", None)
    sin.write_text(json.dumps(carga), encoding="utf-8")
    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", str(sin), "--config", str(nodo)]) == 0
    assert json.loads(nodo.read_text(encoding="utf-8"))["marca"]["nombre"] == "Comercial Modelo"
    assert (nodo.parent / "marca" / "logo.png").exists()
