"""El instalador del nodo, lo que se puede probar fuera de Windows (ADR-010).

Las definiciones de los productos (`instalador/productos/*.json`), la carga del
nodo, el armado del `nodo.env`, el `config.json` del emisor y el ayudante que
corren los scripts de PowerShell. Lo de Windows (Inno Setup, NSSM, el
PostgreSQL embebido) se prueba en una VM: `instalador/PROBAR_EN_VM.md`.
"""

import json
import os
import re
import stat
from pathlib import Path

import pytest

from libraedge import instalador as ins

RAIZ = Path(__file__).resolve().parent.parent
CARPETA = RAIZ / "instalador"
PRODUCTOS = sorted(p.stem for p in (CARPETA / "productos").glob("*.json"))
SECRETO = "secreto-que-no-tiene-que-aparecer-0123456789"


def _definicion(_de="ventalibra", **cambios):
    datos = json.loads((CARPETA / "productos" / f"{_de}.json").read_text(encoding="utf-8"))
    datos.update(cambios)
    return datos


def _carga(producto="ventalibra", variables=None, **cambios):
    datos = {
        "formato": ins.FORMATO_CARGA,
        "producto": producto,
        "entorno": {
            "LIBRAEDGE_CENTRAL_URL": "https://cliente.ventalibra.com.ar/",
            "LIBRAEDGE_NODE_ID": "nodo-centro",
            "LIBRAEDGE_NODE_SECRET": SECRETO,
            "LIBRAEDGE_PREFIJO": "S02",
            "LIBRAEDGE_TABLAS_ESPEJO": "branches:id,party_roles:party_id+role",
        },
        "sucursal": {"id": "2", "nombre": "Centro"},
        "emisor": {"empresa_nombre": "Comercio de Prueba S.A.", "empresa_cuit": "30-99999904-9",
                   "empresa_iva_condition": "Responsable Inscripto"},
    }
    datos["entorno"].update(variables or {})
    datos.update(cambios)
    return datos


# ── las definiciones del repo ───────────────────────────────────────────────


def test_estan_los_tres_productos_de_la_fase_1():
    assert PRODUCTOS == ["contalibra", "restolibra", "ventalibra"]


@pytest.mark.parametrize("producto", PRODUCTOS)
def test_cada_definicion_del_repo_es_valida(producto):
    definicion = ins.leer_definicion(str(CARPETA / "productos" / f"{producto}.json"))
    assert definicion.producto == producto
    assert definicion.prefijo_variables == producto.upper()
    assert definicion.asgi == "app.asgi:app"
    assert definicion.salud == "/health"
    # Las cuatro cadenas del central (`configure(migraciones=...)` de scripts/panel_admin.py), en orden.
    assert [m[0] for m in definicion.migraciones] == [
        "libracore-migrar", "libraauth-migrar", "libracommerce-migrar", "alembic"]
    assert all(m[m.index("--prefijo") + 1] == producto for m in definicion.migraciones if "--prefijo" in m)
    # Una base, con las dos variables que escribe el alta de libracore (dominio y core).
    assert [b.nombre for b in definicion.bases] == [producto]
    assert definicion.base_del_nodo.variables == (f"{producto.upper()}_DATABASE_URL",
                                                  f"{producto.upper()}_LIBRACORE_DATABASE_URL")
    assert "tzdata" in definicion.extras  # zoneinfo en Windows no trae la base de zonas
    plan = definicion.plan()
    assert json.loads(json.dumps(plan)) == plan
    assert plan["base_del_nodo"] == producto


def test_la_base_de_auth_de_cada_producto():
    auth = {p: ins.leer_definicion(str(CARPETA / "productos" / f"{p}.json")).base_auth for p in PRODUCTOS}
    assert auth == {"contalibra": "dominio", "restolibra": "dominio", "ventalibra": "core"}


def test_el_iss_tiene_la_misma_tabla_que_las_definiciones():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    patron = r'#(?:el)?if Producto == "(\w+)"\s+#define NombreProducto "([^"]+)"\s+#define AppIdProducto\s+"([^"]+)"'
    filas = {producto: (nombre, app_id) for producto, nombre, app_id in re.findall(patron, iss)}
    assert sorted(filas) == PRODUCTOS
    for producto, (nombre, app_id) in filas.items():
        definicion = ins.leer_definicion(str(CARPETA / "productos" / f"{producto}.json"))
        assert (nombre, app_id) == (definicion.nombre, definicion.app_id), producto
    # El de RestoLibra es el de siempre: cambiarlo instalaria una segunda copia al lado de la vieja.
    assert filas["restolibra"][1] == "F4887308-2B5A-4D5A-B694-1F85F48CBA4B"
    assert len({a for _, a in filas.values()}) == len(filas)


def test_el_iss_copia_todos_los_scripts():
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    for script in sorted(p.name for p in CARPETA.glob("*.ps1")):
        assert f'Source: "{script}"' in iss, script
    assert 'Source: "..\\libraedge\\instalador.py"' in iss


def test_ninguna_linea_del_codigo_del_iss_empieza_con_corchete():
    """Inno toma una linea que empieza con «[» como seccion nueva, aunque este dentro de un comentario de [Code]:
    ISCC cortaba con «Invalid section tag» (medido en la VM, 2026-10-19)."""
    lineas = (CARPETA / "nodo.iss").read_text(encoding="utf-8").splitlines()
    inicio = lineas.index("[Code]")
    malas = [f"{i + 1}: {linea}" for i, linea in enumerate(lineas[inicio + 1:], start=inicio + 1)
             if linea.lstrip().startswith("[")]
    assert malas == []


def test_la_actualizacion_para_postgres_antes_de_copiar():
    """Sus binarios se reemplazan y el RestartManager no cierra un servicio: en silencio era Abort (VM, 2026-10-19)."""
    iss = (CARPETA / "nodo.iss").read_text(encoding="utf-8")
    prepara = iss[iss.index("function PrepareToInstall"):]
    prepara = prepara[:prepara.index("\nend;")]
    assert "stop LibraEdgePostgres" in prepara
    assert "CloseApplications=force" in iss


def test_el_paquete_lleva_los_modulos_sueltos_de_la_raiz_del_producto():
    """plans.py vive en la raiz de los tres productos y app/db.py lo importa (VM, 2026-10-19)."""
    script = (CARPETA / "armar_paquete.sh").read_text(encoding="utf-8")
    assert "ls-tree --name-only HEAD" in script and '"${sueltos[@]}"' in script


def test_los_textos_de_los_scripts_son_ascii():
    """Windows PowerShell 5.1 lee un .ps1 sin BOM como ANSI e ISCC un .iss tambien: un acento en un texto sale
    roto y un emoji puede cerrar la cadena (paso el 2026-08-30). En los comentarios no importa."""
    for archivo in [*CARPETA.glob("*.ps1"), CARPETA / "nodo.iss"]:
        comentario = ";" if archivo.suffix == ".iss" else "#"
        for numero, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
            if linea.strip().startswith(comentario):
                continue
            assert linea.isascii(), f"{archivo.name}:{numero}: {linea.strip()}"


def test_el_ayudante_solo_usa_la_biblioteca_estandar():
    """Se copia al lado de los scripts y corre con el Python del producto, que trae su propio libraedge."""
    fuente = (RAIZ / "libraedge" / "instalador.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+libraedge", fuente, re.M)


# ── definiciones invalidas ──────────────────────────────────────────────────


@pytest.mark.parametrize("cambios, error", [
    ({"formato": "otro"}, "formato"),
    ({"producto": "Venta Libra"}, "minusculas"),
    ({"app_id": "no-es-un-guid"}, "GUID"),
    ({"prefijo_variables": "VENTA"}, "prefijo_variables"),
    ({"puerto": 55432}, "puerto"),
    ({"puerto": "8000"}, "puerto"),
    ({"salud": "health"}, "salud"),
    ({"asgi": "app.asgi"}, "asgi"),
    ({"migraciones": []}, "migraciones"),
    ({"migraciones": [["Rm -rf"]]}, "migracion"),
    ({"base_auth": "dominio"}, "base_auth"),
    ({"base_auth": "otra"}, "base_auth"),
    ({"migracion": []}, "claves desconocidas"),
    ({"bases": []}, "bases"),
    ({"bases": [{"nombre": "a", "variables": ["A_URL"]}, {"nombre": "b", "variables": ["B_URL"]}]}, "nodo"),
    ({"bases": [{"nombre": "a", "variables": ["X"]}, {"nombre": "b", "variables": ["X"], "nodo": True}]},
     "repetida"),
    ({"bases": [{"nombre": "a", "variables": ["LIBRAEDGE_DATABASE_URL"]}]}, "repetida"),
    ({"variables": {"SECRET_KEY": "fija"}}, "SECRET_KEY"),
    ({"extras": "tzdata"}, "extras"),
])
def test_una_definicion_mal_escrita_no_pasa(cambios, error):
    with pytest.raises(ins.ErrorDeInstalacion, match=error):
        ins.definicion_desde_dict(_definicion(**cambios))


def test_varias_bases_y_una_marcada_para_el_nodo():
    definicion = ins.definicion_desde_dict(_definicion(bases=[
        {"nombre": "libraclub", "variables": ["LIBRACLUB_DATABASE_URL"]},
        {"nombre": "libraclub_core", "variables": ["LIBRACLUB_LIBRACORE_DATABASE_URL"], "nodo": True},
    ], producto="libraclub", prefijo_variables="LIBRACLUB", base_auth=None, migraciones=[["alembic", "upgrade"]]))
    assert definicion.base_del_nodo.nombre == "libraclub_core"
    assert definicion.plan()["bases"][0] == {"nombre": "libraclub", "variables": ["LIBRACLUB_DATABASE_URL"],
                                             "nodo": False}


def test_leer_una_definicion_que_no_existe_o_no_es_json(tmp_path):
    with pytest.raises(ins.ErrorDeInstalacion, match="No esta"):
        ins.leer_definicion(str(tmp_path / "no.json"))
    roto = tmp_path / "roto.json"
    roto.write_text("{", encoding="utf-8")
    with pytest.raises(ins.ErrorDeInstalacion, match="JSON"):
        ins.leer_definicion(str(roto))
    with pytest.raises(ins.ErrorDeInstalacion, match="objeto"):
        ins.definicion_desde_dict([])


# ── la carga del nodo ───────────────────────────────────────────────────────


def test_una_carga_valida_y_su_resumen_sin_el_secreto():
    carga = ins.carga_desde_dict(_carga(), "ventalibra")
    assert carga.entorno["LIBRAEDGE_CENTRAL_URL"] == "https://cliente.ventalibra.com.ar"  # sin la barra
    assert carga.emisor["empresa_cuit"] == "30-99999904-9"
    resumen = carga.resumen()
    assert resumen["node_id"] == "nodo-centro" and resumen["prefijo"] == "S02" and resumen["tablas_espejo"] == 2
    assert SECRETO not in json.dumps(resumen)
    assert resumen["avisos"] == []


def test_la_carga_de_prueba_de_la_vm_es_valida():
    carga = ins.leer_carga(str(CARPETA / "carga-de-prueba.json"), "ventalibra")
    assert carga.entorno["LIBRAEDGE_CENTRAL_URL"].endswith(".invalid")
    assert carga.emisor["empresa_cuit"] == "30-99999904-9"


@pytest.mark.parametrize("cambios, variables, error", [
    ({"formato": "x"}, None, "formato"),
    ({"producto": "restolibra"}, None, "es de 'restolibra'"),
    ({"entorno": "x"}, None, "entorno"),
    ({}, {"LIBRAEDGE_PREFIJO": ""}, "LIBRAEDGE_PREFIJO"),
    ({}, {"PATH": "C:\\malo"}, "no le corresponden"),
    ({}, {"SECRET_KEY": "x"}, "no le corresponden"),
    ({}, {"LIBRAEDGE_CENTRAL_URL": "http://cliente.ventalibra.com.ar"}, "https"),
    ({}, {"LIBRAEDGE_CENTRAL_URL": "https://usuario:clave@cliente.com.ar"}, "base del central"),
    ({}, {"LIBRAEDGE_CENTRAL_URL": "https://cliente.com.ar/?x=1"}, "base del central"),
    ({}, {"LIBRAEDGE_NODE_ID": "nodo centro"}, "NODE_ID"),
    ({}, {"LIBRAEDGE_PREFIJO": "s02"}, "PREFIJO"),
    ({}, {"LIBRAEDGE_PREFIJO": "S0123456X"}, "PREFIJO"),
    ({}, {"LIBRAEDGE_NODE_SECRET": "corto"}, "NODE_SECRET"),
    ({}, {"LIBRAEDGE_NODE_SECRET": SECRETO + "\nPATH=C:\\malo"}, "salto de linea"),
    ({}, {"LIBRAEDGE_TABLAS_ESPEJO": " , "}, "vacia"),
    ({}, {"LIBRAEDGE_TABLAS_ESPEJO": "ventas;drop:id"}, "no valida"),
    ({"emisor": {"empresa_cuit": "30-99999904-1"}}, None, "CUIT"),
    ({"emisor": ["x"]}, None, "emisor"),
    ({"emisor": {"empresa_nombre": {"a": 1}}}, None, "valor no valido"),
    ({"sucursal": "Centro"}, None, "sucursal"),
])
def test_una_carga_mala_no_pasa_y_no_muestra_el_secreto(cambios, variables, error):
    with pytest.raises(ins.ErrorDeInstalacion, match=error) as exc:
        ins.carga_desde_dict(_carga(variables=variables, **cambios), "ventalibra")
    assert SECRETO not in str(exc.value)


def test_http_solo_contra_la_misma_pc():
    carga = ins.carga_desde_dict(_carga(variables={"LIBRAEDGE_CENTRAL_URL": "http://127.0.0.1:8000"}), "ventalibra")
    assert carga.entorno["LIBRAEDGE_CENTRAL_URL"] == "http://127.0.0.1:8000"


def test_el_emisor_avisa_lo_que_falta_y_lo_que_se_ignora():
    sin = ins.carga_desde_dict(_carga(emisor=None), "ventalibra")
    assert sin.emisor == {} and "emisor" in sin.avisos[0]
    otra = ins.carga_desde_dict(_carga(emisor={"empresa_nombre": "X", "mp_access_token": "APP-123"}), "ventalibra")
    assert otra.emisor == {"empresa_nombre": "X"}  # una credencial nunca se copia
    assert any("mp_access_token" in a for a in otra.avisos)
    assert any("empresa_cuit, empresa_iva_condition" in a for a in otra.avisos)


@pytest.mark.parametrize("cuit, valido", [
    ("30-99999904-9", True), ("30999999049", True), ("20-12345678-6", True),
    ("30-99999904-8", False), ("3099999904", False), ("30-9999990A-9", False)])
def test_el_digito_verificador_del_cuit(cuit, valido):
    assert ins._cuit_valido(cuit) is valido


# ── el entorno del nodo ─────────────────────────────────────────────────────


def _armar(previo=None, definicion=None, carga=None, clave="c@ve/con:raros"):
    contador = iter(range(100))
    return ins.armar_entorno(
        definicion or ins.definicion_desde_dict(_definicion()), carga or ins.carga_desde_dict(_carga(), "ventalibra"),
        clave_pg=clave, estado="C:\\Program Files\\LibraEdge\\estado.json", data_dir="C:\\ProgramData\\LibraEdge\\app",
        previo=previo, generar=lambda: f"generado-{next(contador)}")


def test_el_entorno_completo_del_nodo():
    entorno = _armar()
    url = "postgresql://postgres:c%40ve%2Fcon%3Araros@127.0.0.1:55432/ventalibra"
    assert entorno == {
        "LIBRAEDGE_CENTRAL_URL": "https://cliente.ventalibra.com.ar",
        "LIBRAEDGE_NODE_ID": "nodo-centro",
        "LIBRAEDGE_NODE_SECRET": SECRETO,
        "LIBRAEDGE_PREFIJO": "S02",
        "LIBRAEDGE_TABLAS_ESPEJO": "branches:id,party_roles:party_id+role",
        "LIBRAEDGE_DATABASE_URL": url,
        "LIBRAEDGE_ESTADO": "C:\\Program Files\\LibraEdge\\estado.json",
        "LIBRAEDGE_VERSION_ESQUEMA": "alembic",
        "VENTALIBRA_DATABASE_URL": url,
        "VENTALIBRA_LIBRACORE_DATABASE_URL": url,
        "DATA_DIR": "C:\\ProgramData\\LibraEdge\\app",
        "SECRET_KEY": "generado-0",
        "ADMIN_PASSWORD": "generado-1",
        "VENTALIBRA_ADMIN_PASSWORD": "generado-1",
        "TZ": "America/Argentina/Buenos_Aires",
    }
    assert ins.revisar_entorno(ins.definicion_desde_dict(_definicion()), entorno) == []


def test_una_reinstalacion_conserva_lo_generado():
    """Un SECRET_KEY nuevo deja ilegible lo que libraauth cifro con el anterior."""
    entorno = _armar(previo={"SECRET_KEY": "el-de-antes", "VENTALIBRA_ADMIN_PASSWORD": "admin-de-antes",
                             "LIBRAEDGE_NODE_SECRET": "uno-viejo-que-no-vale"})
    assert entorno["SECRET_KEY"] == "el-de-antes"
    assert entorno["ADMIN_PASSWORD"] == entorno["VENTALIBRA_ADMIN_PASSWORD"] == "admin-de-antes"
    # Lo que viene de la carga manda: re-registrar emite un secreto nuevo.
    assert entorno["LIBRAEDGE_NODE_SECRET"] == SECRETO


def test_lo_generado_por_defecto_es_aleatorio():
    definicion = ins.definicion_desde_dict(_definicion())
    carga = ins.carga_desde_dict(_carga(), "ventalibra")
    a, b = (ins.armar_entorno(definicion, carga, clave_pg="x", estado="e", data_dir="d") for _ in range(2))
    assert a["SECRET_KEY"] != b["SECRET_KEY"] and len(a["SECRET_KEY"]) >= 48


def test_las_variables_fijas_de_la_definicion_y_las_de_cada_base():
    definicion = ins.definicion_desde_dict(_definicion(
        variables={"ENV": "production"},
        bases=[{"nombre": "ventalibra", "variables": ["VENTALIBRA_DATABASE_URL"], "nodo": True},
               {"nombre": "ventalibra_core", "variables": ["VENTALIBRA_LIBRACORE_DATABASE_URL"]}]))
    entorno = _armar(definicion=definicion, clave="abc")
    assert entorno["ENV"] == "production"
    assert entorno["VENTALIBRA_LIBRACORE_DATABASE_URL"].endswith("/ventalibra_core")
    assert entorno["LIBRAEDGE_DATABASE_URL"].endswith("/ventalibra")


@pytest.mark.parametrize("kwargs, error", [
    ({"clave_pg": ""}, "PostgreSQL"),
    ({"clave_pg": "a\nb"}, "PostgreSQL"),
    ({"estado": ""}, "LIBRAEDGE_ESTADO"),
    ({"data_dir": "C:\\x\nPATH=y"}, "DATA_DIR"),
])
def test_el_entorno_rechaza_rutas_y_claves_malas(kwargs, error):
    base = {"clave_pg": "x", "estado": "e", "data_dir": "d"}
    base.update(kwargs)
    with pytest.raises(ins.ErrorDeInstalacion, match=error):
        ins.armar_entorno(ins.definicion_desde_dict(_definicion()), ins.carga_desde_dict(_carga(), "ventalibra"),
                          **base)


def test_una_carga_de_otro_producto_no_arma_entorno():
    carga = ins.carga_desde_dict(_carga("restolibra"), "restolibra")
    with pytest.raises(ins.ErrorDeInstalacion, match="producto"):
        _armar(carga=carga)


def test_formatear_y_leer_el_entorno():
    entorno = _armar()
    texto = ins.formatear_entorno(entorno)
    assert texto.endswith("\n") and "\r" not in texto
    assert ins.leer_entorno(texto) == entorno
    assert ins.leer_entorno("\ufeffA=1\r\n# comentario\n\nB=x=y\nsin igual\n") == {"A": "1", "B": "x=y"}
    with pytest.raises(ins.ErrorDeInstalacion):
        ins.formatear_entorno({"A": "1\nB=2"})
    with pytest.raises(ins.ErrorDeInstalacion):
        ins.formatear_entorno({"a minuscula": "1"})


def test_revisar_entorno_nombra_lo_que_falta():
    definicion = ins.definicion_desde_dict(_definicion())
    entorno = _armar()
    del entorno["LIBRAEDGE_PREFIJO"]
    entorno["SECRET_KEY"] = " "
    assert ins.revisar_entorno(definicion, entorno) == ["LIBRAEDGE_PREFIJO", "SECRET_KEY"]


def test_el_nodo_env_solo_se_escribe_si_ya_existe(tmp_path):
    """Lo crea PowerShell con la ACL cortada: crearlo aca lo dejaria con los permisos heredados."""
    ruta = tmp_path / "nodo.env"
    with pytest.raises(ins.ErrorDeInstalacion, match="no existe"):
        ins.escribir_en_archivo_protegido(str(ruta), "A=1\n")
    ruta.write_text("UNA=variable-larga-que-tiene-que-desaparecer\n", encoding="utf-8")
    ins.escribir_en_archivo_protegido(str(ruta), "A=1\n")
    assert ruta.read_bytes() == b"A=1\n"  # sin BOM, sin restos del contenido anterior


# ── el config.json del emisor ───────────────────────────────────────────────


def test_el_config_del_emisor_se_crea_y_se_fusiona(tmp_path):
    ruta = tmp_path / "app" / "config.json"
    assert ins.fusionar_config(str(ruta), {"empresa_cuit": "30-99999904-9"}) == ["empresa_cuit"]
    assert not ruta.read_bytes().startswith(b"\xef\xbb\xbf")
    ruta.write_text(json.dumps({"empresa_cuit": "30-99999904-9", "ticket_pie": "Gracias", "servicio_estado":
                                "activo"}), encoding="utf-8")
    assert ins.fusionar_config(str(ruta), {"empresa_cuit": "30-99999904-9"}) == []
    assert ins.fusionar_config(str(ruta), {"empresa_nombre": "Comercio de Prueba S.A.", "ticket_pie": "Chau"}) == [
        "empresa_nombre", "ticket_pie"]
    assert json.loads(ruta.read_text(encoding="utf-8")) == {
        "empresa_cuit": "30-99999904-9", "ticket_pie": "Chau", "servicio_estado": "activo",
        "empresa_nombre": "Comercio de Prueba S.A."}
    assert [p.name for p in ruta.parent.iterdir()] == ["config.json"]  # sin temporales


def test_un_config_ilegible_no_se_pisa(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text("{roto", encoding="utf-8")
    with pytest.raises(ins.ErrorDeInstalacion):
        ins.fusionar_config(str(ruta), {"empresa_nombre": "X"})
    assert ruta.read_text(encoding="utf-8") == "{roto"
    ruta.write_text("[1]", encoding="utf-8")
    with pytest.raises(ins.ErrorDeInstalacion, match="objeto"):
        ins.fusionar_config(str(ruta), {"empresa_nombre": "X"})


# ── del lado del central ────────────────────────────────────────────────────


def test_el_emisor_sale_del_config_del_central_sin_credenciales(tmp_path):
    ruta = tmp_path / "config.json"
    assert ins.emisor_desde_config(str(ruta)) is None
    ruta.write_text(json.dumps({"empresa_nombre": "Comercio de Prueba S.A.", "empresa_cuit": "30-99999904-9",
                                "mp_access_token": "APP-USR-x", "email_smtp_password": "x", "logo_path": "/app/x",
                                "servicio_estado": "activo", "ticket_ancho_mm": 58}), encoding="utf-8")
    assert ins.emisor_desde_config(str(ruta)) == {
        "empresa_nombre": "Comercio de Prueba S.A.", "empresa_cuit": "30-99999904-9", "ticket_ancho_mm": 58}
    ruta.write_text("[]", encoding="utf-8")
    with pytest.raises(ins.ErrorDeInstalacion):
        ins.emisor_desde_config(str(ruta))
    assert ins.ruta_config_del_central({"DATA_DIR": "/app/data"}) == os.path.join("/app/data", "config.json")


def test_la_carga_se_escribe_privada_y_no_pisa(tmp_path):
    entorno = {k: v for k, v in _carga()["entorno"].items() if k != "LIBRAEDGE_CENTRAL_URL"}
    carga = ins.armar_carga("ventalibra", entorno, "https://cliente.ventalibra.com.ar", {"empresa_nombre": "X"},
                            {"id": "2", "nombre": "Centro"})
    ruta = tmp_path / "carga-nodo.json"
    ins.escribir_carga(str(ruta), carga)
    assert stat.S_IMODE(ruta.stat().st_mode) == 0o600
    leida = ins.leer_carga(str(ruta), "ventalibra")
    assert leida.entorno["LIBRAEDGE_NODE_SECRET"] == SECRETO and leida.sucursal == {"id": "2", "nombre": "Centro"}
    with pytest.raises(FileExistsError):
        ins.escribir_carga(str(ruta), carga)
    sin_emisor = ins.armar_carga("ventalibra", entorno, "https://c.com.ar", None)
    assert "emisor" not in sin_emisor
    with pytest.raises(ins.ErrorDeInstalacion, match="https"):
        ins.armar_carga("ventalibra", entorno, "http://c.com.ar", None)


# ── la linea de comandos del ayudante ───────────────────────────────────────


@pytest.fixture
def archivos(tmp_path):
    definicion = tmp_path / "producto.json"
    definicion.write_text(json.dumps(_definicion()), encoding="utf-8")
    carga = tmp_path / "carga.json"
    carga.write_text(json.dumps(_carga()), encoding="utf-8")
    clave = tmp_path / "postgres.clave"
    clave.write_text("clave-del-pg\n", encoding="utf-8")
    return tmp_path, str(definicion), str(carga), str(clave)


def test_el_ayudante_plan_y_validar(archivos, capsys):
    _, definicion, carga, _ = archivos
    assert ins.main(["plan", "--definicion", definicion]) == 0
    assert json.loads(capsys.readouterr().out)["migraciones"][1][-1] == "core"
    assert ins.main(["validar", "--definicion", definicion, "--carga", carga]) == 0
    salida = capsys.readouterr().out
    assert json.loads(salida)["node_id"] == "nodo-centro" and SECRETO not in salida


def test_el_ayudante_escribe_el_entorno_sin_mostrar_valores(archivos, capsys):
    carpeta, definicion, carga, clave = archivos
    env = carpeta / "nodo.env"
    argumentos = ["entorno", "--definicion", definicion, "--carga", carga, "--clave-pg", clave,
                  "--estado", "C:\\e.json", "--data-dir", "C:\\app", "--salida", str(env)]
    assert ins.main(argumentos) == 2  # el archivo no existe: lo tiene que crear PowerShell con su ACL
    assert "no existe" in capsys.readouterr().err
    env.write_text("SECRET_KEY=el-de-antes\n", encoding="utf-8")
    assert ins.main(argumentos) == 0
    salida = capsys.readouterr().out
    assert SECRETO not in salida and "clave-del-pg" not in salida and "LIBRAEDGE_NODE_SECRET" in salida
    escrito = ins.leer_entorno(env.read_text(encoding="utf-8"))
    assert escrito["SECRET_KEY"] == "el-de-antes"
    assert escrito["LIBRAEDGE_DATABASE_URL"] == "postgresql://postgres:clave-del-pg@127.0.0.1:55432/ventalibra"
    assert ins.main(["revisar-entorno", "--definicion", definicion, "--entorno", str(env)]) == 0
    env.write_text("SECRET_KEY=x\n", encoding="utf-8")
    assert ins.main(["revisar-entorno", "--definicion", definicion, "--entorno", str(env)]) == 1
    assert "FALTA LIBRAEDGE_NODE_ID" in capsys.readouterr().out


def test_el_ayudante_sin_clave_de_postgres(archivos, capsys):
    carpeta, definicion, carga, _ = archivos
    (carpeta / "nodo.env").write_text("", encoding="utf-8")
    assert ins.main(["entorno", "--definicion", definicion, "--carga", carga, "--clave-pg",
                     str(carpeta / "no-esta"), "--estado", "e", "--data-dir", "d",
                     "--salida", str(carpeta / "nodo.env")]) == 2
    assert "clave de PostgreSQL" in capsys.readouterr().err


def test_el_ayudante_config_emisor(archivos, capsys):
    carpeta, definicion, carga, _ = archivos
    config = carpeta / "app" / "config.json"
    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", carga, "--config", str(config)]) == 0
    assert "3 claves" in capsys.readouterr().out
    assert json.loads(config.read_text(encoding="utf-8"))["empresa_iva_condition"] == "Responsable Inscripto"
    sin = carpeta / "sin-emisor.json"
    sin.write_text(json.dumps(_carga(emisor=None)), encoding="utf-8")
    assert ins.main(["config-emisor", "--definicion", definicion, "--carga", str(sin), "--config", str(config)]) == 0
    assert "no se toca" in capsys.readouterr().out


def test_el_ayudante_con_una_carga_de_otro_producto(archivos, capsys):
    carpeta, definicion, _, _ = archivos
    otra = carpeta / "otra.json"
    otra.write_text(json.dumps(_carga("restolibra")), encoding="utf-8")
    assert ins.main(["validar", "--definicion", definicion, "--carga", str(otra)]) == 2
    assert "es de 'restolibra'" in capsys.readouterr().err


def test_ejecutar_un_console_script(capsys):
    """Asi corren las migraciones en el nodo: sin los .exe de Scripts."""
    assert ins.main(["ejecutar", "libraedge-nodo", "--help"]) == 0
    assert "sincronizar" in capsys.readouterr().out
    assert ins.main(["ejecutar", "libraedge-nodo", "comando-que-no-existe"]) == 2  # argparse sale con 2
    assert ins.main(["ejecutar", "no-existe-este-comando"]) == 2
    assert "no-existe-este-comando" in capsys.readouterr().err


def test_ejecutar_devuelve_lo_que_devuelve_el_script(monkeypatch, capsys):
    class Punto:
        def __init__(self, funcion):
            self.funcion = funcion

        def load(self):
            return self.funcion

    import importlib.metadata as metadata

    def con(funcion):
        monkeypatch.setattr(metadata, "entry_points", lambda **_: [Punto(funcion)])

    con(lambda: None)
    assert ins.ejecutar("x", []) == 0
    con(lambda: 3)
    assert ins.ejecutar("x", []) == 3

    def sale_con_texto():
        raise SystemExit("fallo la migracion")

    con(sale_con_texto)
    assert ins.ejecutar("x", []) == 1
    assert "fallo la migracion" in capsys.readouterr().err
